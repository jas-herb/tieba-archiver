"""从 thread.json 生成可读版本：thread.md 和 index.html。

thread.json 来自 tieba_dump.py。
图片、视频等本地文件用相对路径引用，所以 md / html 要和 images/ 等文件夹放在一起。
不依赖第三方库。

用法:
    python render.py tieba_<帖子ID>   # 重新生成该文件夹里的 thread.md 和 index.html

长帖的 md 会分页（thread.md、thread_2.md……），避免 GitHub 网页上文件太大显示不出来。
要发给别人，用 topdf.py 生成 PDF。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
from html import escape
from pathlib import Path, PurePosixPath

VOICE_URL = "https://tiebac.baidu.com/c/p/voice?voice_md5={md5}&play_from=pb_voice_play"
FOLD_COMMENTS = 5  # 楼中楼超过这么多条时，默认只显示前几条


# ---------------------------------------------------------------- 通用


def write_text_atomic(path: Path, text: str):
    """先写临时文件再替换：中途被打断（关窗口、断电）也不会留下写了一半的文件。一律用 \\n 换行。"""
    tmp = path.with_name(f".{path.name}.tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def parse_tid(text: str) -> int | None:
    """从链接或输入里认出帖子 ID。优先认链接里的 /p/数字（或 kz=数字）；没有链接时，
    只认开头就是一串数字的（比如 threads.txt 里的「ID 备注」），
    免得把分享文案标题里的日期、QQ 号之类当成帖子。"""
    m = re.search(r"/p/(\d+)", text) or re.search(r"[?&]kz=(\d+)", text) or re.match(r"\s*(\d{6,})(?!\S)", text)
    return int(m.group(1)) if m else None


def parse_tids(text: str) -> list[int]:
    """一次输入里的多个帖子。有链接就只认链接；没有链接时认用空格、逗号或换行隔开的纯数字 ID。"""
    links = re.findall(r"/p/(\d+)|[?&]kz=(\d+)", text)
    if links:
        found = [int(a or b) for a, b in links]
    else:
        found = [int(t) for t in re.split(r"[\s,，;；、]+", text) if re.fullmatch(r"\d{6,}", t)]
    return list(dict.fromkeys(found))  # 去重，保持顺序


def safe_url(u: str) -> str:
    """页面里只把 http(s) 链接和本地相对路径做成链接或图片；
    其他协议（javascript:、tiebaclient: 之类）一律不做成链接，只显示文字。"""
    u = (u or "").strip()
    m = re.match(r"([a-zA-Z][a-zA-Z0-9+.-]*):", u)
    if m:
        return u if m.group(1).lower() in ("http", "https") else ""
    return "" if u.startswith("//") else u


def iter_parts(item: dict):
    """按原顺序产出 (类型, 片段, 对应图片)。类型去掉 aiotieba 的 _p/_c 后缀，如 Image_p -> Image。"""
    images = item.get("images", [])
    i = 0
    for fr in item.get("fragments", []):
        kind = fr["type"].split("_")[0]
        img = None
        if kind == "Image":
            img = images[i] if i < len(images) else {}
            i += 1
        yield kind, fr, img


def comments_of(f: dict) -> list:
    """楼中楼按时间排序（接口返回的预览顺序不按时间）。"""
    return sorted(f.get("comments", []), key=lambda c: (c["create_time"], c.get("pid", 0)))


def link_url(fr: dict) -> str:
    return safe_url(fr.get("url") or fr.get("raw_url") or "")


def link_title(fr: dict) -> str:
    """链接显示的文字：贴吧里显示的标题（比如「百度网盘」），没有标题才显示网址本身。"""
    return fr.get("title") or fr.get("text") or fr.get("url") or fr.get("raw_url") or ""


def is_unknown(kind: str) -> bool:
    return kind == "Unknown"  # aiotieba 认不出的内容


def voice_src(fr: dict) -> str:
    return safe_url(fr.get("src") or VOICE_URL.format(md5=fr.get("md5", "")))


# ---------------------------------------------------------------- Markdown


# 帖子正文放进 Markdown 时要转义：不然 <!-- 会把后面的楼层整段变成注释，行首的 # - 1. 会变成标题、列表，
# * _ 会变成斜体。换行也要换成 <br>：GitHub 上仓库里的 md 文件，单个换行不会另起一行（前后两段会接在一起）。
_MD_INLINE = re.compile(r"([\\`*_\[\]|~])")
BR = "\x00"  # 正文里的换行先用占位符，最后统一换成 <br>


def md_escape(line: str) -> str:
    """一行纯文字放进 Markdown：会被当成格式的字符都转义掉，显示出来和原文一样。"""
    s = line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    s = _MD_INLINE.sub(r"\\\1", s)
    s = re.sub(r"^[ \t]+", lambda m: "&nbsp;" * len(m.group()), s)  # 行首空格会变成代码块
    s = re.sub(r"^([#+=-])", r"\\\1", s)  # 标题、列表、分隔线
    return re.sub(r"^(\d+)([.)])", r"\1\\\2", s)  # 有序列表


def md_text(text: str) -> str:
    return BR.join(md_escape(line) for line in text.split("\n"))


def md_url(u: str) -> str:
    return u.replace(" ", "%20").replace("(", "%28").replace(")", "%29").replace("<", "%3C").replace(">", "%3E")


def frag_md(kind: str, fr: dict, img: dict | None) -> str:
    if kind == "Image":
        src = safe_url(img.get("local") or img.get("origin_src", ""))
        return f"\n\n![]({md_url(src)})\n\n" if src else "\\[图片\\]"
    if kind == "Emoji":
        return f"\\[{md_escape(fr.get('desc') or '表情')}\\]"
    if kind == "Link":
        url = link_url(fr)
        text = md_text(link_title(fr))
        return f"[{text or md_escape(url)}]({md_url(url)})" if url else text
    if kind == "Video":
        src = safe_url(fr.get("src", ""))
        return f"\n\n[视频]({md_url(src)})\n\n" if src else "\\[视频\\]"
    if kind == "Voice":
        src = voice_src(fr)
        return f"[语音]({md_url(src)})" if src else "\\[语音\\]"
    if is_unknown(kind):
        return md_text(fr["text"]) if fr.get("text") else "\\[未识别的内容\\]"
    return md_text(fr.get("text", ""))


def body_md(item: dict) -> str:
    s = "".join(frag_md(*p) for p in iter_parts(item))
    s = re.sub(f"[{BR} \t\n]*\n\n[{BR} \t\n]*", "\n\n", s)  # 图片、视频前后多余的换行
    return s.strip(f"{BR} \t\n").replace(BR, "<br>\n")


def floor_meta(f: dict) -> list[str]:
    meta = [f["create_time"][:16]]
    if f.get("agree"):
        meta.append(f"赞 {f['agree']}")
    return meta




# 长帖的 md 按楼层号分页：thread.md 是 1–100 楼，thread_2.md 是 101–200 楼……
# 被删掉、没存下来的楼层不影响分段。某一段内容特别多（比如楼中楼特别多）时，
# 这一段再拆开（thread_2_2.md……），因为 GitHub 网页上 Markdown 太大会显示不全甚至不显示
# （实测仓库首页在 512,000 字节处截断，官方没有公开准确上限）。
MD_PAGE_FLOORS = 100
MD_PAGE_BYTES = 300_000


def gone_note(item: dict) -> str:
    return f"{item['missing_since'][:10]} 起已看不到（被删除或隐藏），以下是之前保存的内容"


def versions_md(item: dict) -> list[str]:
    vs = item.get("earlier_versions", [])
    if not vs:
        return []
    lines = ["<details><summary>这一层的内容后来变了，点开查看之前的版本</summary>", ""]
    for v in vs:
        lines += [f"**{v['seen_at'][:16]} 时的版本：**", "", body_md(v), ""]
    return lines + ["</details>", ""]


def floor_md(f: dict) -> str:
    u = f["user"]
    tag = "（楼主）" if f["is_thread_author"] else ""
    lv = [f"Lv.{u['level']}"] if u.get("level") else []
    meta = f"<sub>{' · '.join(lv + floor_meta(f))}</sub>"
    content = [body_md(f), ""] + versions_md(f)
    for c in comments_of(f):
        mark = "〔已删除〕" if c.get("missing_since") else ""
        first, *rest = body_md(c).split("\n")
        content.append(f"> **{md_escape(c['user']['show_name'])}** <sub>{c['create_time']}</sub>：{mark}{first}")
        content += [f"> {line}" if line else ">" for line in rest]
        content.append(">")

    if f.get("missing_since"):
        # 已删除：保留，但正文默认收起，点开才显示
        lines = [
            "---",
            f"<details><summary><b>{f['floor']}楼 · {escape(u['show_name'])}{tag}</b>（已删除，点击展开）</summary>",
            "",
            meta,
            "",
            f"> ⚠️ {gone_note(f)}",
            "",
        ] + content + ["", "</details>"]
    else:
        lines = ["---", f"### {f['floor']}楼 · {md_escape(u['show_name'])}{tag}", meta, ""] + content
    lines.append("")
    return "\n".join(lines)


def deleted_summary(data: dict) -> str:
    gone_f = sum(1 for f in data["floors"] if f.get("missing_since"))
    gone_c = sum(1 for f in data["floors"] for c in f["comments"] if c.get("missing_since"))
    parts = [f"{gone_f} 层" if gone_f else "", f"{gone_c} 条楼中楼" if gone_c else ""]
    parts = [x for x in parts if x]
    if not parts:
        return ""
    return f"其中 {'、'.join(parts)}已被删除或隐藏（保留的是之前存下的内容）"


def thread_gone_note(data: dict) -> str:
    since = data.get("thread_gone_since")
    return f"原帖从 {since[:16]} 起已经看不到了（被删除或隐藏），下面是之前保存的内容" if since else ""


def paginate(blocks: list[tuple[dict, str]], stem: str) -> list[dict]:
    """按楼层号分段，返回页面列表：[{"name", "label", "items"}]。第一段（1–100 楼）一定存在，文件名是 thread.md。"""
    groups: dict[int, list] = {0: []}
    for f, text in blocks:
        groups.setdefault((max(f["floor"], 1) - 1) // MD_PAGE_FLOORS, []).append((f, text))
    pages = []
    for k in sorted(groups):
        lo, hi = k * MD_PAGE_FLOORS + 1, (k + 1) * MD_PAGE_FLOORS
        parts, cur, size = [], [], 0
        for f, text in groups[k]:  # 这一段太大时再拆开
            n = len(text.encode("utf-8"))
            if cur and size + n > MD_PAGE_BYTES:
                parts.append(cur)
                cur, size = [], 0
            cur.append((f, text))
            size += n
        parts.append(cur)
        base = stem if k == 0 else f"{stem}_{k + 1}"
        for j, items in enumerate(parts, 1):
            label = f"{lo}–{hi} 楼" + (f"（第 {j}/{len(parts)} 部分）" if len(parts) > 1 else "")
            name = f"{base}.md" if j == 1 else f"{stem}_{k + 1}_{j}.md"
            pages.append({"name": name, "label": label, "items": items})
    return pages


def page_nav(pages: list[dict], current: int) -> str:
    items = [f"**{p['label']}**" if i == current else f"[{p['label']}]({p['name']})" for i, p in enumerate(pages)]
    return "页码：" + " · ".join(items)


def jump_links(pages: list[dict], current: int) -> tuple[str, str]:
    """每页顶部和底部的跳转链接：本页顶部/底部，不在最后一页时再加「最新的楼层」（最后一页的末尾）。
    GitHub 会保留 <a name> 锚点，所以跨文件的 thread_N.md#bottom 也能跳到位。"""
    if len(pages) == 1:
        return "[⬇ 跳到最后（最新的楼层）](#bottom)", "[⬆ 回到顶部](#top)"
    if current == len(pages) - 1:
        return "[⬇ 本页底部（最新的楼层）](#bottom)", "[⬆ 本页顶部](#top)"
    latest = f"[⏬ 最新的楼层]({pages[-1]['name']}#bottom)"
    return f"[⬇ 本页底部](#bottom)　·　{latest}", f"[⬆ 本页顶部](#top)　·　{latest}"


