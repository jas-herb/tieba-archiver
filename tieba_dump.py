"""抓取一个百度贴吧帖子的全部楼层、楼中楼、作者信息和图片。

走的是贴吧手机客户端接口（aiotieba），不解析网页，公开帖子不需要登录。

用法:
    pip install -r requirements.txt
    python tieba_dump.py                           # 不带参数：问答模式，一步步提示（存到程序旁边的「贴吧存档」）
    python tieba_dump.py <帖子ID>                  # 即 tieba.baidu.com/p/<帖子ID>，也可以直接贴整个链接
    python tieba_dump.py <帖子ID> --no-images      # 只要文字
    python tieba_dump.py <帖子ID> --login          # 用自己的账号抓（被隐藏、只有自己看得到的帖子）
    python tieba_dump.py --update-all [文件夹]      # 更新文件夹里已存的全部帖子（默认是程序旁边的「贴吧存档」）
    python tieba_dump.py <帖子ID> --images-only    # 不重新抓文字，只接着下载还没下完的图片
--login 会提示粘贴 BDUSS（网页登录贴吧后在浏览器 Cookie 里找），输入时不显示，
只在本机内存里用来请求贴吧，不写入任何文件。也可以用环境变量 TIEBA_BDUSS 传入。
输出目录 tieba_<帖子ID>/ 下有:
    thread.json   完整结构化数据（每个人只存昵称、等级、是否吧务，不存用户 ID、IP 属地等个人信息）
    thread.md     按楼层排版的可读版本（引用本地图片）
    index.html    浏览器打开的网页版（搜索、倒序、只看楼主、跳楼层）
    images/       原图，文件名 <楼层>_<序号>_<hash>.<ext>
命令行模式的返回码：0 完成；1 出错；4 原帖看不到了（已有存档原样保留）；
5 图片还没下完（到了 TIEBA_MAX_MB 体积上限，再运行 --images-only 接着下）；6 找不到帖子，也从没存过。
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import logging
import os
import random
import re
import sys
import time
import webbrowser
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiotieba as tb
import httpx

from render import parse_tid, write_html, write_markdown, write_text_atomic

RN = 30                 # 每页楼层数（客户端接口上限 30）
COMMENT_RN = 30         # 每层随楼层一起取回的楼中楼条数
MAX_COMMENT_FAILS = 5   # 超过这么多层的楼中楼都取不到，多半是网络问题：整个帖子这次算失败
REQUEST_DELAY = (0.8, 1.6)  # 每次接口请求之间随机停顿，避免触发风控
IMG_CONCURRENCY = 4
RECHECK_FLOORS, RECHECK_COMMENTS = 5, 20  # 一次少了这么多内容时，再抓一次核对，免得把限流当成删除
MISSING_RETRY_DAYS = 10  # 楼层被删后这么多天内，没下到的图片还会再试（原图链接大约两周后失效）


# 存档里的时间一律用北京时间（UTC+8，没有夏令时），不受运行这台电脑的时区影响
BEIJING = timezone(timedelta(hours=8))


def ts(t: int) -> str:
    return datetime.fromtimestamp(t, BEIJING).strftime("%Y-%m-%d %H:%M:%S") if t else ""


# 不同版本的 aiotieba 返回的贴吧网址有的是 http://、有的是 https://（内容一样），
# 统一成 https://，免得换了 Python 版本后被误判为「内容有变化」
_TIEBA_HTTP = re.compile(r"http://((?:tieba|tiebapic|tiebac|imgsrc)\.baidu\.com)/")


def https(s):
    return _TIEBA_HTTP.sub(r"https://\1/", s) if isinstance(s, str) else s


def user_dict(u) -> dict:
    # 只存昵称、等级、是否吧务；用户 ID、portrait、用户名、IP 属地这些能追踪到账号的信息一律不存
    return {
        "show_name": getattr(u, "show_name", ""),
        "level": getattr(u, "level", 0),
        "is_bawu": getattr(u, "is_bawu", False),
    }


def unknown_payload(raw) -> dict:
    """aiotieba 认不出的内容（比如楼中楼里的「梗百科」词条、贴吧以后新加的类型）：
    原始数据整个存下来，能认出文字的顺便取出来显示，免得内容被悄悄吞掉。"""
    try:
        if hasattr(raw, "DESCRIPTOR"):  # protobuf 消息
            from google.protobuf.json_format import MessageToDict

            raw = MessageToDict(raw, preserving_proto_field_name=True)
        elif isinstance(raw, Mapping):
            raw = dict(raw)
        json.dumps(raw)
    except Exception:
        raw = str(raw)
    text = raw.get("text", "") if isinstance(raw, dict) else ""
    return {"text": https(text) if isinstance(text, str) else "", "raw": raw}


def frag_dict(obj) -> dict:
    """把 contents.objs 里的片段（文字/表情/图片/@/链接/视频…）按原顺序转成 dict。"""
    d = {"type": type(obj).__name__.removeprefix("Frag")}
    if d["type"] == "Unknown":
        return {**d, **unknown_payload(getattr(obj, "data", None))}
    for name in dir(obj):
        if name.startswith("_") or name in ("from_proto", "from_json", "user_id"):  # @ 里的用户 ID 不存
            continue
        try:
            val = getattr(obj, name)
        except Exception:  # 个别属性是现算的，数据不规范时会出错（比如链接缺参数）：跳过这一项，别让整个帖子失败
            continue
        if callable(val):
            continue
        if isinstance(val, (str, int, float, bool)) or val is None:
            d[name] = https(val)
        elif isinstance(val, tuple):
            d[name] = list(val)
        elif type(val).__name__ == "URL":  # 链接片段的 url 是 yarl.URL
            d[name] = https(str(val))
    return d


def contents_dict(contents) -> dict:
    imgs = [
        {
            "origin_src": https(im.origin_src),
            "big_src": https(im.big_src),
            "hash": im.hash,
            "width": im.show_width,
            "height": im.show_height,
            "origin_size": im.origin_size,
        }
        for im in getattr(contents, "imgs", ())  # 楼中楼的 contents 没有 imgs
    ]
    return {"fragments": [frag_dict(o) for o in contents.objs], "images": imgs}


def comment_dict(c) -> dict:
    return {
        "pid": c.pid,
        "user": user_dict(c.user),
        "is_thread_author": bool(getattr(c, "is_thread_author", False)),
        "create_time": ts(c.create_time),
        "agree": getattr(c, "agree", 0),  # aiotieba 4.8 起，随楼层带回的楼中楼不再有点赞数
        "text": https(c.text),
        **contents_dict(c.contents),
    }


class ArchiveError(Exception):
    """给用户看的错误：直接显示这句话，不显示技术细节。"""


class ThreadGone(ArchiveError):
    """帖子整个看不到了（被删除，或者被隐藏、只有发帖人自己看得到）。"""


class BadLogin(ArchiveError):
    """BDUSS 无效或已过期。"""


class NeedsLogin(ArchiveError):
    """这个帖子不登录看不到（之前是登录后才存下来的），这次没有登录。"""


GONE_CODES = {4, 350008}  # 4「贴子可能已被删除」（也包括被隐藏的），350008「该贴已被删除」
EXIT_GONE = 4  # 命令行模式：原帖看不到了，但已有的存档保留、没有出错
EXIT_MORE_IMAGES = 5  # 到了体积上限，还有图片没下完
EXIT_NOT_FOUND = 6  # 找不到这个帖子，而且从没存过


NOT_FOUND_HINT = (
    "找不到这个帖子：刚发的帖子可能还在审核中（通常几分钟到半小时，过一会儿再试）；\n"
    "也可能已经被删除，或者被隐藏了（只有发帖人自己看得到）——如果是你自己发的，登录后才能保存。"
)
NOT_FOUND_LOGGED_IN = "登录后也找不到这个帖子：可能还在审核中（过一会儿再试），或者已经被删除了。"
NEEDS_LOGIN_HINT = "这个帖子不登录看不到（之前是登录后才存下来的），需要登录才能更新。"


async def pause():
    await asyncio.sleep(random.uniform(*REQUEST_DELAY))


async def get_comments_retry(client, tid: int, pid: int, pn: int):
    for attempt in range(3):
        await pause()
        cs = await client.get_comments(tid, pid, pn=pn)
        if not cs.err:
            return cs
        print(f"楼中楼请求失败（{cs.err}），重试…")
        await asyncio.sleep(3 * (attempt + 1))
    raise ArchiveError(f"获取楼中楼失败（{cs.err}），请检查网络后稍后重试。")


async def fetch_all_comments(client, tid: int, pid: int) -> list:
    out, seen, pn = [], set(), 1
    while True:
        cs = await get_comments_retry(client, tid, pid, pn)
        for c in cs:
            if c.pid not in seen:  # 翻页时有回复被删，页边界可能重复一条
                seen.add(c.pid)
                out.append(comment_dict(c))
        if not cs.page.has_more:
            return out
        pn += 1


async def get_posts_retry(client, tid: int, pn: int):
    for attempt in range(3):
        posts = await client.get_posts(
            tid, pn=pn, rn=RN, with_comments=True, comment_rn=COMMENT_RN
        )
        err = posts.err
        if not err:
            if len(posts) or pn > max(posts.page.total_page, 1):
                return posts
            err = "这一页是空的"  # 还没到最后一页却没有内容：多半是被限流了，当作出错重试
        elif getattr(err, "code", None) in GONE_CODES:  # 帖子没了：重试也没用
            raise ThreadGone(NOT_FOUND_HINT)
        print(f"第 {pn} 页请求失败（{err}），重试…")
        await asyncio.sleep(3 * (attempt + 1))
    raise ArchiveError(f"请求贴吧失败（{err}），请检查网络后稍后重试。")


async def visible_without_login(tid: int) -> bool | None:
    """不登录能不能看到这个帖子；网络出错之类看不出来时返回 None。"""
    try:
        await pause()
        async with tb.Client() as client:
            posts = await client.get_posts(tid, pn=1, rn=1)
    except Exception:
        return None
    if not posts.err:
        return True
    return False if getattr(posts.err, "code", None) in GONE_CODES else None


async def fetch_thread(tid: int, bduss: str = "") -> dict:
    floors, thread_info, failed = [], None, []
    async with tb.Client(BDUSS=bduss) as client:
        if bduss:
            me = await client.get_self_info()
            if not me.user_id:
                raise BadLogin("BDUSS 无效或已过期，请在浏览器里重新登录贴吧后再复制一次。")
            print(f"已登录为: {me.show_name}")
        pn = 1
        while True:
            posts = await get_posts_retry(client, tid, pn)
            if thread_info is None:
                th = posts.thread
                thread_info = {
                    "tid": tid,
                    "title": th.title,
                    "forum": posts.forum.fname,
                    "fid": posts.forum.fid,
                    "author": user_dict(th.user),
                    "create_time": ts(th.create_time),
                    "reply_num": th.reply_num,
                    "agree": getattr(th, "agree", 0),
                    "url": f"https://tieba.baidu.com/p/{tid}",
                }
            print(f"第 {pn}/{posts.page.total_page} 页，{len(posts)} 层")

            for p in posts:
                comments = [comment_dict(c) for c in p.comments]
                incomplete = False
                # get_posts 顺带返回的楼中楼只是预览：只有前面一部分有内容（后面的是空壳），
                # 顺序也不按时间。不完整时单独用 get_comments 拉全。
                if p.reply_num > len(comments) or any(not c["fragments"] for c in comments):
                    try:
                        comments = await fetch_all_comments(client, tid, p.pid)
                    except ArchiveError:
                        # 这一层可能正好被删了：先留下预览里能看到的，之前存过的楼中楼合并时照样保留，下次更新再补全
                        failed.append(p.floor)
                        if len(failed) > MAX_COMMENT_FAILS:
                            raise ArchiveError(f"有 {len(failed)} 层的楼中楼都取不到，请检查网络后稍后重试。") from None
                        print(f"第 {p.floor} 楼的楼中楼这次没取全（可能这一层刚被删了），先保留能看到的，下次更新再补。")
                        comments = [c for c in comments if c["fragments"]]
                        incomplete = True
                comments.sort(key=lambda c: (c["create_time"], c["pid"]))
                floors.append({
                    "floor": p.floor,
                    "pid": p.pid,
                    "user": user_dict(p.user),
                    "is_thread_author": p.is_thread_author,
                    "create_time": ts(p.create_time),
                    "agree": getattr(p, "agree", 0),  # 点赞数可有可无：aiotieba 升级时去掉了也不影响存档
                    "disagree": getattr(p, "disagree", 0),
                    "text": https(p.text),
                    **contents_dict(p.contents),
                    "reply_num": p.reply_num,
                    "comments": comments,
                    **({"comments_incomplete": True} if incomplete else {}),  # 只在合并时用，保存前去掉
                })

            if not posts.page.has_more:
                if pn < posts.page.total_page:  # 说没有下一页了，页数却对不上：内容不完整
                    raise ArchiveError(
                        f"这次只拿到了 {pn}/{posts.page.total_page} 页，内容不完整（可能被贴吧限流了），请稍后再试。")
                break
            pn += 1
            await pause()

    # 分页边界偶尔会重复一层，按 pid 去重
    seen, uniq = set(), []
    for f in sorted(floors, key=lambda f: f["floor"]):
        if f["pid"] not in seen:
            seen.add(f["pid"])
            uniq.append(f)
    return {**thread_info, "fetched_at": ts(int(datetime.now().timestamp())), "floors": uniq}


def recently_missing(item: dict, days: int = MISSING_RETRY_DAYS) -> bool:
    """被删除 / 隐藏才没几天：原图链接多半还有效，没下到的图片值得再试。"""
    try:
        since = datetime.strptime(item["missing_since"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=BEIJING)
    except (KeyError, ValueError):
        return False
    return datetime.now(BEIJING) - since < timedelta(days=days)


async def download_images(data: dict, img_dir: Path, deadline: float | None = None,
                          max_bytes: int | None = None, fetch: bool = True) -> bool:
    """下载还没下过的图片，已经下载过的直接用本地文件。
    到了 deadline（时间戳）或者这次下载的体积到了 max_bytes，就不再开始新的下载，剩下的下次接着下。
    fetch=False 时只认本地已有的文件、不下载。返回 True 表示因为体积上限停下、还有图片没下完。"""
    img_dir.mkdir(parents=True, exist_ok=True)
    jobs = []  # (文件名前缀, 图片, 是否已被删除)
    for f in data["floors"]:
        f_gone = bool(f.get("missing_since"))
        if f_gone and not recently_missing(f):  # 删了很久的楼层：原链接早已失效，不再尝试
            continue
        for i, im in enumerate(f["images"], 1):
            jobs.append((f"{f['floor']:04d}_{i:02d}_{im['hash']}", im, f_gone))
        for c in f["comments"]:
            c_gone = f_gone or bool(c.get("missing_since"))
            if c.get("missing_since") and not recently_missing(c):
                continue
            for i, im in enumerate(c["images"], 1):
                jobs.append((f"{f['floor']:04d}_c{c['pid']}_{i:02d}_{im['hash']}", im, c_gone))

    have = {}  # 文件名前缀 -> 已有的文件名（先列一次目录，不要每张图都扫一遍）
    for p in img_dir.iterdir():
        if p.name.startswith("."):
            if p.name.endswith(".part"):  # 上次下载被打断留下的半张图
                p.unlink(missing_ok=True)
            continue
        have.setdefault(p.name.split(".")[0], p.name)

    sem = asyncio.Semaphore(IMG_CONCURRENCY)
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://tieba.baidu.com/"}
    done = late = 0
    got_bytes, budget_hit = 0, False
    show_progress = sys.stdout.isatty() and jobs and fetch

    async def one(http, stem, im, gone):
        nonlocal done
        try:
            await fetch_one(http, stem, im, gone)
        finally:
            done += 1
            if show_progress:
                print(f"\r下载图片 {done}/{len(jobs)}", end="", flush=True)

    async def fetch_one(http, stem, im, gone):
        nonlocal late, got_bytes, budget_hit
        if stem in have:
            im["local"] = f"images/{have[stem]}"
            return
        if not fetch or not im.get("origin_src"):
            return
        async with sem:
            if deadline and time.time() > deadline:
                late += 1
                return
            if max_bytes and got_bytes >= max_bytes:
                budget_hit = True
                return
            for attempt in range(3):
                try:
                    r = await http.get(im["origin_src"])
                    r.raise_for_status()
                    ext = {"image/png": "png", "image/gif": "gif", "image/webp": "webp"}.get(
                        r.headers.get("content-type", "").split(";")[0], "jpg"
                    )
                    path = img_dir / f"{stem}.{ext}"
                    part = img_dir / f".{stem}.part"  # 先写临时文件再改名：中途被打断也不会留下半张图
                    part.write_bytes(r.content)
                    part.replace(path)
                    got_bytes += len(r.content)
                    im["local"] = f"images/{path.name}"
                    return
                except Exception as e:  # 一张图出问题（网络、链接失效、磁盘）不影响其他图片
                    if attempt == 2:
                        if not gone:  # 已删除楼层的图片多半已经失效，不用一条条报
                            print(f"图片下载失败 {stem}: {type(e).__name__}: {e}")
                        return
                    await asyncio.sleep(2 * (attempt + 1))

    async with httpx.AsyncClient(headers=headers, timeout=30, follow_redirects=True) as http:
        await asyncio.gather(*(one(http, stem, im, gone) for stem, im, gone in jobs))
    if show_progress:  # 擦掉进度行，免得下面较短的一行后面残留旧字符
        print("\r" + " " * 40 + "\r", end="")
    if not fetch:
        return False
    missing = sum("local" not in im for _, im, _ in jobs)
    print(f"图片 {len(jobs) - missing}/{len(jobs)} 张")
    if budget_hit:
        print(f"这次已经下载了 {got_bytes // 2**20} MB 图片，先停一下（免得超过上传上限），剩下的 {missing} 张接着下载")
    elif late:
        print(f"时间不够，还有 {late} 张图片这次没下载，下次更新时会接着下载")
    return budget_hit


def clean_bduss(raw: str) -> str:
    # 容错：复制时可能带上 "BDUSS=" 前缀、引号、分号或空白
    bduss = re.sub(r"^\s*BDUSS\s*[=:]\s*", "", raw).strip().strip("'\";")
    # 这个长度检查不能去掉：长度不对时，aiotieba 会把 BDUSS 原文写进报错信息，而问答模式会把报错显示出来
    if bduss and len(bduss) != 192:
        raise ArchiveError(f"BDUSS 应该是 192 个字符，粘贴的内容有 {len(bduss)} 个。请只复制 Cookie 里「值」那一栏。")
    return bduss


# ---------------------------------------------------------------- 只增不减：和已有存档合并


MAX_VERSIONS = 20


def _content_sig(item: dict):
    return item.get("text", ""), tuple(im.get("hash") for im in item.get("images", []))


def _merge_lists(old_items: list, new_items: list, old_time: str, now: str, sort_key, merge_children=None,
                 mark_missing: bool = True) -> list:
    """按 pid 合并。新旧都有：内容变了就把旧版本存进 earlier_versions；
    只有旧的有（这次看不到了）：保留旧内容，并记下第一次发现看不到的时间 missing_since
    （mark_missing 为 False 时只保留、不标记：这次本来就没取全）。"""
    old_by_pid = {x["pid"]: x for x in old_items}
    seen, out = set(), []
    for x in new_items:
        seen.add(x["pid"])
        o = old_by_pid.get(x["pid"])
        if o is not None:
            versions = list(o.get("earlier_versions", []))
            # 内容变了才存旧版本；来回变（比如被折叠又恢复）时同样的内容只存一次，最多存 MAX_VERSIONS 个
            if _content_sig(o) != _content_sig(x) and all(_content_sig(v) != _content_sig(o) for v in versions):
                versions.append({"seen_at": old_time, "text": o.get("text", ""),
                                 "fragments": o.get("fragments", []), "images": o.get("images", [])})
                if len(versions) > MAX_VERSIONS:  # 留下最早的原始版本和最近的几个
                    versions = versions[:1] + versions[-(MAX_VERSIONS - 1):]
            if versions:
                x["earlier_versions"] = versions
            if merge_children:
                merge_children(o, x)
        out.append(x)
    for pid, o in old_by_pid.items():
        if pid not in seen:
            kept = dict(o)
            if mark_missing:
                kept.setdefault("missing_since", now)
            out.append(kept)
    return sorted(out, key=sort_key)


def merge_archive(old: dict, new: dict) -> dict:
    """存档只增不减：被删除 / 隐藏的楼层和楼中楼保留下来并做标记，内容变化时保留旧版本。"""
    old_time, now = old.get("fetched_at", ""), new["fetched_at"]

    def merge_comments(o: dict, x: dict):
        x["comments"] = _merge_lists(o.get("comments", []), x.get("comments", []), old_time, now,
                                     lambda c: (c["create_time"], c["pid"]),
                                     mark_missing=not x.get("comments_incomplete"))

    new["floors"] = _merge_lists(old["floors"], new["floors"], old_time, now,
                                 lambda f: (f["floor"], f["pid"]), merge_comments)
    return new


def load_existing(out: Path, tid: int) -> dict | None:
    """读已有的存档；还没有存档返回 None。存档读不出来、或者存的是别的帖子时报错：
    绝不能当成「没有存档」再整个覆盖掉，那样之前保留下来的已删除内容就永远没了。"""
    path = out / "thread.json"
    if not path.exists():
        return None
    try:
        old = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ArchiveError(
            f"存档文件读不出来：{path}\n"
            "可能是上次保存时被打断了。为了不覆盖已经存下的内容，这次没有更新。\n"
            "请把这个文件夹改个名字留作备份，再重新保存一次。") from None
    if not isinstance(old, dict) or old.get("tid") != tid:
        other = old.get("tid") if isinstance(old, dict) else "?"
        raise ArchiveError(f"文件夹「{out}」里存的是另一个帖子（{other}），为了不覆盖它，这次没有保存。请换一个文件夹。")
    return old


def save_archive(data: dict, out: Path):
    out.mkdir(parents=True, exist_ok=True)
    write_text_atomic(out / "thread.json", json.dumps(data, ensure_ascii=False, indent=2))
    write_markdown(data, out / "thread.md")
    write_html(data, out / "index.html")


def vanished(old: dict, new: dict) -> tuple[int, int]:
    """上次还在、这次没看到的楼层数和楼中楼数（已经标记过删除的不算）。"""
    new_floors = {f["pid"]: f for f in new["floors"]}
    n_f = n_c = 0
    for f in old["floors"]:
        if f.get("missing_since"):
            continue
        nf = new_floors.get(f["pid"])
        if nf is None:
            n_f += 1
            continue
        if nf.get("comments_incomplete"):  # 这一层的楼中楼这次没取全，不算少
            continue
        now_c = {c["pid"] for c in nf["comments"]}
        n_c += sum(1 for c in f["comments"] if not c.get("missing_since") and c["pid"] not in now_c)
    return n_f, n_c


def union_fetch(first: dict, second: dict) -> dict:
    """两次抓取的并集：只要有一次看到，就不算删除（以第二次的内容为准）。"""
    floors = {f["pid"]: f for f in second["floors"]}
    for f in first["floors"]:
        g = floors.get(f["pid"])
        if g is None:
            floors[f["pid"]] = f
            continue
        if not f.get("comments_incomplete"):  # 有一次取全了就算全
            g.pop("comments_incomplete", None)
        have = {c["pid"] for c in g["comments"]}
        g["comments"] = sorted(g["comments"] + [c for c in f["comments"] if c["pid"] not in have],
                               key=lambda c: (c["create_time"], c["pid"]))
    return {**second, "floors": sorted(floors.values(), key=lambda f: (f["floor"], f["pid"]))}


async def archive(tid: int, out: Path, bduss: str = "", no_images: bool = False,
                  deadline: float | None = None, max_bytes: int | None = None, stats: dict | None = None) -> dict:
    """抓取帖子并保存到 out 文件夹（和已有存档合并，只增不减），返回数据。
    原帖整个看不到了：有存档就原样保留，只记下 thread_gone_since（返回的数据里带着它）；没有存档就报错。
    stats 不为空时，写入 images_pending：是否因为体积上限还有图片没下完。"""
    old = load_existing(out, tid)

    def keep_gone() -> dict:
        """原帖整个看不到了：有存档就原样保留并标注；没有存档就报错。"""
        if old is None:
            if bduss:
                raise ThreadGone(NOT_FOUND_LOGGED_IN)
            raise ThreadGone(NOT_FOUND_HINT)
        if old.get("needs_login") and not bduss:  # 没登录当然看不到，不能据此认定原帖没了
            raise NeedsLogin(NEEDS_LOGIN_HINT)
        if not old.get("thread_gone_since"):
            old["thread_gone_since"] = ts(int(time.time()))
        print(f"原帖现在看不到了（被删除或隐藏，{old['thread_gone_since'][:16]} 起），已有的存档原样保留。")
        save_archive(old, out)
        return old

    try:
        data = await fetch_thread(tid, bduss)
        if old:
            n_f, n_c = vanished(old, data)
            if n_f >= RECHECK_FLOORS or n_c >= RECHECK_COMMENTS:
                # 贴吧限流时接口不一定报错，只是少给内容：再抓一次，两次都没看到的才算删除
                print(f"这次比上次少了 {n_f} 层、{n_c} 条楼中楼（可能是贴吧限流，也可能真的删了），再抓一次核对……")
                await asyncio.sleep(10)
                data = union_fetch(data, await fetch_thread(tid, bduss))
    except ThreadGone:
        return keep_gone()

    n_comments = sum(len(f["comments"]) for f in data["floors"])
    print(f"共 {len(data['floors'])} 层，楼中楼 {n_comments} 条")
    if bduss:
        # 登录着存的帖子不一定非要登录才能看：再用不登录的方式看一眼，真的看不到才记下来。
        # 以后不登录更新时，据此提示登录，而不是误以为原帖被删了。（只记这一个标记，不含任何账号信息）
        visible = await visible_without_login(tid)
        if visible is False or (visible is None and (old is None or old.get("needs_login"))):
            data["needs_login"] = True
    if old:
        if old.get("thread_gone_since"):
            print("原帖又能看到了。")
        data = merge_archive(old, data)
        now = data["fetched_at"]
        gone_f = sum(f.get("missing_since") == now for f in data["floors"])
        gone_c = sum(c.get("missing_since") == now for f in data["floors"] for c in f["comments"])
        if gone_f or gone_c:
            print(f"有 {gone_f} 层、{gone_c} 条楼中楼这次已经看不到了（被删除或隐藏），已保留之前存下的内容")
    for f in data["floors"]:
        f.pop("comments_incomplete", None)

    # 已经下载过的图片先对上本地文件，再存一份文字：图片下载中途出问题也不丢；--no-images 时也不丢掉已有的图
    await download_images(data, out / "images", fetch=False)
    save_archive(data, out)
    pending = False
    if not no_images:
        pending = await download_images(data, out / "images", deadline, max_bytes)
        save_archive(data, out)
    if stats is not None:
        stats["images_pending"] = pending
    return data


async def download_more_images(tid: int, out: Path, deadline: float | None = None,
                               max_bytes: int | None = None) -> bool:
    """不重新抓文字，只接着下载还没下完的图片（用存档里的图片链接）。返回是否还有图片没下完。"""
    data = load_existing(out, tid)
    if data is None:
        raise ArchiveError(f"「{out}」里还没有这个帖子的存档。")
    pending = await download_images(data, out / "images", deadline, max_bytes)
    save_archive(data, out)
    return pending


def find_archives(home: Path) -> list:
    """文件夹里已存的帖子（tieba_<ID>，不含 PDF 文件夹）：[(帖子 ID, 文件夹, 数据)]。"""
    found = []
    for d in sorted(home.glob("tieba_*")):
        m = re.fullmatch(r"tieba_(\d+)", d.name)
        if not m:
            continue
        try:
            data = load_existing(d, int(m.group(1)))
        except ArchiveError as e:
            print(f"跳过：{e}\n")
            continue
        if data:
            found.append((int(m.group(1)), d, data))
    return found


async def update_all(archives: list, bduss: str = "", no_images: bool = False,
                     deadline: float | None = None) -> dict:
    """逐个更新已存的帖子。
    返回 {"failed": 出错的标题, "need_login": 不登录看不到、这次没更新的存档,
          "newly_gone": 这次才发现原帖看不到了的存档}（原帖看不到了不算出错）。"""
    ok, gone, need_login, failed, newly_gone = 0, [], [], [], []
    for i, (tid, folder, old) in enumerate(archives, 1):
        title = old.get("title") or str(tid)
        print(f"\n[{i}/{len(archives)}] {title}")
        try:
            data = await archive(tid, folder, bduss, no_images, deadline)
        except NeedsLogin as e:
            print(e)
            need_login.append((tid, folder, old))
            continue
        except ArchiveError as e:
            print(e)
            failed.append(title)
            continue
        except Exception as e:  # 一个帖子出错不影响后面的
            print(f"出错了，可能是网络问题（技术信息：{type(e).__name__}: {e}）")
            failed.append(title)
            continue
        if data.get("thread_gone_since"):
            gone.append(title)
            if not old.get("thread_gone_since"):
                newly_gone.append((tid, folder, old))
        else:
            ok += 1
            added = len(data["floors"]) - len(old["floors"])
            if added:
                print(f"新增 {added} 层")

    parts = [f"{ok} 个已更新"]
    if gone:
        parts.append(f"{len(gone)} 个原帖已经看不到了（存档原样保留）")
    if need_login:
        parts.append(f"{len(need_login)} 个要登录才能看、这次没有更新")
    if failed:
        parts.append(f"{len(failed)} 个出错：" + "、".join(failed))
    print("\n全部处理完：" + "，".join(parts) + "。")
    return {"failed": failed, "need_login": need_login, "newly_gone": newly_gone}


# ---------------------------------------------------------------- 命令行模式


def cli():
    ap = argparse.ArgumentParser(description="抓取贴吧帖子。不带任何参数运行则进入问答模式。")
    ap.add_argument("thread", nargs="?", help="帖子 ID 或完整链接")
    ap.add_argument("--update-all", nargs="?", const="", metavar="存档文件夹",
                    help="更新文件夹里已存的全部帖子（不写文件夹就是程序旁边的「贴吧存档」）")
    ap.add_argument("--no-images", action="store_true", help="只保存文字")
    ap.add_argument("--images-only", action="store_true", help="不重新抓文字，只接着下载还没下完的图片")
    ap.add_argument("--login", action="store_true", help="用自己的账号抓取（会提示输入 BDUSS）")
    ap.add_argument("-o", "--out", type=Path)
    args = ap.parse_args()

    tid = None
    if args.update_all is None:
        tid = parse_tid(args.thread or "")
        if not tid:
            ap.error(f"看不出帖子 ID: {args.thread}" if args.thread else "请给出帖子链接或 ID，或者用 --update-all")
    # 自动运行时（GitHub Actions）用环境变量限制图片下载：截止时间、这次最多下载多少 MB（GitHub 单次上传有上限），
    # 剩下的下次再下
    deadline = float(os.environ["TIEBA_DEADLINE"]) if os.environ.get("TIEBA_DEADLINE") else None
    max_bytes = int(float(os.environ["TIEBA_MAX_MB"]) * 2**20) if os.environ.get("TIEBA_MAX_MB") else None
    try:
        raw = os.environ.get("TIEBA_BDUSS", "")
        if args.login and not raw:
            raw = getpass.getpass("粘贴 BDUSS 后回车（输入时不显示）: ")
        bduss = clean_bduss(raw)
        if tid is None:
            home = Path(args.update_all) if args.update_all else archive_home()
            archives = find_archives(home)
            if not archives:
                raise ArchiveError(f"「{home}」里没有找到已存的帖子。")
            result = asyncio.run(update_all(archives, bduss, args.no_images, deadline))
            if result["need_login"]:
                print("要登录才能看的帖子，请加上 --login 再运行一次。")
            sys.exit(1 if result["failed"] or result["need_login"] else 0)
        out = args.out or Path(f"tieba_{tid}")
        if args.images_only:
            pending = asyncio.run(download_more_images(tid, out, deadline, max_bytes))
            sys.exit(EXIT_MORE_IMAGES if pending else 0)
        stats = {}
        data = asyncio.run(archive(tid, out, bduss, args.no_images, deadline, max_bytes, stats))
    except ArchiveError as e:
        print(f"\n{e}", file=sys.stderr)
        if isinstance(e, (ThreadGone, NeedsLogin)) and not args.login:
            print("如果需要登录，请加上 --login 再运行一次。", file=sys.stderr)
        sys.exit(EXIT_NOT_FOUND if isinstance(e, ThreadGone) else 1)
    except KeyboardInterrupt:
        print("\n已取消。", file=sys.stderr)
        sys.exit(130)
    if data.get("thread_gone_since"):
        sys.exit(EXIT_GONE)
    print(f"已保存到 {out}/")
    if stats.get("images_pending"):
        sys.exit(EXIT_MORE_IMAGES)


# ---------------------------------------------------------------- 问答模式

BDUSS_GUIDE = """
BDUSS 的找法（在电脑上操作）：
  1. 用浏览器登录 tieba.baidu.com
  2. 按 F12，点「应用程序」（Application）→ 左边的 Cookie → tieba.baidu.com
  3. 找到 BDUSS 那一行，复制「值」
