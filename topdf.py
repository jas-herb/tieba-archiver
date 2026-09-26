"""把存档生成可以直接发给别人的 PDF：每 100 层一个文件，图片缩小，手机上也能直接打开。

在后台调用电脑上已有的 Chrome / Edge（Windows 自带 Edge）把专门排版的网页打印成 PDF，不会打开窗口。
找不到这类浏览器时，改为生成打印用的网页，用浏览器打开后「打印 → 存储为 PDF」即可。

用法:
    python topdf.py tieba_<帖子ID>                  # 生成到旁边的 tieba_<帖子ID>_PDF/
    python topdf.py tieba_<帖子ID> -o 输出文件夹
    python topdf.py tieba_<帖子ID> --page-size A4   # 纸张大小，默认 A5（适合手机阅读）
    python topdf.py tieba_<帖子ID> --drop-deleted   # 去掉已被删除的内容（默认保留并标注「已删除」）
环境变量 TIEBA_BROWSER 可以指定浏览器程序的路径。
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from html import escape
from pathlib import Path

from render import CSS, deleted_summary, floor_html, is_safe_rel, link_or_copy, thread_gone_note

PER_FILE = 100  # 每个 PDF 的楼层数（按楼层号分：1–100 楼、101–200 楼……）
PAGE_SIZE = "A5"  # 默认纸张：A5 在手机上不用放大就能看清；电脑上看或者打印可以用 A4
MAX_IMAGE_PX = 1200  # 图片缩到这个宽度以内（屏幕阅读足够清楚，PDF 小很多）
TALL_RATIO = 1.8  # 高度超过宽度这么多倍的竖图（截图等）是给人看字的：最高可以占满一页，别的图最高半页
SLICE_RATIO = 2.5  # 更长的截图（聊天记录、榜单等）占满一页也会窄得看不清：切成几段，每段和页面一样宽
SLICE_PIECE = 0.5  # 每段最高 = 宽度 × 这个数。段短一些，页面底下剩的地方也能接着放，不会留下大片空白
PRINT_TIMEOUT = 600  # 打印一个 PDF 最多等这么多秒


PRINT_WORDING = (
    ("这一层已被删除或隐藏，点击展开查看保存的内容", "这一层已被删除或隐藏，以下是之前保存的内容"),
    ("的内容后来变了，点开查看之前的版本", "的内容后来变了，之前的版本如下"),
)


class PdfError(Exception):
    """给用户看的错误：直接显示这句话。"""


# ---------------------------------------------------------------- 找浏览器


def find_browser() -> str | None:
    """找电脑上 Chromium 内核的浏览器（Chrome、Edge 等）：它们都能在后台把网页打印成 PDF。"""
    if os.environ.get("TIEBA_BROWSER"):
        return os.environ["TIEBA_BROWSER"]
    cands = []
    if sys.platform == "win32":
        for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA")):
            if base:
                cands += [Path(base, "Microsoft", "Edge", "Application", "msedge.exe"),
                          Path(base, "Google", "Chrome", "Application", "chrome.exe")]
    elif sys.platform == "darwin":
        for app in ("Google Chrome", "Microsoft Edge", "Chromium", "Brave Browser"):
            for root in (Path("/Applications"), Path.home() / "Applications"):
                cands.append(root / f"{app}.app" / "Contents" / "MacOS" / app)
    for c in cands:
        if c.is_file():
            return str(c)
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "microsoft-edge", "msedge"):
        if found := shutil.which(name):
            return found
    return None


def print_pdf(browser: str, page: Path, pdf: Path, timeout: int = PRINT_TIMEOUT):
    """让浏览器在后台把网页打印成 PDF。用一个临时的空白配置，不碰浏览器里原有的登录和数据。
    有的浏览器（比如 Mac 上的 Chrome）打印完不会自己退出：PDF 写好、大小不再变化就算完成，再把它关掉。"""
    pdf.unlink(missing_ok=True)
    profile = Path(tempfile.mkdtemp(prefix="tieba-pdf-profile-"))
    args = [browser, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
            "--disable-extensions", "--disable-crash-reporter", f"--user-data-dir={profile}",
            "--no-pdf-header-footer", "--print-to-pdf-no-header", f"--print-to-pdf={pdf}", page.as_uri()]
    env = {k: v for k, v in os.environ.items() if k != "TIEBA_BDUSS"}  # 浏览器用不着登录信息
    proc = None
    try:
        try:
            proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
        except OSError as e:
            raise PdfError(f"打不开浏览器（{browser}）：{e}") from None
        last, stable, deadline = -1, 0, time.time() + timeout
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            size = pdf.stat().st_size if pdf.exists() else -1
            stable = stable + 1 if size > 0 and size == last else 0
            if stable >= 4:  # 连续 2 秒大小不变：写完了
                break
            last = size
            time.sleep(0.5)
        else:
            raise PdfError("浏览器生成 PDF 超时了。")
    finally:
        if proc is not None and proc.poll() is None:
            stop(proc)
        for _ in range(5):  # Windows 上浏览器的子进程可能还占着这个文件夹，稍等再删
            shutil.rmtree(profile, ignore_errors=True)
            if not profile.exists():
                break
            time.sleep(1)
    if not pdf.exists() or pdf.stat().st_size == 0:
        raise PdfError("浏览器没有生成 PDF。")


def stop(proc: subprocess.Popen):
    """关掉浏览器。Windows 上连同它开的子进程一起关，免得留在后台。"""
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        proc.terminate()
    try:
        proc.wait(10)
    except subprocess.TimeoutExpired:
        proc.kill()


# ---------------------------------------------------------------- 图片


def slice_rows(img, step: int) -> list[tuple[int, int]]:
    """长图切段的位置 [(上, 下)]：每段最高 step 像素，尽量切在空白的行（颜色一样的一整行）上，
    免得一行字被切成两半、分在两页上。"""
    gray = img.convert("L").resize((min(img.width, 200), img.height))  # 只看每行颜色是否一致：缩窄了算得快
    def blank(y: int) -> bool:
        lo, hi = gray.crop((0, y, gray.width, y + 1)).getextrema()
        return hi - lo <= 12
    cuts, top = [], 0
    while img.height - top > step:
        cut = next((y for y in range(top + step, top + step // 2, -1) if blank(y)), top + step)
        cuts.append((top, cut))
        top = cut
    return cuts + [(top, img.height)]


def prepare_images(data: dict, src: Path, dst: Path):
    """把页面里用到的本地图片缩小后放到 dst/img/，并改写数据里的路径（只改这份副本）。
    动图取第一帧并做标记；特别长的截图切成几段；没有本地文件的图片不放进 PDF（原链接大多已经失效）。
    没装 Pillow 时直接用原图。"""
    try:
        from PIL import Image, ImageOps
    except ImportError:
        Image = None
    (dst / "img").mkdir(parents=True, exist_ok=True)
    done: dict[str, dict] = {}  # 同一张图（比如正文和旧版本里都有）只处理一次

    def convert(path: Path) -> dict:
        """缩小、转正、切段，返回要写进图片数据的字段。"""
        name = path.stem
        if Image is None:
            link_or_copy(path, dst / "img" / path.name)
            return {"local": f"img/{path.name}"}
        try:
            with Image.open(path) as img:
                info = {}
                if getattr(img, "is_animated", False):
                    info["print_animated"] = True
                    img.seek(0)
                img = ImageOps.exif_transpose(img).convert("RGBA")  # 手机照片按拍摄时的方向转正
                if img.width > MAX_IMAGE_PX:
                    img = img.resize((MAX_IMAGE_PX, round(img.height * MAX_IMAGE_PX / img.width)), Image.LANCZOS)
                page = Image.new("RGB", img.size, "white")  # 透明的部分铺成白色
                page.paste(img, mask=img.getchannel("A"))
                save = {"format": "JPEG", "quality": 80, "optimize": True, "progressive": True}
                if page.height > SLICE_RATIO * page.width:
                    parts = []
                    for i, (top, bottom) in enumerate(slice_rows(page, round(page.width * SLICE_PIECE)), 1):
                        piece = f"{name}_{i}.jpg"
                        page.crop((0, top, page.width, bottom)).save(dst / "img" / piece, **save)
                        parts.append(f"img/{piece}")
                    return {**info, "local": parts[0], "print_parts": parts}
                page.save(dst / "img" / f"{name}.jpg", **save)
                if page.height > TALL_RATIO * page.width:
                    info["print_tall"] = True
                return {**info, "local": f"img/{name}.jpg"}
        except Exception:  # 个别图片读不了：用原图
            link_or_copy(path, dst / "img" / path.name)
            return {"local": f"img/{path.name}"}

    def one(im: dict):
        rel = im.get("local")
        im["origin_src"] = ""  # 生成 PDF 时不联网取图
        if not rel or not is_safe_rel(rel) or not (src / rel).is_file():
            im.pop("local", None)
            return
        if rel not in done:
            done[rel] = convert(src / rel)
        im.update(done[rel])
        w, h = im.get("width"), im.get("height")
        if Image is None and w and h and h > TALL_RATIO * w:
            im["print_tall"] = True
        if not (w and h) or im.get("print_parts"):  # 切段后原图的宽高不再适用
            im.pop("width", None), im.pop("height", None)

    for f in data["floors"]:
        for item in (f, *f["comments"], *f.get("earlier_versions", [])):
            for im in item.get("images", []):
                one(im)
        for c in f["comments"]:
            for v in c.get("earlier_versions", []):
                for im in v.get("images", []):
                    one(im)


# ---------------------------------------------------------------- 排版


def print_css(page_size: str) -> str:
    # 图片最高约半页：竖长的截图跟着变窄，一页通常能放下两张，也不会为了塞一张大图留下大半页空白
    max_img = {"A5": "90mm", "A4": "130mm"}.get(page_size.upper(), "90mm")
    max_tall = {"A5": "178mm", "A4": "265mm"}.get(page_size.upper(), "178mm")
    return CSS + f"""