def write_markdown(data: dict, path: Path):
    """写 thread.md；帖子太长时拆成 thread.md、thread_2.md、thread_3.md……，每页带页码导航。"""
    header = [
        '<a name="top"></a>',
        "",
        f"# {md_escape(data['title'])}",
        "",
        f"- 贴吧: {md_escape(data['forum'])}吧　楼主: {md_escape(data['author']['show_name'])}　发帖: {data['create_time']}",
        f"- 原帖: {data['url']}　抓取时间: {data['fetched_at']}　楼层数: {len(data['floors'])}　（时间均为北京时间）",
    ]
    if note := deleted_summary(data):
        header.append(f"- {note}")
    if note := thread_gone_note(data):
        header.append(f"- **{note}**")
    header.append("")
    pages = paginate([(f, floor_md(f)) for f in data["floors"]], path.stem)
    names = {p["name"] for p in pages}

    # 清理上次生成、这次用不到的分页文件（thread_2.md、thread_2_2.md 这种）
    for old in path.parent.glob(f"{path.stem}_*.md"):
        if re.fullmatch(rf"{re.escape(path.stem)}_\d+(_\d+)?\.md", old.name) and old.name not in names:
            old.unlink()

    for i, page in enumerate(pages):
        nav = [page_nav(pages, i), ""] if len(pages) > 1 else []
        to_end, to_top = jump_links(pages, i)
        body = [text for _, text in page["items"]] or [f"（{page['label']}没有存档内容）", ""]
        tail = ["---", '<a name="bottom"></a>', "", to_top, ""]
        write_text_atomic(path.parent / page["name"], "\n".join(header + [to_end, ""] + nav + body + nav + tail))