注意：BDUSS 相当于你的账号密码，不要发给任何人。本程序只在这台电脑上使用它，不会保存。
"""


def ask_yes_no(question: str, default: bool) -> bool:
    hint = "[Y/n]" if default else "[y/N]"
    while True:
        ans = input(f"{question} {hint}：").strip().lower()
        if not ans:
            return default
        if ans in ("y", "yes", "是", "要", "好", "需要"):
            return True
        if ans in ("n", "no", "否", "不", "不要", "不用"):
            return False
        print("请输入 y 或 n（直接回车表示默认）。")


def archive_home() -> Path:
    """存档放在程序所在文件夹的「贴吧存档」里；那里写不了就放到用户主目录。"""
    base = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
    for home in (base / "贴吧存档", Path.home() / "贴吧存档"):
        try:
            home.mkdir(parents=True, exist_ok=True)
            probe = home / ".write_test"
            probe.write_text("")
            probe.unlink()
            return home
        except OSError:
            continue
    raise ArchiveError("找不到可以保存存档的文件夹。")


def ask_bduss() -> str:
    print(BDUSS_GUIDE)
    while True:
        try:
            bduss = clean_bduss(getpass.getpass("粘贴 BDUSS 后回车（屏幕上不会显示，这是正常的）："))
            if bduss:
                return bduss
            print("没有收到内容，请再粘贴一次。不想继续的话，直接关掉窗口就行。")
        except ArchiveError as e:
            print(e)


def open_page(page: Path):
    if sys.platform == "win32":
        os.startfile(page)  # 路径里有中文时，交给系统用默认浏览器打开最稳
    else:
        webbrowser.open(page.as_uri())


def interactive():
    print("贴吧存档工具：把帖子的每一层、楼中楼和原图都保存到这台电脑上。\n")
    if sys.version_info < (3, 10):
        print("（提示：你在用较旧的 Python 3.9，现在可以用；如果以后报错，请到 python.org 安装新版 Python。）\n")

    home = archive_home()
    existing = find_archives(home)
    prompt = "请粘贴帖子链接（直接回车退出）："
    if existing:
        print(f"已经存过 {len(existing)} 个帖子（在 {home}）。")
        prompt = "请粘贴新帖子的链接，或者输入 1 更新已存的全部帖子（直接回车退出）："
    while True:
        text = input(prompt).strip()
        if not text:
            return
        if existing and text == "1":
            return interactive_update_all(existing)
        tid = parse_tid(text)
        if tid:
            break
        print("看不出这是哪个帖子。请粘贴形如 https://tieba.baidu.com/p/1234567890 的链接。\n")

    def archive_logged_in() -> dict:
        """登录后抓取；BDUSS 不对时让用户重新粘贴，不用从头再来。"""
        nonlocal bduss
        while True:
            bduss = ask_bduss()
            print("\n开始抓取……")
            try:
                return asyncio.run(archive(tid, out, bduss))
            except BadLogin as e:
                print(f"\n{e}")

    bduss = ""
    login_first = ask_yes_no("这个帖子是不是只有你自己看得到（需要登录）？", False)

    out = home / f"tieba_{tid}"
    if (out / "thread.json").exists():
        print("\n这个帖子之前存过，这次会更新（已经下载过的图片不会重复下载）。")
    print("\n帖子越长抓取越久，请耐心等待。")
    if login_first:
        data = archive_logged_in()
    else:
        print("\n开始抓取……")
        try:
            data = asyncio.run(archive(tid, out))
        except (ThreadGone, NeedsLogin) as e:
            print(f"\n{e}")
            if not ask_yes_no("要登录后再试一次吗？", isinstance(e, NeedsLogin)):
                return
            data = archive_logged_in()
    if data.get("thread_gone_since") and not bduss:
        # 已有存档的帖子现在看不到了：可能真被删了，也可能被隐藏成只有发帖人自己看得到
        if ask_yes_no("\n如果这是你自己发的帖子，登录后也许还能看到。要登录后再试一次吗？", False):
            data = archive_logged_in()
    if data.get("thread_gone_since"):
        print(f"\n存档在这里（保留的是原帖还能看到时存下的内容）：\n  {out}")
    else:
        n_images = sum(len(f["images"]) for f in data["floors"])
        print(f"\n完成！「{data['title']}」已保存（{len(data['floors'])} 层，{n_images} 张图）：\n  {out}")

    if ask_yes_no("\n要生成可以直接发给别人的 PDF 吗？（每 100 层一个文件，手机上也能直接打开）", False):
        from topdf import PdfError, make_pdfs  # 用到时才导入：缺了 topdf.py 也不影响抓取
        try:
            pdfs = make_pdfs(out)
            print(f"已生成 {len(pdfs)} 个 PDF，直接发给别人就行：\n  {pdfs[0].parent}")
        except PdfError as e:
            print(f"\n{e}")

    if ask_yes_no("\n现在打开存档看看吗？", True):
        open_page((out / "index.html").resolve())


def interactive_update_all(existing: list):
    print("\n开始更新，帖子越多越久，请耐心等待……")
    result = asyncio.run(update_all(existing))
    retry = result["need_login"] + result["newly_gone"]
    if retry:
        n_login, n_gone = len(result["need_login"]), len(result["newly_gone"])
        why = [f"{n_login} 个要登录才能看" if n_login else "",
               f"{n_gone} 个这次发现看不到了（如果是你自己发的帖子，登录后也许还能看到）" if n_gone else ""]
        question = "\n有 " + "，".join(w for w in why if w) + "。现在登录再试一次吗？"
        if ask_yes_no(question, bool(n_login)):
            bduss = ask_bduss()
            asyncio.run(update_all(retry, bduss))


def run_interactive():
    # aiotieba 自己会往屏幕打印英文技术警告；问答模式下由我们给出中文提示，所以把它关掉
    tb.logging.get_logger().setLevel(logging.CRITICAL)
    try:
        interactive()
    except ArchiveError as e:
        print(f"\n出错了：{e}")
    except KeyboardInterrupt:
        print("\n已取消。")
    except Exception as e:  # 兜底：给普通用户看一句话，技术细节放在后面方便反馈
        print(f"\n出错了，可能是网络问题，稍后再试一次。\n（技术信息：{type(e).__name__}: {e}）")
    finally:
        if getattr(sys, "frozen", False):  # 双击打开的程序：窗口不要一闪而过
            try:
                input("\n按回车键退出……")
            except EOFError:  # 输入不是键盘（比如自动测试时用管道输入）
                pass


if __name__ == "__main__":
    # 输出编码不支持的字符（比如 Windows 上输出被重定向时遇到表情符号）用 ? 代替，不要崩溃
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    if len(sys.argv) > 1:
        cli()
    else:
        run_interactive()
