"""网页版（GitHub Actions）存档用：在仓库根目录运行。
存档放在 archives/tieba_<ID>/，要跟踪的帖子列在根目录的 threads.txt，仓库首页的 README.md 是存档列表
（第一次运行时，原来的使用说明 README.md 改名为 USAGE.md 保留）。

抓取完成后：

1. 逐个比较帖子和上一次提交的版本，只看内容：楼层、楼中楼、正文、图片。
   点赞数、抓取时间、图片链接里每次都会变的参数都不算。
2. 内容没变的帖子：恢复上一版的数据（避免抓取时间、点赞数之类的变化产生提交），
   再用当前的 render.py 重新生成 md 和网页。页面格式没变时结果与上一版完全相同；
   格式改了（比如新增分页）则会更新，已有的存档也能用上新格式。
3. 重新生成首页 README.md 的存档列表。「最后内容更新」取上一次内容有变化时的抓取时间。
4. 有东西要提交：打印提交信息，退出码 0；没有：退出码 3；其他退出码表示出错。

用法:
    python archive_update.py [仓库目录]                    # 抓取完成后：还原没变的帖子、更新存档列表、输出提交信息
    python archive_update.py --only <ID> [仓库目录]         # 同上，但只处理这一个帖子（逐个帖子提交时用）
    python archive_update.py --add "<链接或ID…>" [仓库目录]  # 把帖子加进 threads.txt，可以一次填多个（已存在则跳过）
    python archive_update.py --ids [仓库目录]               # 列出 threads.txt 里的帖子 ID（去重，还没存过的排在前面）
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # 仓库根目录，用来导入 render.py
import render  # noqa: E402
from render import md_escape, parse_tids, write_text_atomic  # noqa: E402


NO_CHANGE = 3
DATA = "archives"  # 存档放在仓库里的这个文件夹
GUIDE = "USAGE.md"  # 原来的 README.md（工具的使用说明）改名后的文件
INDEX_MARK = "<!-- 这个文件由「贴吧存档」自动生成，手动修改会被覆盖 -->"


def git(root: Path, *args: str) -> subprocess.CompletedProcess:
    # 不做换行符转换：页面一律用 \n 换行，Windows 上的 git 默认还原时会改成 \r\n，重新生成后就被当成有变化
    return subprocess.run(["git", "-C", str(root), "-c", "core.autocrlf=false", *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def head_json(root: Path, rel: str):
    r = git(root, "show", f"HEAD:{rel}")
    return json.loads(r.stdout) if r.returncode == 0 else None


def n_local(item: dict) -> int:
    """已经下载到本地的图片数。"""
    return sum(1 for im in item.get("images", []) if im.get("local"))


def signature(data: dict):
    """只看内容：正文、图片（以及已经下载了几张）、是否已被删除、保留了几个旧版本。"""
    floors, comments = {}, {}
    for f in data["floors"]:
        floors[f["pid"]] = (f["text"], tuple(im["hash"] for im in f.get("images", [])), n_local(f),
                            bool(f.get("missing_since")), len(f.get("earlier_versions", [])))
        for c in f["comments"]:
            comments[c["pid"]] = (c["text"], n_local(c), bool(c.get("missing_since")),
                                  len(c.get("earlier_versions", [])))
    return floors, comments


def _changes(new: dict, old: dict, unit: str) -> list[str]:
    """签名的最后两项固定是「是否已删除」和「旧版本数」。"""
    def deleted(sig): return sig[-2]
    def versions(sig): return sig[-1]

    added = new.keys() - old.keys()
    gone = [k for k in new if deleted(new[k]) and not (k in old and deleted(old[k]))]
    back = [k for k in new if k in old and deleted(old[k]) and not deleted(new[k])]
    edited = [k for k in new.keys() & old.keys() if versions(new[k]) > versions(old[k])]
    vanished = old.keys() - new.keys()  # 只有没经过合并的旧存档才会出现
    parts = []
    if added:
        parts.append(f"新增 {len(added)} {unit}")
    if gone:
        parts.append(f"{len(gone)} {unit}已被删除（内容已保留）")
    if back:
        parts.append(f"{len(back)} {unit}重新出现")
    if edited:
        parts.append(f"{len(edited)} {unit}内容有变化（旧版本已保留）")
    if vanished:
        parts.append(f"{len(vanished)} {unit}已被删除")
    return parts


def describe(new: dict, old: dict | None) -> str | None:
    nf, nc = signature(new)
    if old is None:
        return f"{new['title']}：首次存档，{len(nf)} 层、{len(nc)} 条楼中楼"
    of, oc = signature(old)
    parts = _changes(nf, of, "层") + _changes(nc, oc, "条楼中楼")
    # 上次没下完（时间不够、网络问题）的图片这次补上了；新楼层的图片不算在这里
    more = sum(nf[k][2] - of[k][2] for k in nf.keys() & of.keys() if nf[k][2] > of[k][2])
    if more:
        parts.append(f"补下载 {more} 张图片")
    was_gone, is_gone = bool(old.get("thread_gone_since")), bool(new.get("thread_gone_since"))
    if is_gone and not was_gone:
        parts.insert(0, "原帖已经看不到了（被删除或隐藏），存档原样保留")
    elif was_gone and not is_gone:
        parts.insert(0, "原帖又能看到了")
    if (new.get("title"), new.get("forum")) != (old.get("title"), old.get("forum")):
        parts.append("帖子标题或所在的吧有变化")
    if not parts and (nf != of or nc != oc):
        parts.append("内容有变化")
    return f"{new['title']}：" + "，".join(parts) if parts else None


def line_ids(line: str) -> list[int]:
    """threads.txt 一行里的帖子 ID（# 后面是注释）。行首是 ID 时就是它，后面的都算备注（比如
    「ID（备注）」「ID：备注」）；否则认这一行里所有的帖子链接。"""
    line = line.split("#", 1)[0]
    if m := re.match(r"\s*(\d{6,})(?!\d)", line):
        return [int(m.group(1))]
    return [int(a or b) for a, b in re.findall(r"/p/(\d+)|[?&]kz=(\d+)", line)]


def ids_in(text: str, warn: bool = False) -> list[str]:
    """threads.txt 内容里的帖子 ID，按出现顺序去重。warn 为真时，对认不出帖子的行给出警告。"""
    ids = []
    for n, line in enumerate(text.splitlines(), 1):
        found = line_ids(line)
        if warn and not found and line.split("#", 1)[0].strip():
            print(f"::warning::threads.txt 第 {n} 行看不出是哪个帖子，已跳过：{line.strip()[:60]}"
                  "（每行写一个帖子链接，或者链接里 /p/ 后面的那串数字）", file=sys.stderr)
        for tid in found:
            if str(tid) not in ids:
                ids.append(str(tid))
    return ids


def listed_ids(root: Path, warn: bool = False) -> list[str]:
    f = root / "threads.txt"
    return ids_in(f.read_text(encoding="utf-8"), warn) if f.exists() else []


def fetch_order(root: Path) -> list[str]:
    """抓取顺序：还没存过的帖子排在最前面（刚加的帖子最怕被删，别让它等旧帖子都抓完），其余按列表顺序。"""
    ids = listed_ids(root, warn=True)
    return sorted(ids, key=lambda t: (root / DATA / f"tieba_{t}" / "thread.json").exists())


def add_threads(root: Path, text: str) -> int:
    tids = [str(t) for t in parse_tids(text)]
    if not tids:
        print(f"::error::看不出帖子 ID：{text}（请填帖子链接，或者链接里 /p/ 后面的那串数字）")
        return 2
    f = root / "threads.txt"
    old = f.read_text(encoding="utf-8") if f.exists() else ""
    if old and not old.endswith("\n"):
        old += "\n"
    listed = ids_in(old)
    new = [t for t in tids if t not in listed]
    for t in tids:
        print(f"已添加 {t}" if t in new else f"{t} 已经在列表里了")
    if new:
        write_text_atomic(f, old + "".join(t + "\n" for t in new))
    return 0


def list_changes(root: Path) -> list[str]:
    """threads.txt 和上一次提交相比，新增、停止跟踪了哪些帖子。"""
    r = git(root, "show", "HEAD:threads.txt")
    before_text = r.stdout if r.returncode == 0 else ""
    f = root / "threads.txt"
    now_text = f.read_text(encoding="utf-8") if f.exists() else ""
    before, now = ids_in(before_text), ids_in(now_text)
    added = [t for t in now if t not in before]
    removed = [t for t in before if t not in now]
    out = [f"添加帖子：{'、'.join(added)}"] if added else []
    return out + ([f"停止跟踪：{'、'.join(removed)}"] if removed else [])


def cell(s) -> str:
    """表格里的文字：转义 Markdown 符号（标题里的 <!--、[ 之类不会弄乱页面），竖线和换行也不能有。"""
    return md_escape(str(s).replace("\n", " "))  # md_escape 也转义了竖线


def schedule_text(root: Path) -> str:
    """从 archive.yml 的 cron 读出每天检查的时间，换算成北京时间（cron 用的是 UTC）。改了定时，首页跟着变。"""
    try:
        wf = (root / ".github" / "workflows" / "archive.yml").read_text(encoding="utf-8")
    except OSError:
        wf = ""
    crons = re.findall(r"cron:\s*[\"']([^\"']+)[\"']", wf)
    if len(crons) == 1 and (m := re.fullmatch(r"(\d{1,2}) (\d{1,2}) \* \* \*", crons[0].strip())):
        minute, hour = int(m.group(1)), (int(m.group(2)) + 8) % 24
        return f"目前设置为每天北京时间 {hour}:{minute:02d} 自动检查一次"
    return "按 `.github/workflows/archive.yml` 中设置的时间自动检查"


def index_head(root: Path) -> str:
    return INDEX_MARK + f"""
# 我的贴吧存档

点击帖子标题即可阅读（长帖每 100 层一页，页首可直接跳到最新楼层）。{schedule_text(root)}，有新回复时自动补存。详细说明见[使用说明](USAGE.md)。

- **网页版（本仓库，推荐）**：适用于公开的、几百层以内的帖子。完全免费，无需安装软件；存档保存在本仓库中，每天自动更新，可直接在网页上阅读。
- **电脑版（Windows / Mac / Linux）**：适用于需要登录才能查看的帖子，或上千层、图片达数 GB 的大帖。存档保存在本地，支持搜索、倒序和只看楼主。下载与使用方法见[使用说明](USAGE.md)。

## 网页版常用操作

- **添加帖子**：点击顶部 **Actions** → 左侧选择 **贴吧存档** → 右侧点击 **Run workflow**，在输入框中粘贴帖子链接（多个链接用空格分隔），再点击绿色的 **Run workflow**。运行完成后刷新本页，新帖子会出现在下方列表中（第一次存档要等几分钟，帖子越长、图片越多越久）。
  - 手机浏览器上：**Actions** 在顶部的 **More** 菜单里；**贴吧存档** 在 **All workflows** 列表里。
- **立即更新**：点击 **Run workflow** 时输入框留空，即可立即检查全部帖子，无需等待每日定时运行。
- **停止跟踪**：打开 [threads.txt](threads.txt)，点击右上角的铅笔图标，删除对应的行后点击 **Commit changes**。已保存的内容会保留，但不再更新。也可以在此文件中直接添加链接，每行一个。
- **下载到电脑**：在本页点击 **Code** → **Download ZIP**，解压后打开 `archives` 中对应帖子的文件夹，双击 `index.html` 即可离线阅读，并可搜索、倒序、只看楼主。
- **保持仓库私有**：存档里是别人发的帖子，请不要公开这个仓库。如需分享给他人，请生成 PDF（见[使用说明](USAGE.md)）。

## 帖子列表

| 帖子 | 贴吧 | 层数 | 楼中楼 | 图片 | 最后内容更新（北京时间） |
|---|---|---|---|---|---|
"""


INDEX_FOOT = "\n注：「最后内容更新」指帖子内容最后一次发生变化的时间；每次检查的运行记录见 **Actions** 页面。\n"
NOT_YET_NOTE = ("「还没存到」：刚发的帖子可能还在审核中（通常半小时内就能看到），也可能链接有误或帖子已被删除。"
                "每次检查时都会再试；确认链接有误的话，从 threads.txt 里删掉那一行即可。\n")
NOT_YET = "（还没存到，每次检查时会再试）"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("root", type=Path, nargs="?", default=Path("."), help="仓库根目录（默认当前目录）")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--add", metavar="链接或ID")
    mode.add_argument("--ids", action="store_true")
    mode.add_argument("--only", metavar="ID")
    args = ap.parse_args()
    if args.add is not None:
        sys.exit(add_threads(args.root, args.add))
    if args.ids:
        print("\n".join(fetch_order(args.root)))
        return
    update(args.root, args.only)


def keep_guide(root: Path):
    """首页 README.md 要换成存档列表：第一次换之前，把原来的使用说明改名为 USAGE.md 留着。"""
    readme, guide = root / "README.md", root / GUIDE
    if readme.exists() and not guide.exists():
        if not readme.read_text(encoding="utf-8").startswith(INDEX_MARK):
            readme.rename(guide)


def update(root: Path, only: str | None = None):
    """only 不为空时只检查这一个帖子（逐个帖子提交时，其他帖子这次没动过），存档列表照样全部重新生成。"""
    listed = listed_ids(root)
    data_dir = root / DATA
    archived = sorted(p.name.removeprefix("tieba_") for p in data_dir.glob("tieba_*") if (p / "thread.json").exists())
    changes, rows = [], []

    for tid in listed + [t for t in archived if t not in listed]:
        folder = f"{DATA}/tieba_{tid}"
        path = root / folder / "thread.json"
        if not path.exists():
            rows.append(f"| {tid}{NOT_YET} | | | | | |")
            continue
        new = json.loads(path.read_text(encoding="utf-8"))
        if only and tid != only:  # 这次没动过的帖子：内容就是上一次提交的，直接用来生成索引
            shown, updated = new, new["fetched_at"][:16]
        elif line := describe(new, old := head_json(root, f"{folder}/thread.json")):
            changes.append(line)
            shown, updated = new, new["fetched_at"][:16]
        else:
            # 内容没变：恢复上一版（抓取时间、点赞数之类的变化不提交），再用当前格式重新生成页面
            git(root, "checkout", "HEAD", "--", folder)
            git(root, "clean", "-fdq", "--", folder)
            render.render_dir(root / folder)
            shown, updated = old, old["fetched_at"][:16]
        floors = shown["floors"]
        title = f"[{cell(shown['title'])}]({folder}/thread.md)"
        if shown.get("thread_gone_since"):
            title += "（原帖已删除或隐藏）"
        if tid not in listed:
            title += "（已停止更新）"
        n_gone = sum(1 for f in floors if f.get("missing_since"))
        n_floors = f"{len(floors)}（已删除 {n_gone}）" if n_gone else str(len(floors))
        rows.append(
            f"| {title} | {cell(shown['forum'])}吧 | {n_floors} | {sum(len(f['comments']) for f in floors)} "
            f"| {sum(len(f.get('images', [])) for f in floors)} | {updated} |"
        )

    keep_guide(root)
    foot = INDEX_FOOT + ("\n" + NOT_YET_NOTE if any(NOT_YET in r for r in rows) else "")
    write_text_atomic(root / "README.md", index_head(root) + "\n".join(rows) + "\n" + foot)  # 一律 \n 换行

    # 只看存档相关的文件（别把别的改动当成存档变化）
    status = git(root, "status", "--porcelain", "--", DATA, "threads.txt", "README.md", GUIDE).stdout
    if not status.strip():
        sys.exit(NO_CHANGE)
    changes = list_changes(root) + changes
    if changes:
        print("存档更新\n\n" + "\n".join(f"- {c}" for c in changes))
    elif any("tieba_" in line for line in status.splitlines()):
        print("更新页面格式")
    else:
        print("更新存档索引")


if __name__ == "__main__":
    main()