# ---------------------------------------------------------------- HTML


def frag_html(kind: str, fr: dict, img: dict | None) -> str:
    if kind == "Text":
        return escape(fr.get("text", ""))
    if kind == "Emoji":
        return f'<span class="emo">{escape(fr.get("desc") or "表情")}</span>'
    if kind == "Image":
        src = escape(safe_url(img.get("local") or img.get("origin_src", "")))
        if not src:
            return '<span class="missing">[图片]</span>'
        w, h = img.get("width"), img.get("height")
        dims = f' width="{w}" height="{h}"' if w and h else ""
        gif = '<span class="gif">动图（这里只显示第一帧）</span>' if img.get("print_animated") else ""
        if img.get("print_parts"):  # 生成 PDF 时切成几段的长截图
            parts = "".join(f'<img src="{escape(safe_url(p))}" alt="">' for p in img["print_parts"])
            return f'<a class="pic long" href="{src}" target="_blank">{parts}</a>'
        cls = "pic tall" if img.get("print_tall") else "pic"  # 生成 PDF 时标记的竖长截图
        return f'<a class="{cls}" href="{src}" target="_blank"><img src="{src}"{dims} loading="lazy" alt="">{gif}</a>'
    if kind == "At":
        return f'<span class="at">{escape(fr.get("text", ""))}</span>'
    if kind == "Link":
        url = link_url(fr)
        text = escape(link_title(fr) or url)
        if not url:
            return text
        return f'<a href="{escape(url)}" target="_blank" rel="noopener noreferrer">{text}</a>'
    if kind == "Video":
        if src := safe_url(fr.get("src", "")):
            return f'<a class="pill" href="{escape(src)}" target="_blank" rel="noopener noreferrer">▶ 视频（外链）</a>'
        return '<span class="missing">[视频]</span>'
    if kind == "Voice":
        if src := voice_src(fr):
            return f'<a class="pill" href="{escape(src)}" target="_blank" rel="noopener noreferrer">♪ 语音（外链）</a>'
        return '<span class="missing">[语音]</span>'
    if is_unknown(kind):
        tip = "贴吧的这种内容本工具暂时认不出来，原始数据保存在 thread.json 里"
        return f'<span class="unk" title="{tip}">{escape(fr.get("text") or "[未识别的内容]")}</span>'
    return escape(fr.get("text", ""))