:root{{--bg:#fff;--card:#fff;--text:#1f2328;--muted:#6b7280;--line:#e5e7eb;--accent:#3b6fd8;--accent-soft:#e8effc;
--sub:#f5f6f7;--emo-bg:#fff4d6;--emo:#8a5a00;--del:#b42318;--del-bg:#fdecea;color-scheme:light}}
@page{{size:{page_size};margin:10mm 9mm}}
html,body{{background:#fff}}
body{{-webkit-print-color-adjust:exact;print-color-adjust:exact;line-height:1.6}}
.wrap{{max-width:none;padding:0}}
.top{{padding:0 0 10px}}h1{{font-size:20px}}
.floor{{break-inside:auto;border-radius:8px;padding:10px 12px;margin:0 0 8px}}
.floor>header{{break-inside:avoid;break-after:avoid}}
.pic,.sub,.ver{{break-inside:avoid}}
.pic{{text-align:center}}.pic img{{display:inline-block;width:auto;height:auto;max-width:100%;max-height:{max_img}}}
.pic.tall img{{max-height:{max_tall}}}
.pic.long{{display:block;break-inside:auto}}.pic.long img{{display:block;width:100%;max-height:none;border-radius:0;break-inside:avoid}}
.gif{{display:block;font-size:11px;color:var(--muted)}}
.sub.more{{display:flex}}.unfold{{display:none}}
.body{{font-size:14px}}.sub{{font-size:13px}}
"""


def page_html(data: dict, floors: list, label: str, page_size: str) -> str:
    author = data["author"]
    notes = [n for n in (thread_gone_note(data), deleted_summary(data)) if n]
    body = "".join(floor_html(f) for f in floors)
    body = body.replace("<details", "<details open").replace(' loading="lazy"', "")  # 收起的内容全部展开，图片直接加载
    # 网页里图片外面包着「点开看大图」的链接；PDF 里不要：它指向生成时的临时文件，会把电脑上的路径（含用户名）带进 PDF
    body = re.sub(r'<a class="(pic[^"]*)" href="[^"]*" target="_blank">(.*?)</a>', r'<span class="\1">\2</span>', body)
    for tap, plain in PRINT_WORDING:  # 网页上「点击展开」之类的提示，在 PDF 里换个说法
        body = body.replace(tap, plain)
    n_comments = sum(len(f["comments"]) for f in floors)
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>{escape(data['title'])}（{label}）</title>
<style>{print_css(page_size)}</style>
</head>
<body>
<div class="wrap">
<header class="top">
<div class="forum">{escape(data['forum'])}吧</div>
<h1>{escape(data['title'])}</h1>
<p class="facts">{label}（本文件 {len(floors)} 层、{n_comments} 条楼中楼）· 楼主 {escape(author['show_name'])} · 发帖于 {escape(data['create_time'][:16])}</p>
<p class="facts">原帖 {escape(data['url'])} · 存档于 {escape(data['fetched_at'][:16])} · 时间均为北京时间</p>
{"".join(f'<p class="facts gone-note">{escape(n)}</p>' for n in notes)}
</header>
<main>{body}</main>
</div>
</body>
</html>
"""


def safe_filename(title: str, fallback: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\s]+', " ", title).strip(" .")
    return name[:40].rstrip(" .") or fallback


def make_pdfs(folder: Path, out_dir: Path | None = None, page_size: str = PAGE_SIZE,
              drop_deleted: bool = False, log=print) -> list[Path]:
    """生成 PDF，返回生成的文件列表。找不到浏览器时生成打印用的网页，并抛出 PdfError 说明怎么手动存成 PDF。"""
    data = json.loads((folder / "thread.json").read_text(encoding="utf-8"))
    if drop_deleted:
        data["floors"] = [f for f in data["floors"] if not f.get("missing_since")]
        for f in data["floors"]:
            f["comments"] = [c for c in f["comments"] if not c.get("missing_since")]
            for item in (f, *f["comments"]):
                item.pop("earlier_versions", None)
    folder = folder.resolve()  # 「topdf.py .」这种写法也要能算出旁边的文件夹
    out_dir = out_dir or folder.with_name(folder.name + "_PDF")
    groups: dict[int, list] = {}
    for f in data["floors"]:
        groups.setdefault((max(f["floor"], 1) - 1) // PER_FILE, []).append(f)
    if not groups:
        raise PdfError("这个存档里没有楼层。")
    stem = safe_filename(data["title"], f"tieba_{data.get('tid', '')}")
    browser = find_browser()

    tmp = Path(tempfile.mkdtemp(prefix="tieba-pdf-"))
    try:
        return build(data, folder, out_dir, stem, groups, page_size, browser, tmp, log)
    except OSError as e:
        raise PdfError(f"读写文件出错了：{e}\n如果之前生成的 PDF 正开着，请先关掉再试。") from None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def build(data: dict, folder: Path, out_dir: Path, stem: str, groups: dict, page_size: str,
          browser: str | None, tmp: Path, log) -> list[Path]:
    """在临时文件夹 tmp 里排版、打印，把 PDF 移到 out_dir。"""
    work = copy.deepcopy(data)
    log("正在准备图片……")
    prepare_images(work, folder, tmp)
    pages = []
    for k in sorted(groups):
        lo, hi = k * PER_FILE + 1, (k + 1) * PER_FILE
        label = f"{lo}–{hi} 楼"
        floors = [f for f in work["floors"] if (max(f["floor"], 1) - 1) // PER_FILE == k]
        page = tmp / f"print_{lo}.html"
        page.write_text(page_html(work, floors, label, page_size), encoding="utf-8")
        pages.append((page, f"{stem}_{lo}-{hi}楼"))

    out_dir.mkdir(parents=True, exist_ok=True)
    if browser is None:
        manual = out_dir / "打印用网页"
        shutil.rmtree(manual, ignore_errors=True)
        shutil.copytree(tmp, manual)
        for page, name in pages:
            page_in = manual / page.name
            page_in.rename(manual / f"{name}.html")
        raise PdfError(
            "没有找到 Chrome 或 Edge 浏览器，没法自动生成 PDF。\n"
            f"已经把排好版的网页放在：{manual}\n"
            "用浏览器打开其中每个 .html 文件，选择「打印 → 存储为 PDF」即可。\n"
            "打印设置里请取消勾选「页眉和页脚」，否则会把电脑上的文件路径印进 PDF。")

    done = []
    for i, (page, name) in enumerate(pages, 1):
        log(f"正在生成 PDF {i}/{len(pages)}：{name}.pdf")
        tmp_pdf = tmp / f"out_{i}.pdf"  # 先存到临时文件夹（路径简单），再移过去
        print_pdf(browser, page, tmp_pdf)
        target = out_dir / f"{name}.pdf"
        shutil.move(str(tmp_pdf), target)
        done.append(target)
    # 全部生成好以后，再删掉上次生成、这次没有的分册。只删本工具起名的文件，别的文件不碰；
    # 默认的 _PDF 文件夹只放这个帖子的 PDF，帖子改了标题时旧标题的分册也删掉
    own_dir = out_dir == folder.with_name(folder.name + "_PDF")
    mine = re.compile((".+" if own_dir else re.escape(stem)) + r"_\d+-\d+楼\.pdf")
    for old in out_dir.glob("*.pdf"):
        if mine.fullmatch(old.name) and old not in done:
            old.unlink()
    return done


def main():
    ap = argparse.ArgumentParser(description="把存档生成可以直接发给别人的 PDF（每 100 层一个文件）")
    ap.add_argument("folder", type=Path, help="包含 thread.json 的存档文件夹")
    ap.add_argument("-o", "--out", type=Path, help="输出文件夹（默认是存档旁边的 <文件夹名>_PDF）")
    ap.add_argument("--page-size", default=PAGE_SIZE, help=f"纸张大小，比如 A4、A5（默认 {PAGE_SIZE}）")
    ap.add_argument("--drop-deleted", action="store_true", help="去掉已被删除的内容（默认保留并标注「已删除」）")
    args = ap.parse_args()
    try:
        pdfs = make_pdfs(args.folder, args.out, args.page_size, args.drop_deleted)
    except PdfError as e:
        sys.exit(str(e))
    total = sum(p.stat().st_size for p in pdfs) / 2**20
    print(f"已生成 {len(pdfs)} 个 PDF（共 {total:.1f} MB）：{pdfs[0].parent}")


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    main()
