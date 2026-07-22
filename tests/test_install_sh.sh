#!/usr/bin/env bash
# install.sh 里那几段「错了就很惨」的逻辑的回归测试。
# 装机流程本身要 root + systemd，没法在这儿跑；这里只钉住纯函数部分。
#
#   bash tests/test_install_sh.sh

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT="$(dirname "${HERE}")"
PASS=0
FAIL=0

check() {  # check 说明 实际 期望
  if [[ "$2" == "$3" ]]; then
    printf ' ✓ %s\n' "$1"; PASS=$((PASS + 1))
  else
    printf ' ✗ %s\n   期望: %s\n   实际: %s\n' "$1" "$3" "$2"; FAIL=$((FAIL + 1))
  fi
}

# shellcheck source=/dev/null
source "${PROJECT}/install.sh"

SANDBOX="$(mktemp -d)"
trap 'rm -rf "${SANDBOX}"' EXIT

# ---- env_get / env_set 往返 ----
CONF_DIR="${SANDBOX}/etc"
ENV_FILE="${CONF_DIR}/worker.env"

check "键不存在时返回默认值" "$(env_get NOPE fallback)" "fallback"

env_set TRANSLATE_WORKER_PORT 8791
check "写入后读得回来" "$(env_get TRANSLATE_WORKER_PORT)" "8791"

env_set TRANSLATE_WORKER_PORT 9000
check "改写不留旧值" "$(env_get TRANSLATE_WORKER_PORT)" "9000"
check "改写不会写成两行" "$(grep -c '^TRANSLATE_WORKER_PORT=' "${ENV_FILE}")" "1"

# base64 token 里会有 = 号，别被 ${line#*=} 截断
env_set TRANSLATE_WORKER_TOKEN "abc==def=="
check "值里带 = 也能完整读回" "$(env_get TRANSLATE_WORKER_TOKEN)" "abc==def=="

env_set OTHER_KEY keep-me
env_set TRANSLATE_WORKER_PORT 7000
check "改一个键不会伤到别的键" "$(env_get OTHER_KEY)" "keep-me"

# ---- token 生成 ----
TOKEN="$(gen_token)"
check "生成的 token 长度达标（≥32）" "$([[ ${#TOKEN} -ge 32 ]] && echo yes || echo no)" "yes"
check "两次生成不重样" "$([[ "$(gen_token)" != "${TOKEN}" ]] && echo yes || echo no)" "yes"

# ---- 软链解析：菜单入口 /usr/local/bin/pdf2zh-worker → APP_DIR/install.sh ----
# 走真实代码路径：usage 里打的就是 SCRIPT_PATH，从软链调起来必须显示真身，
# 否则 upgrade 会跑到 /usr/local/bin 去找源码然后死掉。
mkdir -p "${SANDBOX}/opt/app" "${SANDBOX}/bin"
cp "${PROJECT}/install.sh" "${SANDBOX}/opt/app/install.sh"
chmod +x "${SANDBOX}/opt/app/install.sh"
ln -sf "${SANDBOX}/opt/app/install.sh" "${SANDBOX}/bin/pdf2zh-worker"
RESOLVED="$("${SANDBOX}/bin/pdf2zh-worker" --help | grep -m1 -o "${SANDBOX}[^ ]*install.sh")"
check "从软链调起来能解回真身" "${RESOLVED}" "${SANDBOX}/opt/app/install.sh"

# ---- sync_code 的自删防线：源和目标同一个目录时必须跳过拷贝 ----
APP_DIR="${SANDBOX}/opt/app"
SRC_DIR="${SANDBOX}/opt/app"
CLI_LINK="${SANDBOX}/bin/pdf2zh-worker"
mkdir -p "${APP_DIR}/pdf2zh_worker"
echo "x" >"${APP_DIR}/pdf2zh_worker/__init__.py"
sync_code >/dev/null 2>&1 || true
check "就地运行时源码没被删掉" \
  "$([[ -f "${APP_DIR}/pdf2zh_worker/__init__.py" ]] && echo yes || echo no)" "yes"

# ---- 真正的同步路径：从项目目录拷到一个新 APP_DIR ----
APP_DIR="${SANDBOX}/opt/fresh"
SRC_DIR="${PROJECT}"
CLI_LINK="${SANDBOX}/bin/pdf2zh-worker2"
sync_code >/dev/null 2>&1
check "正常同步拷进了包" \
  "$([[ -f "${APP_DIR}/pdf2zh_worker/app.py" ]] && echo yes || echo no)" "yes"
check "正常同步拷进了 pyproject" \
  "$([[ -f "${APP_DIR}/pyproject.toml" ]] && echo yes || echo no)" "yes"
check "建好了菜单入口软链" \
  "$([[ -L "${CLI_LINK}" ]] && echo yes || echo no)" "yes"

echo
if [[ ${FAIL} -eq 0 ]]; then
  printf '%s\n' "install.sh：${PASS} 项全过"
else
  printf '%s\n' "install.sh：${PASS} 过 / ${FAIL} 挂"
  exit 1
fi