def body_html(item: dict) -> str:
    return "".join(frag_html(*p) for p in iter_parts(item)).strip()


def avatar_html(u: dict, cls: str = "av") -> str:
    name = u.get("show_name") or "?"
    hue = sum(map(ord, name)) * 37 % 360
    return f'<span class="{cls}" style="--h:{hue}" aria-hidden="true">{escape(name[:1])}</span>'


def comment_html(c: dict, hidden: bool) -> str:
    u = c["user"]
    lz = '<span class="badge">楼主</span>' if c.get("is_thread_author") else ""
    gone = ""
    cls = "sub more" if hidden else "sub"
    if c.get("missing_since"):
        gone = f'<span class="badge del" title="{escape(gone_note(c))}">已删除</span>'
        cls += " gone"
    return (
        f'<div class="{cls}">{avatar_html(u, "av sm")}<div>'
        f'<span class="sname">{escape(u["show_name"])}</span>{lz}{gone}：'
        f'<span class="sbody">{body_html(c)}</span>'
        f'<span class="stime">{escape(c["create_time"][:16])}</span>{versions_html(c, "这条回复")}</div></div>'
    )


def versions_html(item: dict, what: str = "这一层") -> str:
    vs = item.get("earlier_versions", [])
    if not vs:
        return ""
    parts = "".join(
        f'<div class="ver"><div class="meta">{escape(v["seen_at"][:16])} 时的版本</div>'
        f'<div class="body">{body_html(v)}</div></div>'
        for v in vs
    )
    return f'<details class="vers"><summary>{what}的内容后来变了，点开查看之前的版本</summary>{parts}</details>'


