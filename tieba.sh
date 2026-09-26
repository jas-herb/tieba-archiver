#!/usr/bin/env bash
# 贴吧存档启动脚本（Mac / Linux）
#
# 第一次运行会自动准备好运行环境（放在本文件夹的 .venv 里，不影响电脑上的其他东西），之后直接启动。
#   bash tieba.sh                  问答模式，一步步提示
#   bash tieba.sh <链接> [参数]     和 tieba_dump.py 的命令行参数相同
set -euo pipefail
# 工具所在的文件夹。不切换当前目录：参数里的相对路径（比如 -o、--update-all 后面的文件夹）照常按当前目录算
DIR="$(cd "$(dirname "$0")" && pwd)"

VENV="$DIR/.venv"
STAMP="$VENV/.requirements.sha256"
REQ="$DIR/requirements.txt"
MIRROR="https://pypi.tuna.tsinghua.edu.cn/simple" # 默认源下载失败时改用清华镜像

say() { printf '%s\n' "$*" >&2; }

# at_least / below <python> <版本>：该 python 的版本是否 ≥ / < 给定版本（例如 "3, 10"）
at_least() { "$1" -c "import sys; sys.exit(0 if sys.version_info >= ($2) else 1)" >/dev/null 2>&1; }
below() { "$1" -c "import sys; sys.exit(0 if sys.version_info < ($2) else 1)" >/dev/null 2>&1; }
version_of() { "$1" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null; }

# 找 Python：依赖的 aiotieba 目前只提供到 3.14 的安装包，所以优先 3.10–3.14，其次 3.9；
# 只有更新的版本（比如 3.15）时也试一下，但会提醒
find_python() {
  local cand fallback="" newer=""
  for cand in python3.14 python3.13 python3.12 python3.11 python3.10 python3 python python3.9; do
    command -v "$cand" >/dev/null 2>&1 || continue
    at_least "$cand" "3, 9" || continue
    if ! below "$cand" "3, 15"; then
      [ -n "$newer" ] || newer="$cand"
      continue
    fi
    if at_least "$cand" "3, 10"; then echo "$cand"; return 0; fi
    [ -n "$fallback" ] || fallback="$cand"
  done
  if [ -n "$fallback" ]; then echo "$fallback"; elif [ -n "$newer" ]; then echo "$newer"; fi
  return 0 # 找不到时输出为空，由调用方提示（不能返回非 0，否则 set -e 会让脚本直接退出）
}

PY="$(find_python)"
if [ -z "$PY" ]; then
  say "没有找到可用的 Python（需要 3.9 或更新的版本）。"
  if [ "$(uname)" = "Darwin" ]; then
    say "如果刚才弹出了安装「命令行开发者工具」的窗口，点「安装」，装好后重新运行这个命令即可。"
    say "也可以到 https://www.python.org/downloads/ 下载安装 Python 3.14。"
  else
    say "请用系统的包管理器安装 python3，例如：sudo apt install python3 python3-venv"
  fi
  exit 1
fi
too_new=0
below "$PY" "3, 15" || too_new=1

# 需要（重新）创建运行环境：还没有、已经损坏，现在有了更新的 Python（原来是 3.9），
# 或者原来用的是太新、还不支持的 Python（现在有了支持的版本）
rebuild=0
if ! [ -x "$VENV/bin/python" ] || ! "$VENV/bin/python" -c "import sys" >/dev/null 2>&1; then
  rebuild=1
elif ! at_least "$VENV/bin/python" "3, 10" && at_least "$PY" "3, 10" && [ "$too_new" = 0 ]; then
  say "发现了更新的 Python $(version_of "$PY")，换用它重新准备运行环境……"
  rebuild=1
elif ! below "$VENV/bin/python" "3, 15" && [ "$too_new" = 0 ]; then
  say "换用支持的 Python $(version_of "$PY") 重新准备运行环境……"
  rebuild=1
fi

# 重建失败时恢复原来的运行环境：不要先把能用的删掉再说。
# 原来没有的，就把这次没建好的删掉，免得下次换了 Python 还接着用这个坏的
restore() {
  if [ -d "$VENV.old" ]; then
    rm -rf "$VENV"
    mv "$VENV.old" "$VENV"
  elif [ "$rebuild" = 1 ]; then
    rm -rf "$VENV"
  fi
}
fail() {
  say "$1"
  [ "$too_new" = 0 ] || say "你的 Python 是 $(version_of "$PY")，比工具目前支持的版本（3.9–3.14）新，依赖可能还装不上。请安装 Python 3.14 后再试。"
  restore
  exit 1
}

if [ "$rebuild" = 1 ]; then
  rm -rf "$VENV.old"
  if [ -d "$VENV" ]; then mv "$VENV" "$VENV.old"; fi
  say "正在准备运行环境（Python $(version_of "$PY")），第一次大约需要一两分钟……"
  "$PY" -m venv "$VENV" >/dev/null 2>&1 || fail "创建运行环境失败。Linux 上可能需要先安装：sudo apt install python3-venv"
  "$VENV/bin/python" -m pip install -q --disable-pip-version-check --upgrade pip >/dev/null 2>&1 || true
fi

# 依赖：第一次安装，或者 requirements.txt 有变化时重新安装
want="$("$VENV/bin/python" -c 'import hashlib, sys; print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())' "$REQ")"
if [ ! -f "$STAMP" ] || [ "$(cat "$STAMP")" != "$want" ]; then
  [ "$rebuild" = 1 ] || say "依赖有更新，正在安装……"
  pip_install() { "$VENV/bin/python" -m pip install -q --disable-pip-version-check --timeout 15 --retries 2 "$@" -r "$REQ"; }
  if ! pip_install; then
    say "下载失败，改用国内镜像重试……"
    pip_install -i "$MIRROR" || fail "安装依赖失败，请检查网络后重试。"
  fi
  echo "$want" > "$STAMP"
  say "运行环境准备好了。"
  say ""
fi
rm -rf "$VENV.old"

exec "$VENV/bin/python" "$DIR/tieba_dump.py" "$@"
