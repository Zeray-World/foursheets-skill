#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Foursheets · 四文件式多身份记忆

目录约定（每个身份一个文件夹）：
    AGENTS.md   用户手写规则，只读
    SOUL.md     用户手写人设，只读
    MEMORY.md   长期记忆，本脚本维护
    USER.md     用户画像，模型写
    journal/    只追加的日记
    state.json  策略与计数器

本脚本只写 MEMORY.md / journal / state.json，永不写 SOUL.md / AGENTS.md。

退出码：0 成功 / 1 运行失败 / 2 参数错误
"""

import argparse
import json
import os
import re
import sys
from datetime import date, timedelta
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

SEC_FACT, SEC_COMMIT, SEC_DEAD = "事实", "约定", "已作废"
READONLY = ("SOUL.md", "AGENTS.md")
DUP_THRESHOLD = 0.72
ENTRY_RE = re.compile(r"^-\s*\[([FC]-\d{4})\]\s+(\d{4}-\d{2}-\d{2})\s+(.*)$")
SEP = " ｜"


def die(msg, code=1):
    print(msg, file=sys.stderr)
    sys.exit(code)


def today():
    return date.today().isoformat()


def bigrams(s):
    s = re.sub(r"\s+", "", s or "")
    return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) > 1 else ({s} if s else set())


def similarity(a, b):
    """Jaccard + 包含度。中文短句对长条目时，包含度比 Jaccard 更能反映"说的是同一件事"。"""
    A, B = bigrams(a), bigrams(b)
    if not A or not B:
        return 0.0
    inter = len(A & B)
    jac = inter / len(A | B)
    if min(len(A), len(B)) >= 8:
        return max(jac, inter / min(len(A), len(B)))
    return jac


def locate(root, ident):
    if not root.exists():
        die(f"记忆库不存在：{root}", 2)
    dirs = [d for d in sorted(root.iterdir()) if d.is_dir()]
    key = ident.strip()
    pad = key.zfill(3) if key.isdigit() else None
    for d in dirs:
        code = d.name.split("-", 1)[0]
        if d.name == key or (pad and code == pad) or (not key.isdigit() and key in d.name):
            return d
    die(f"找不到编号 {ident}。现有：" + ("、".join(d.name for d in dirs) or "（空）"), 2)


def load_state(pdir):
    p = pdir / "state.json"
    if not p.exists():
        return {"num": pdir.name.split("-")[0], "name": pdir.name.split("-", 1)[-1],
                "policy": None, "last_brief": "", "seq": {"F": 0, "C": 0}}
    return json.loads(p.read_text(encoding="utf-8"))


def save_state(pdir, st):
    (pdir / "state.json").write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------- MEMORY.md

def parse_memory(text):
    entries, cur = [], SEC_FACT
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.startswith("## "):
            name = line[3:].strip()
            cur = name if name in (SEC_FACT, SEC_COMMIT, SEC_DEAD) else cur
            continue
        m = ENTRY_RE.match(line.strip())
        if not m:
            continue
        eid, frm, rest = m.groups()
        parts = [p.strip() for p in rest.split(SEP) if p.strip()]
        body, fields = parts[0], parts[1:]
        meta = {"tags": [], "flags": {}}
        for f in fields:
            if f.startswith("~"):
                k, _, v = f[1:].partition(":")
                meta["flags"][k] = v or True
            else:
                meta["tags"].append(f)
        entries.append({"id": eid, "from": frm, "text": body, "section": cur, **meta})
    return entries


def fmt_entry(e):
    parts = [e["text"]] + list(e.get("tags") or [])
    parts += [f"~{k}:{v}" if v is not True else f"~{k}" for k, v in (e.get("flags") or {}).items()]
    return f"- [{e['id']}] {e['from']} " + SEP.join(parts)


def render_memory(entries, st):
    def block(sec):
        rows = [fmt_entry(e) for e in entries if e["section"] == sec]
        return "\n".join(rows) if rows else "（空）"

    head = (
        "# MEMORY\n\n"
        "<!-- 这是什么：长期记忆。这是干嘛的：模型读写的事实与约定；"
        "SOUL.md 与 AGENTS.md 不许碰。文件由 persona.py 维护，手改也认。 -->\n"
    )
    body = "".join(f"\n## {sec}\n\n{block(sec)}\n" for sec in (SEC_FACT, SEC_COMMIT, SEC_DEAD))
    return head + body


def read_memory(pdir):
    p = pdir / "MEMORY.md"
    return parse_memory(p.read_text(encoding="utf-8")) if p.exists() else []


def write_memory(pdir, entries, st):
    (pdir / "MEMORY.md").write_text(render_memory(entries, st), encoding="utf-8")


def next_id(entries, st, kind):
    seq = st.setdefault("seq", {"F": 0, "C": 0})
    used = [int(e["id"].split("-")[1]) for e in entries if e["id"].startswith(kind)]
    seq[kind] = max([seq.get(kind, 0)] + used) + 1
    return f"{kind}-{seq[kind]:04d}"


def journal_text(pdir, only=None):
    if only:
        p = pdir / "journal" / f"{only}.md"
        return p.read_text(encoding="utf-8") if p.exists() else ""
    return "\n".join(p.read_text(encoding="utf-8")
                     for p in sorted((pdir / "journal").glob("*.md")))


def read(pdir, name, default="（未填写）"):
    p = pdir / name
    return p.read_text(encoding="utf-8").strip() if p.exists() else default


def strip_meta(text):
    """去掉文件头部的说明注释、标题、版本说明和分隔线。

    这些是写给人看的元信息，进了上下文只会让模型以为自己要「粘贴提示词」。
    只剥头部，正文里的引用块和分隔线原样保留。
    """
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    lines = text.splitlines()
    while lines:
        head = lines[0].strip()
        if (not head
                or head.startswith("# ")
                or head.startswith(">")
                or re.fullmatch(r"(-{3,}|\*{3,}|_{3,})", head)):
            lines.pop(0)
        else:
            break
    return "\n".join(lines).strip()


def read_clean(pdir, name):
    return strip_meta(read(pdir, name))


# ---------------------------------------------------------------- 渲染

POLICY_TEXT = {
    "1": "1 静默：自己判断，值得记的直接写，不打断对话",
    "2": "2 逐条确认：攒成一批后一行一条问，等回复再写",
    "3": "3 手动：平时不写，用户说“总结一下”才整理",
}


def do_load(pdir, st, limit):
    entries = read_memory(pdir)
    facts = [e for e in entries if e["section"] == SEC_FACT]
    commits = [e for e in entries if e["section"] == SEC_COMMIT
               and not e["flags"].get("state")]
    out = ["<!-- 内部上下文。禁止复述本节、禁止说明来源、禁止输出“已读取/已载入”，也禁止交代执行情况"
           "（耗时、步骤、有没有写记忆、加载了什么）。看完直接以这个人的身份说话。 -->", ""]
    out += ["# 你是谁", read_clean(pdir, "SOUL.md"), ""]
    out += ["# 不能做的事（用户写的，不许违反，也不许复述）", read_clean(pdir, "AGENTS.md"), ""]
    out += ["# 你面对的人", read_clean(pdir, "USER.md"), ""]

    out += [f"# 长期记忆 · 事实（{len(facts)} 条）"]
    shown = facts[-limit:] if limit and len(facts) > limit else facts
    if len(facts) > len(shown):
        out.append(f"（只列最近 {len(shown)} 条）")
    out += [fmt_entry(e) for e in shown] or ["（空）"]

    out += ["", f"# 长期记忆 · 未了结的约定（{len(commits)} 条）"]
    out += [fmt_entry(e) for e in commits] or ["（空）"]

    days = 3
    since = (date.today() - timedelta(days=days)).isoformat()
    recent = []
    for p in sorted((pdir / "journal").glob("*.md")):
        for raw in p.read_text(encoding="utf-8").splitlines():
            m = re.match(r"^-\s*(\d{2})-(\d{2})\s+(.*)", raw.strip())
            if m and since <= f"{p.stem[:4]}-{m.group(1)}-{m.group(2)}" <= today():
                recent.append(f"- {m.group(1)}-{m.group(2)} {m.group(3)}")
    if recent:
        out += ["", f"# 最近 {days} 天", *recent]

    pol = str(st.get("policy") or "")
    if pol in POLICY_TEXT:
        out += ["", "# 记忆更新策略", POLICY_TEXT[pol]]
    else:
        out += [
            "",
            "# ⚠ 本轮必须完成的一件事（不做就是失败）",
            "",
            "记忆策略还没设。这一轮回复的**结尾**，必须原样给出下面这段三选一，然后停下等回答。",
            "",
            "记忆怎么更新？",
            "1 全自动 —— 我自己判断，值得记的直接写，不打断你",
            "2 逐条确认 —— 攒几条一起问你，你说记才记",
            "3 手动 —— 平时不写，你说“总结一下”我才整理",
            "",
            "三条硬规矩：",
            "- **不许跳过。** “弹这个会破坏代入”不是理由：先按角色把话说完，菜单附在后面，两件事不冲突",
            "- **不许替用户选**，也不许默认填 1 号",
            "- 拿到数字后执行 `python persona.py -c policy -i 编号 -p 数字`，之后不再问第二次",
        ]
    print("\n".join(out))


def render_brief(pdir, st, days=3):
    entries = read_memory(pdir)
    since = (date.today() - timedelta(days=days)).isoformat()
    opens = [e for e in entries if e["section"] == SEC_COMMIT and not e["flags"].get("state")]
    stale = [e for e in entries if "review" in e["flags"] and str(e["flags"]["review"]) < today()]
    dead = [e for e in entries if e["section"] == SEC_DEAD and str(e["flags"].get("until", "")) >= since]

    lines = [f"# {today()} 醒来 · {st.get('name', '')}", "", "## 还没了结"]
    lines += [fmt_entry(e) for e in opens] or ["- 无"]
    if stale:
        lines += ["", "## 该复查了"] + [fmt_entry(e) for e in stale]
    if dead:
        lines += ["", "## 最近作废"] + [fmt_entry(e) for e in dead]
    lines += ["", f"## 最近 {days} 天"]
    hits = []
    for p in sorted((pdir / "journal").glob("*.md")):
        for raw in p.read_text(encoding="utf-8").splitlines():
            m = re.match(r"^-\s*(\d{2})-(\d{2})\s+(.*)", raw.strip())
            if m and since <= f"{p.stem[:4]}-{m.group(1)}-{m.group(2)}" <= today():
                hits.append(f"- {m.group(1)}-{m.group(2)} {m.group(3)}")
    lines += hits or ["- 无记录"]
    text = "\n".join(lines) + "\n"
    (pdir / "brief").mkdir(exist_ok=True)
    (pdir / "brief" / "latest.md").write_text(text, encoding="utf-8")
    st["last_brief"] = today()
    save_state(pdir, st)
    return text


# ---------------------------------------------------------------- 写入

def apply_ops(pdir, st, ops, import_mode=False):
    entries = read_memory(pdir)
    by_id = {e["id"]: e for e in entries}
    written, rejected = [], []

    for op in ops:
        kind = op.get("op")

        if kind in ("add", "supersede"):
            text = (op.get("text") or "").strip()
            mtype = op.get("type", "fact")
            if len(text) < 4:
                rejected.append({"op": kind, "reason": "内容过短", "text": text})
                continue

            dup = max(entries, key=lambda e: similarity(text, e["text"]), default=None)
            if kind == "add" and dup and similarity(text, dup["text"]) >= DUP_THRESHOLD:
                rejected.append({"op": kind, "reason": "疑似重复", "dup": dup["id"],
                                 "dup_text": dup["text"], "text": text})
                continue

            quote = (op.get("quote") or "").strip()
            if not import_mode:
                if not quote:
                    rejected.append({"op": kind, "reason": "缺少 quote 引文", "text": text})
                    continue
                src = str(op.get("source") or "").replace("journal/", "").replace(".md", "")[:7]
                if quote not in journal_text(pdir, src):
                    rejected.append({"op": kind, "reason": f"引文未在 journal/{src}.md 命中", "text": text})
                    continue

            e = {"id": next_id(entries, st, "C" if mtype == "commit" else "F"),
                 "from": op.get("from") or today(), "text": text,
                 "section": SEC_COMMIT if mtype == "commit" else SEC_FACT,
                 "tags": list(op.get("tags") or []),
                 "flags": {}}
            if op.get("confidence") == "inferred":
                e["flags"]["inferred"] = True
            if op.get("review"):
                e["flags"]["review"] = op["review"]
            if op.get("due"):
                e["flags"]["due"] = op["due"]

            if kind == "supersede":
                old = by_id.get(op.get("old"))
                if not old:
                    rejected.append({"op": kind, "reason": f"找不到旧条目 {op.get('old')}", "text": text})
                    continue
                if str(e["from"]) <= str(old["from"]):
                    rejected.append({"op": kind, "reason": "新证据日期不比旧条目更晚",
                                     "old": old["id"], "text": text})
                    continue
                old["section"] = SEC_DEAD
                old["flags"]["until"] = e["from"]
                old["flags"]["by"] = e["id"]
                e["flags"]["supersedes"] = old["id"]

            entries.append(e)
            by_id[e["id"]] = e
            written.append({"id": e["id"], "op": kind, "text": text})

        elif kind == "close":
            e = by_id.get(op.get("id"))
            if not e:
                rejected.append({"op": "close", "reason": f"找不到 {op.get('id')}"})
                continue
            state = op.get("state") or "done"
            e["flags"]["state"] = state
            e["flags"]["until"] = op.get("until") or today()
            written.append({"id": e["id"], "op": "close", "state": state})
        else:
            rejected.append({"op": str(kind), "reason": "未知操作"})

    write_memory(pdir, entries, st)
    save_state(pdir, st)
    return {"written": written, "rejected": rejected}


def cmd_check(pdir, text):
    entries = [e for e in read_memory(pdir) if e["section"] != SEC_DEAD]
    scored = sorted(((similarity(text, e["text"]), e) for e in entries), key=lambda x: -x[0])
    hits = [{"id": e["id"], "score": round(s, 2), "section": e["section"], "text": e["text"]}
            for s, e in scored[:5] if s >= 0.30]
    verdict = "重复" if hits and hits[0]["score"] >= DUP_THRESHOLD else ("相近" if hits else "新")
    print(json.dumps({"verdict": verdict, "hits": hits}, ensure_ascii=False, indent=2))


def cmd_audit(pdir, st):
    entries = read_memory(pdir)
    problems, warns = [], []
    active = [e for e in entries if e["section"] != SEC_DEAD]
    for i, a in enumerate(active):
        for b in active[i + 1:]:
            s = similarity(a["text"], b["text"])
            if s >= 0.60:
                warns.append(f"{a['id']} ≈ {b['id']} 相似度 {s:.2f}，可能重复")
    for e in active:
        if e["section"] == SEC_COMMIT and e["flags"].get("due") and str(e["flags"]["due"]) < today():
            warns.append(f"{e['id']} 已过期未结案（{e['flags']['due']}）")
        if e["flags"].get("review") and str(e["flags"]["review"]) < today():
            warns.append(f"{e['id']} 过复查日（{e['flags']['review']}）")
    for e in entries:
        if e["section"] == SEC_DEAD and "by" in e["flags"] and e["flags"]["by"] not in {x["id"] for x in entries}:
            problems.append(f"{e['id']} 的取代者 {e['flags']['by']} 不存在")
    size = (pdir / "MEMORY.md").stat().st_size if (pdir / "MEMORY.md").exists() else 0
    if size > 40000:
        warns.append(f"MEMORY.md 已 {size // 1024}KB，建议合并同类条目")
    stat = {}
    for e in entries:
        key = e["section"] if e["section"] != SEC_COMMIT else "约定/" + str(e["flags"].get("state", "open"))
        stat[key] = stat.get(key, 0) + 1
    print(json.dumps({"身份": st.get("name"), "策略": st.get("policy"), "规模": f"{size}B",
                      "分布": stat, "问题": problems, "提醒": warns},
                     ensure_ascii=False, indent=2))
    return 1 if problems else 0


STICKER_GROUPS = [
    ("讨要", ["token", "付款", "要钱", "二维码", "讨"], "讨 token、要东西、开玩笑要钱"),
    ("撒娇卖萌", ["撒娇", "卖萌", "萌", "娇", "Ciallo", "眨眼", "花", "腼腆", "得意"], "撒娇、卖乖、被夸、求关注"),
    ("摆烂犯困", ["困", "醒", "瘫", "摆烂", "悠闲", "敷", "兴致"], "犯困、摆烂、不想动、敷衍"),
    ("惊讶慌张", ["惊", "慌", "感叹", "好奇", "兴奋", "问号", "未响应", "呆滞", "うつけ", "期待", "雀跃"], "吃惊、被问到、装傻、慌"),
    ("求饶认怂", ["求饶", "对不起", "交给我", "迫害"], "认错、求饶、推脱、拒绝"),
]
STICKER_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp"}


def sticker_token(group, fname):
    """占位写法：情绪-名字。编号会随加图漂移，一律不用。

    文件名本身已经以情绪开头时不再重复加前缀，`讨要-举二维码` 就是 `讨要-举二维码`。
    """
    stem = Path(fname).stem
    return stem if stem.startswith(group) else f"{group}-{stem}"


def cmd_stickers(base, keyword=None):
    """列出表情包并重建索引。选图前必须跑一次，不靠记忆硬套。"""
    d = base / "表情包"
    if not d.exists():
        die(f"没有表情包目录：{d}", 1)
    files = sorted(p.name for p in d.iterdir()
                   if p.is_file() and p.suffix.lower() in STICKER_EXT)
    if not files:
        die("表情包目录是空的", 1)

    rows = []
    for f in files:
        group, use = "其他", "通用"
        for g, kws, u in STICKER_GROUPS:
            if any(k.lower() in f.lower() for k in kws):
                group, use = g, u
                break
        rows.append((f, group, use))

    idx = ["# 表情包索引", "",
           f"<!-- 由 `persona.py -c stickers` 生成，共 {len(rows)} 张。加图后重跑一次即可。 -->", "",
           "发不了图的框架，在回复里写 `[表情包:情绪-名字]` 指代，写法见「占位写法」列。",
           "**不要用编号**：编号会随加图漂移，情绪+名字不会。", "",
           "| 占位写法 | 文件 | 情绪 | 什么时候用 |", "| --- | --- | --- | --- |"]
    for f, g, u in rows:
        idx.append(f"| `[表情包:{sticker_token(g, f)}]` | `{f}` | {g} | {u} |")
    (d / "索引.md").write_text("\n".join(idx) + "\n", encoding="utf-8")

    if keyword:
        kw = keyword.lower()
        rows = [r for r in rows if kw in r[0].lower() or kw in r[1].lower() or kw in r[2].lower()]
        if not rows:
            die(f"没有匹配「{keyword}」的表情包", 1)

    for f, g, u in rows:
        print(f"[表情包:{sticker_token(g, f)}]   ← {f}")
    print(f"\n共 {len(rows)} 张 · 索引已更新：{d / '索引.md'}")


def cmd_log(pdir, text, kind, daily_cap=3):
    """日记闸门：必须归类、必须够长、必须不重复、每天最多几条。不合格直接拒。"""
    kinds = {"commit": "约定", "record": "记录", "turn": "转折", "pit": "坑", "fun": "趣事"}
    if not kind:
        die("log 需要 -k 类型：commit/record/turn/pit/fun\n"
            "分不出类型，就说明这件事不值得记。", 2)
    if kind not in kinds:
        die(f"未知类型 {kind}，只能是：commit/record/turn/pit/fun", 2)

    t = (text or "").strip()
    if len(t) < 8:
        die("太短，像是流水账。说清发生了什么、有什么后果。", 1)
    if len(t) > 140:
        die("超过 140 字。日记是索引不是作文，截到一句能看懂。", 1)

    jp = pdir / "journal" / f"{today()[:7]}.md"
    existing = jp.read_text(encoding="utf-8").splitlines() if jp.exists() else []
    today_prefix = f"- {today()[5:]} "

    todays = [l for l in existing if l.startswith(today_prefix)]
    if len(todays) >= daily_cap:
        die(f"今天已经记了 {len(todays)} 条。日记不是聊天记录，到此为止。", 1)

    for line in existing:
        m = re.match(r"^-\s*\d{2}-\d{2}\s+(?:〔.+?〕)?\s*(.*)$", line.strip())
        if m and similarity(t, m.group(1)) >= 0.55:
            die(f"和已有条目重复：{line.strip()[:60]}", 1)

    jp.parent.mkdir(exist_ok=True)
    if not jp.exists():
        jp.write_text(f"# {today()[:7]}\n\n", encoding="utf-8")
    with jp.open("a", encoding="utf-8") as f:
        f.write(f"- {today()[5:]} 〔{kinds[kind]}〕 {t}\n")
    print(str(jp))


def main():
    ap = argparse.ArgumentParser(description="Foursheets · 四文件式多身份记忆")
    ap.add_argument("-c", "--cmd", required=True,
                    choices=["list", "load", "brief", "check", "commit", "import",
                             "policy", "audit", "new", "log", "stickers"])
    ap.add_argument("-i", "--id", help="身份编号：2 / 002 / 002-Iris")
    ap.add_argument("-r", "--root", help="记忆库根目录")
    ap.add_argument("-f", "--file", help="ops JSON 路径")
    ap.add_argument("-t", "--text", help="文本（check / log）")
    ap.add_argument("-k", "--kind", help="日记类型：commit/record/turn/pit/fun（log 必填）")
    ap.add_argument("-q", "--query", help="按关键词筛表情包（stickers 用）")
    ap.add_argument("-p", "--policy", help="1 / 2 / 3，0 清空")
    ap.add_argument("-n", "--name", help="新建身份名字")
    ap.add_argument("--limit", type=int, default=120)
    ap.add_argument("--days", type=int, default=3)
    a = ap.parse_args()

    root = Path(a.root or os.environ.get("PERSONA_ROOT")
                or Path(__file__).resolve().parent / "记忆库")

    if a.cmd == "stickers":
        cmd_stickers(Path(__file__).resolve().parent, a.query or a.text)
        return 0

    if a.cmd == "list":
        for d in sorted(x for x in root.iterdir() if x.is_dir()):
            st = load_state(d)
            print(f"{d.name}\t策略={st.get('policy') or '未设置'}\t"
                  f"事实={len([e for e in read_memory(d) if e['section'] == SEC_FACT])}\t"
                  f"约定={len([e for e in read_memory(d) if e['section'] == SEC_COMMIT])}")
        return 0

    if a.cmd == "new":
        if not a.id or not a.name:
            die("new 需要 -i 编号 -n 名字", 2)
        pdir = root / f"{a.id.zfill(3)}-{a.name}"
        if pdir.exists():
            die(f"已存在：{pdir}", 1)
        (pdir / "journal").mkdir(parents=True)
        st = {"num": a.id.zfill(3), "name": a.name, "policy": None, "last_brief": "",
              "seq": {"F": 0, "C": 0}, "created": today()}
        for f in ("AGENTS.md", "SOUL.md", "USER.md"):
            src = root / "000-模板" / f
            if src.exists():
                (pdir / f).write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        write_memory(pdir, [], st)
        save_state(pdir, st)
        print(str(pdir))
        return 0

    if not a.id:
        die("除 list 外都必须带 -i 编号", 2)
    pdir = locate(root, a.id)
    st = load_state(pdir)

    if a.cmd == "load":
        do_load(pdir, st, a.limit)
    elif a.cmd == "brief":
        print(render_brief(pdir, st, a.days))
        save_state(pdir, st)
    elif a.cmd == "check":
        if not a.text:
            die("check 需要 -t 文本", 2)
        cmd_check(pdir, a.text)
    elif a.cmd == "log":
        if not a.text:
            die("log 需要 -t 文本", 2)
        cmd_log(pdir, a.text, a.kind)
    elif a.cmd == "audit":
        return cmd_audit(pdir, st)
    elif a.cmd == "policy":
        if a.policy not in ("1", "2", "3", "0"):
            die("policy 只能是 1 / 2 / 3 / 0", 2)
        st["policy"] = None if a.policy == "0" else a.policy
        save_state(pdir, st)
        print(POLICY_TEXT.get(str(st["policy"]), "策略已清空"))
    elif a.cmd in ("commit", "import"):
        if not a.file:
            die(f"{a.cmd} 需要 -f ops.json", 2)
        data = json.loads(Path(a.file).read_text(encoding="utf-8"))
        res = apply_ops(pdir, st, data.get("ops", []), import_mode=(a.cmd == "import"))
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 1 if res["rejected"] else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