def floor_html(f: dict) -> str:
    u = f["user"]
    lz = '<span class="badge">楼主</span>' if f["is_thread_author"] else ""
    lv = f'<span class="lv">Lv.{u["level"]}</span>' if u.get("level") else ""
    meta = [f'<a href="#f{f["floor"]}">{f["floor"]}楼</a>'] + [escape(m) for m in floor_meta(f)]

    subs = ""
    cs = comments_of(f)
    if cs:
        items = "".join(comment_html(c, i >= FOLD_COMMENTS) for i, c in enumerate(cs))
        btn = ""
        if len(cs) > FOLD_COMMENTS:
            btn = f'<button class="unfold" type="button">展开剩余 {len(cs) - FOLD_COMMENTS} 条回复</button>'
        subs = f'<div class="subs">{items}{btn}</div>'

    gone = ""
    cls = "floor"
    content = f'<div class="body">{body_html(f)}</div>{versions_html(f)}{subs}'
    if f.get("missing_since"):
        # 已删除：保留，但正文默认收起，点开才显示
        gone = '<span class="badge del">已删除</span>'
        cls += " gone"
        content = (
            f'<details class="gone-wrap"><summary>这一层已被删除或隐藏，点击展开查看保存的内容</summary>'
            f'<p class="gone-note">⚠️ {escape(gone_note(f))}</p>{content}</details>'
        )
    return (
        f'<article class="{cls}" id="f{f["floor"]}" data-lz="{int(bool(f["is_thread_author"]))}">'
        f'<header>{avatar_html(u)}<div class="who">'
        f'<div class="name">{escape(u["show_name"])}{lz}{gone}{lv}</div>'
        f'<div class="meta">{" · ".join(meta)}</div></div></header>'
        f'{content}</article>'
    )


CSS = """
:root{--bg:#f6f7f9;--card:#fff;--text:#1f2328;--muted:#6b7280;--line:#e5e7eb;--accent:#3b6fd8;
--accent-soft:#e8effc;--sub:#f3f4f6;--emo-bg:#fff4d6;--emo:#8a5a00;--del:#b42318;--del-bg:#fdecea;color-scheme:light}
@media (prefers-color-scheme:dark){:root{--bg:#111318;--card:#1a1d24;--text:#e6e8eb;--muted:#9aa1ac;
--line:#2b303a;--accent:#7aa2f7;--accent-soft:#1f2a44;--sub:#21252d;--emo-bg:#3a2f14;--emo:#f3c969;--del:#f7a19a;--del-bg:#3b1d1b;color-scheme:dark}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);line-height:1.7;
font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Hiragino Sans GB","Microsoft YaHei","Noto Sans CJK SC",sans-serif}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
.wrap{max-width:760px;margin:0 auto;padding:0 16px 64px}
.top{padding:32px 0 16px}
.forum{color:var(--accent);font-weight:600;font-size:14px}
h1{font-size:24px;line-height:1.35;margin:6px 0 12px}
.facts{color:var(--muted);font-size:13px;margin:2px 0}
.bar{position:sticky;top:0;z-index:5;display:flex;gap:8px;align-items:center;flex-wrap:wrap;
padding:10px 0;background:var(--bg);border-bottom:1px solid var(--line);margin-bottom:16px}
.bar button,.bar input{font:inherit;font-size:14px;border:1px solid var(--line);background:var(--card);
color:var(--text);border-radius:8px;padding:5px 12px}
.bar button{cursor:pointer}
.bar button[aria-pressed=true]{background:var(--accent);border-color:var(--accent);color:#fff}
.bar input[type=number]{width:110px}
.bar #q{flex:1 1 180px;min-width:0}
.bar .count{color:var(--muted);font-size:13px;margin-left:auto}
.floor{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;margin:0 0 12px;scroll-margin-top:64px}
.floor:target{border-color:var(--accent);box-shadow:0 0 0 2px var(--accent-soft)}
.floor>header{display:flex;gap:10px;align-items:center;margin-bottom:10px}
.av{width:40px;height:40px;border-radius:50%;flex:none;object-fit:cover;display:inline-flex;align-items:center;
justify-content:center;font-weight:600;color:#fff;background:hsl(var(--h,210) 45% 55%)}
.av.sm{width:22px;height:22px;font-size:12px}
.name{font-weight:600;display:flex;align-items:center;gap:6px;flex-wrap:wrap}
.badge{font-size:11px;font-weight:600;color:var(--accent);background:var(--accent-soft);border-radius:4px;padding:0 5px;line-height:18px}
.lv{font-size:11px;color:var(--muted);border:1px solid var(--line);border-radius:4px;padding:0 4px;line-height:16px}
.meta{font-size:12px;color:var(--muted)}.meta a{color:inherit}
.body{white-space:pre-wrap;word-break:break-word;font-size:15.5px}
.pic{display:block;margin:8px 0;white-space:normal}
.pic img{display:block;max-width:100%;height:auto;border-radius:8px;background:var(--sub)}
.emo{display:inline-block;font-size:12px;line-height:18px;padding:0 5px;margin:0 1px;border-radius:9px;
background:var(--emo-bg);color:var(--emo);vertical-align:1px;white-space:nowrap}
.at{color:var(--accent)}
.pill{display:inline-block;font-size:13px;border:1px solid var(--line);border-radius:6px;padding:0 8px}
.missing,.unk{color:var(--muted);font-style:italic}
.subs{margin-top:12px;background:var(--sub);border-radius:8px;padding:6px 12px}
.sub{display:flex;gap:8px;align-items:flex-start;padding:6px 0;font-size:14px;border-bottom:1px dashed var(--line)}
.sub:last-of-type{border-bottom:0}.sub .av{margin-top:2px}
.sname{color:var(--accent);font-weight:600}
.sbody{white-space:pre-wrap;word-break:break-word}
.stime{color:var(--muted);font-size:12px;margin-left:8px;white-space:nowrap}
.sub.more{display:none}.subs.open .sub.more{display:flex}.subs.open .unfold{display:none}
.unfold{font:inherit;font-size:13px;color:var(--accent);background:none;border:0;padding:6px 0;cursor:pointer}
.lz-only .floor[data-lz="0"],.floor[hidden]{display:none}
::highlight(hit){background:#ffe066;color:#1f2328}
.empty{color:var(--muted);text-align:center;margin:32px 0}
.badge.del{color:var(--del);background:var(--del-bg)}
.floor.gone{border-style:dashed;border-color:var(--del)}
.gone-note{margin:8px 0;font-size:13px;color:var(--del)}
.thread-gone{margin:10px 0;padding:8px 12px;border-radius:8px;font-size:14px;color:var(--del);background:var(--del-bg)}
.gone-wrap>summary{cursor:pointer;font-size:14px;color:var(--del)}
.vers{margin-top:10px;font-size:14px}.vers summary{cursor:pointer;color:var(--muted)}
.ver{border-left:3px solid var(--line);padding-left:10px;margin-top:8px}
footer{color:var(--muted);font-size:12px;text-align:center;margin-top:32px}
@media (max-width:520px){.bar #q{flex-basis:100%}h1{font-size:20px}.floor{padding:12px;border-radius:10px;scroll-margin-top:120px}.body{font-size:15px}
.stime{display:block;margin-left:0}.bar .count{width:100%;margin-left:0}}
"""

JS = """
const floors=[...document.querySelectorAll('.floor')], main=document.querySelector('main');
const lzBtn=document.getElementById('lz'), revBtn=document.getElementById('rev');
const q=document.getElementById('q'), cnt=document.getElementById('cnt'), empty=document.getElementById('empty');
const text=new Map(floors.map(f=>[f,f.textContent.toLowerCase()]));
function refresh(){
  const kw=q.value.trim().toLowerCase(), lz=document.body.classList.contains('lz-only');
  let n=0;
  for(const f of floors){
    const hit=!kw||text.get(f).includes(kw);
    f.hidden=!hit;
    if(hit&&kw){ /* 关键词在收起的内容里时自动展开 */
      f.querySelectorAll('details').forEach(d=>{if(d.textContent.toLowerCase().includes(kw))d.open=true;});
      f.querySelectorAll('.sub.more').forEach(c=>{if(c.textContent.toLowerCase().includes(kw))c.parentElement.classList.add('open');});
    }
    if(hit&&(!lz||f.dataset.lz==='1'))n++;
  }
  cnt.textContent=kw?`找到 ${n} 层`:lz?`只看楼主：${n} 层`:`共 ${n} 层`;
  empty.hidden=n>0;
  mark(kw);
}
function mark(kw){ /* 高亮匹配的文字（浏览器不支持时跳过，不影响搜索） */
  if(!(window.CSS&&CSS.highlights&&window.Highlight))return;
  CSS.highlights.delete('hit');
  if(!kw)return;
  const ranges=[];
  for(const f of floors){
    if(f.hidden)continue;
    const w=document.createTreeWalker(f,NodeFilter.SHOW_TEXT);
    for(let node=w.nextNode();node&&ranges.length<3000;node=w.nextNode()){
      const t=node.nodeValue.toLowerCase();
      for(let i=t.indexOf(kw);i>=0;i=t.indexOf(kw,i+kw.length)){
        const r=new Range();r.setStart(node,i);r.setEnd(node,i+kw.length);ranges.push(r);
      }
    }
  }
  CSS.highlights.set('hit',new Highlight(...ranges));
}
let timer;
q.addEventListener('input',()=>{clearTimeout(timer);timer=setTimeout(refresh,200);});
lzBtn.addEventListener('click',()=>{lzBtn.setAttribute('aria-pressed',document.body.classList.toggle('lz-only'));refresh();});
revBtn.addEventListener('click',()=>{
  const desc=revBtn.getAttribute('aria-pressed')!=='true';
  revBtn.setAttribute('aria-pressed',desc);
  for(const f of desc?[...floors].reverse():floors)main.appendChild(f); /* 逐个移动：楼层上万时一次展开参数会报错 */
});
document.getElementById('jump').addEventListener('submit',e=>{
  e.preventDefault();const n=+e.target.n.value;if(!n)return;
  const vis=floors.filter(f=>f.offsetParent!==null);
  const t=vis.find(f=>+f.id.slice(1)>=n)||vis[vis.length-1];
  if(t)location.hash=t.id;
});
document.addEventListener('click',e=>{if(e.target.classList.contains('unfold'))e.target.parentElement.classList.add('open');});
refresh();
"""


def render_html(data: dict) -> str:
    author = data["author"]
    floors = data["floors"]
    n_comments = sum(len(f["comments"]) for f in floors)
    n_images = sum(len(f.get("images", [])) for f in floors)
    body = "".join(floor_html(f) for f in floors)
    tid = data.get("tid", "")
    note = deleted_summary(data)
    gone_html = f'<p class="facts gone-note">{escape(note)}</p>\n' if note else ""
    if thread_note := thread_gone_note(data):
        gone_html = f'<p class="thread-gone">⚠️ {escape(thread_note)}</p>\n' + gone_html
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(data['title'])}</title>
<style>{CSS}</style>
</head>
<body>
<div class="wrap" id="top">
<header class="top">
<div class="forum">{escape(data['forum'])}吧</div>
<h1>{escape(data['title'])}</h1>
<p class="facts">楼主 {escape(author['show_name'])} · 发帖于 {escape(data['create_time'][:16])} · {len(floors)} 层 · {n_comments} 条楼中楼 · {n_images} 张图</p>
{gone_html}<p class="facts">原帖 <a href="{escape(data['url'])}" target="_blank" rel="noopener noreferrer">tieba.baidu.com/p/{tid}</a> · 存档于 {escape(data['fetched_at'][:16])} · 时间均为北京时间</p>
</header>
<nav class="bar">
<input id="q" type="search" placeholder="搜索正文或作者" aria-label="搜索正文或作者">
<button id="lz" type="button" aria-pressed="false">只看楼主</button>
<button id="rev" type="button" aria-pressed="false">倒序</button>
<form id="jump"><input name="n" type="number" min="1" placeholder="跳到楼层" aria-label="跳到楼层"></form>
<a href="#top">回到顶部</a>
<a href="#bottom">跳到最后</a>
<span class="count" id="cnt"></span>
</nav>
<p class="empty" id="empty" hidden>没有找到包含这个关键词的楼层。</p>
<main>{body}</main>
<footer id="bottom">本地存档 · 抓取于 {escape(data['fetched_at'][:16])}</footer>
</div>
<script>{JS}</script>
</body>
</html>
"""


def write_html(data: dict, path: Path):
    write_text_atomic(path, render_html(data))


# ---------------------------------------------------------------- 文件


def link_or_copy(src: Path, dst: Path) -> bool:
    """把文件放到 dst：优先硬链接（同一块磁盘上不额外占空间），失败则复制。源文件不存在返回 False。"""
    if not src.is_file():
        return False
    if dst.exists():
        return True
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)
    return True


def is_safe_rel(rel: str) -> bool:
    """只接受存档文件夹里面的相对路径（thread.json 可能来自别人，不能借 ../ 读别处的文件）。"""
    p = PurePosixPath(rel.replace("\\", "/"))
    return bool(p.parts) and not p.is_absolute() and ".." not in p.parts and not re.match(r"[a-zA-Z]:", rel)


def render_dir(folder: Path):
    data = json.loads((folder / "thread.json").read_text(encoding="utf-8"))
    write_markdown(data, folder / "thread.md")
    write_html(data, folder / "index.html")


def main():
    ap = argparse.ArgumentParser(description="从 thread.json 生成 thread.md 和 index.html")
    ap.add_argument("folder", type=Path, help="包含 thread.json 的存档文件夹")
    args = ap.parse_args()
    render_dir(args.folder)
    print(f"已生成 {args.folder / 'thread.md'} 和 {args.folder / 'index.html'}")


if __name__ == "__main__":
    main()
