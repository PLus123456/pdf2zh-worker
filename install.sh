#!/usr/bin/env bash
# pdf2zh-worker 一键安装 / 运维脚本
#
#   sudo ./install.sh              打开菜单
#   sudo ./install.sh install      无人值守安装
#   sudo ./install.sh upgrade      升级 wrapper + pdf2zh-next
#   sudo ./install.sh uninstall    卸载（加 --purge 连数据一起删）
#   sudo ./install.sh status|logs|restart|doctor|token|warmup|config
#
# 装完之后直接敲 `pdf2zh-worker` 就能再调出这个菜单。

set -Eeuo pipefail

APP_NAME="pdf2zh-worker"
APP_DIR="/opt/${APP_NAME}"
VENV_DIR="${APP_DIR}/venv"
DATA_DIR="/var/lib/${APP_NAME}"
CONF_DIR="/etc/${APP_NAME}"
ENV_FILE="${CONF_DIR}/worker.env"
SERVICE_NAME="${APP_NAME}.service"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}"
RUN_USER="pdf2zh"
CLI_LINK="/usr/local/bin/${APP_NAME}"
PY_SERIES=("3.12" "3.11" "3.13" "3.10")   # pdf2zh-next 要求 >=3.10,<3.14

# 脚本可能是通过 /usr/local/bin/pdf2zh-worker 这个软链调起来的，
# 必须解到真身，否则 SRC_DIR 会指到 /usr/local/bin，sync_code 找不到源码。
_resolve_self() {
  local target="${BASH_SOURCE[0]}" dir
  while [[ -L "${target}" ]]; do
    dir="$(cd -P "$(dirname "${target}")" && pwd)"
    target="$(readlink "${target}")"
    [[ "${target}" != /* ]] && target="${dir}/${target}"
  done
  printf '%s/%s' "$(cd -P "$(dirname "${target}")" && pwd)" "$(basename "${target}")"
}
SCRIPT_PATH="$(_resolve_self)"
SRC_DIR="$(dirname "${SCRIPT_PATH}")"

# ---------------------------------------------------------------- 输出工具

if [[ -t 1 ]]; then
  C_RESET=$'\033[0m'; C_DIM=$'\033[2m'; C_RED=$'\033[31m'
  C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'; C_BLUE=$'\033[36m'; C_BOLD=$'\033[1m'
else
  C_RESET=""; C_DIM=""; C_RED=""; C_GREEN=""; C_YELLOW=""; C_BLUE=""; C_BOLD=""
fi

info()  { printf '%s\n' "${C_BLUE}==>${C_RESET} $*"; }
ok()    { printf '%s\n' "${C_GREEN} ✓ ${C_RESET} $*"; }
warn()  { printf '%s\n' "${C_YELLOW} ! ${C_RESET} $*" >&2; }
die()   { printf '%s\n' "${C_RED} ✗ ${C_RESET} $*" >&2; exit 1; }
dim()   { printf '%s\n' "${C_DIM}$*${C_RESET}"; }

confirm() {
  [[ "${ASSUME_YES:-0}" == "1" ]] && return 0
  local reply
  read -r -p "$(printf '%s' "$1 [y/N] ")" reply || return 1
  [[ "${reply}" =~ ^[Yy]$ ]]
}

trap 'printf "%s\n" "${C_RED} ✗ ${C_RESET} 第 ${LINENO} 行执行失败，已中断。上面的报错就是原因。" >&2' ERR

# ---------------------------------------------------------------- 环境探测

require_root() {
  [[ ${EUID} -eq 0 ]] || die "需要 root：请用 sudo 重跑（sudo ${SCRIPT_PATH} $*）"
}

has_systemd() { [[ -d /run/systemd/system ]] && command -v systemctl >/dev/null 2>&1; }

require_linux_systemd() {
  [[ "$(uname -s)" == "Linux" ]] || die "服务化安装只支持 Linux。macOS 本地开发请看 README 的「本地跑一份」。"
  has_systemd || die "没检测到 systemd，无法安装为系统服务。"
}

service_state() {
  if ! has_systemd; then printf 'n/a'; return 0; fi
  systemctl is-active "${SERVICE_NAME}" 2>/dev/null || printf 'inactive'
}

is_installed() { [[ -x "${VENV_DIR}/bin/python" ]]; }

env_get() {  # env_get KEY [默认值]
  local key="$1" fallback="${2:-}" line
  if [[ ! -f "${ENV_FILE}" ]]; then printf '%s' "${fallback}"; return 0; fi
  line="$(grep -E "^${key}=" "${ENV_FILE}" 2>/dev/null | tail -n1 || true)"
  if [[ -z "${line}" ]]; then printf '%s' "${fallback}"; return 0; fi
  printf '%s' "${line#*=}"
}

env_set() {  # env_set KEY VALUE —— 原地改，键不存在就追加
  local key="$1" value="$2" tmp
  install -d -m 750 "${CONF_DIR}"
  touch "${ENV_FILE}"
  if grep -qE "^${key}=" "${ENV_FILE}" 2>/dev/null; then
    tmp="$(mktemp)"
    grep -vE "^${key}=" "${ENV_FILE}" >"${tmp}" || true
    printf '%s=%s\n' "${key}" "${value}" >>"${tmp}"
    cat "${tmp}" >"${ENV_FILE}"    # 用重定向而不是 mv，保住原文件的属主/权限
    rm -f "${tmp}"
  else
    printf '%s=%s\n' "${key}" "${value}" >>"${ENV_FILE}"
  fi
}

#: 配置文件让 root 写、worker 用户读（systemd 是以 root 读 EnvironmentFile 的，
#: 但 --check/--warmup 要以 worker 身份 source 它）
secure_env_file() {
  chown root:"${RUN_USER}" "${CONF_DIR}" "${ENV_FILE}" 2>/dev/null || true
  chmod 750 "${CONF_DIR}"
  chmod 640 "${ENV_FILE}"
}

gen_token() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 32
  else
    head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n'
  fi
}

#: 以 worker 用户跑一条 venv 里的命令，环境从 env 文件里 source（不走命令行，
#: 免得 token 出现在 ps 输出里）
run_as_worker() {
  runuser -u "${RUN_USER}" -- bash -c '
    set -a
    # shellcheck disable=SC1090
    [ -f "$1" ] && . "$1"
    set +a
    shift
    exec "$@"
  ' _ "${ENV_FILE}" "$@"
}

# ---------------------------------------------------------------- Python

find_system_python() {
  local series candidate
  for series in "${PY_SERIES[@]}"; do
    candidate="$(command -v "python${series}" 2>/dev/null || true)"
    if [[ -n "${candidate}" ]]; then printf '%s' "${candidate}"; return 0; fi
  done
  if command -v python3 >/dev/null 2>&1; then
    if python3 -c 'import sys; sys.exit(0 if (3,10) <= sys.version_info < (3,14) else 1)' >/dev/null 2>&1; then
      command -v python3
      return 0
    fi
  fi
  return 1
}

find_uv() {
  local candidate
  for candidate in \
      "$(command -v uv 2>/dev/null || true)" \
      /usr/local/bin/uv /root/.local/bin/uv "${HOME:-/root}/.local/bin/uv"; do
    if [[ -n "${candidate}" && -x "${candidate}" ]]; then printf '%s' "${candidate}"; return 0; fi
  done
  return 1
}

install_uv() {
  info "安装 uv（Astral 官方脚本，用来拉一个合适版本的 Python）…"
  command -v curl >/dev/null 2>&1 || die "需要 curl。先装：apt-get install -y curl 或 yum install -y curl"
  curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh
  find_uv >/dev/null || die "uv 装完却找不到可执行文件，请手动检查 /usr/local/bin/uv"
}

create_venv() {
  local python_bin uv_bin
  if python_bin="$(find_system_python)"; then
    info "用系统 Python：${python_bin}（$("${python_bin}" -V 2>&1)）"
    if "${python_bin}" -m venv "${VENV_DIR}" 2>/dev/null; then
      return 0
    fi
    warn "python -m venv 失败，多半是缺 venv 模块"
    if command -v apt-get >/dev/null 2>&1; then
      info "尝试安装 python3-venv…"
      apt-get update -qq || true
      apt-get install -y -qq python3-venv
      "${python_bin}" -m venv "${VENV_DIR}"
      return 0
    fi
    die "请先装好 venv 模块（Debian/Ubuntu: apt-get install python3-venv）"
  fi

  warn "系统上没有 3.10–3.13 的 Python（pdf2zh-next 只支持这个区间）"
  if ! uv_bin="$(find_uv)"; then
    if confirm "要用 uv 自动装一个 Python 3.12 吗？"; then
      install_uv
      uv_bin="$(find_uv)"
    else
      die "请自行安装 Python 3.10–3.13 后重跑"
    fi
  fi
  info "用 uv 准备 Python 3.12…"
  "${uv_bin}" python install 3.12
  "${uv_bin}" venv --python 3.12 "${VENV_DIR}"
}

# ---------------------------------------------------------------- 安装步骤

sync_code() {
  install -d -m 755 "${APP_DIR}"
  # 从 /usr/local/bin 软链调起来时 SRC_DIR 已经解成 APP_DIR 了，
  # 这时源和目标是同一个地方，不能删了再拷（会把自己删光）
  if [[ "${SRC_DIR}" == "${APP_DIR}" ]]; then
    dim "源码就地运行，跳过同步"
  else
    info "同步 wrapper 源码到 ${APP_DIR}"
    [[ -d "${SRC_DIR}/pdf2zh_worker" ]] || die "在 ${SRC_DIR} 找不到 pdf2zh_worker/，别把脚本单独搬出项目跑"
    rm -rf "${APP_DIR}/pdf2zh_worker"
    cp -R "${SRC_DIR}/pdf2zh_worker" "${APP_DIR}/pdf2zh_worker"
    cp "${SRC_DIR}/pyproject.toml" "${APP_DIR}/"
    local extra
    for extra in README.md LICENSE; do
      if [[ -f "${SRC_DIR}/${extra}" ]]; then cp "${SRC_DIR}/${extra}" "${APP_DIR}/"; fi
    done
    if [[ -d "${SRC_DIR}/deploy" ]]; then
      rm -rf "${APP_DIR}/deploy"
      cp -R "${SRC_DIR}/deploy" "${APP_DIR}/deploy"
    fi
    cp "${SCRIPT_PATH}" "${APP_DIR}/install.sh"
    chmod 755 "${APP_DIR}/install.sh"
  fi
  # 菜单入口：装完敲 pdf2zh-worker 就是这个脚本
  ln -sf "${APP_DIR}/install.sh" "${CLI_LINK}"
  find "${APP_DIR}" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
}

install_deps() {
  info "安装 pdf2zh-next 与 wrapper（依赖树很大，头一次要几分钟）…"
  "${VENV_DIR}/bin/python" -m ensurepip --upgrade >/dev/null 2>&1 || true
  "${VENV_DIR}/bin/python" -m pip install --upgrade pip setuptools wheel
  "${VENV_DIR}/bin/python" -m pip install --upgrade "${APP_DIR}"
  ok "pdf2zh-next $("${VENV_DIR}/bin/python" -c 'import pdf2zh_next; print(pdf2zh_next.__version__)')"
}

ensure_user_and_dirs() {
  if ! getent group "${RUN_USER}" >/dev/null 2>&1; then
    groupadd --system "${RUN_USER}"
  fi
  if ! id -u "${RUN_USER}" >/dev/null 2>&1; then
    info "创建系统用户 ${RUN_USER}"
    local nologin="/usr/sbin/nologin"
    [[ -x "${nologin}" ]] || nologin="/sbin/nologin"
    [[ -x "${nologin}" ]] || nologin="/bin/false"
    useradd --system --gid "${RUN_USER}" --home-dir "${DATA_DIR}" --shell "${nologin}" "${RUN_USER}"
  fi
  install -d -m 750 -o "${RUN_USER}" -g "${RUN_USER}" "${DATA_DIR}"
  install -d -m 750 -o "${RUN_USER}" -g "${RUN_USER}" "${DATA_DIR}/cache" "${DATA_DIR}/config"
  install -d -m 750 "${CONF_DIR}"
}

write_env_defaults() {
  local token
  token="$(env_get TRANSLATE_WORKER_TOKEN)"
  if [[ -z "${token}" ]]; then
    token="$(gen_token)"
    info "已生成新的鉴权 token"
  fi
  env_set TRANSLATE_WORKER_TOKEN "${token}"
  env_set TRANSLATE_WORKER_HOST  "$(env_get TRANSLATE_WORKER_HOST 127.0.0.1)"
  env_set TRANSLATE_WORKER_PORT  "$(env_get TRANSLATE_WORKER_PORT 8791)"
  env_set TRANSLATE_WORKER_DATA  "${DATA_DIR}"
  env_set TRANSLATE_WORKER_CONCURRENCY     "$(env_get TRANSLATE_WORKER_CONCURRENCY 1)"
  env_set TRANSLATE_WORKER_QUEUE_LIMIT     "$(env_get TRANSLATE_WORKER_QUEUE_LIMIT 8)"
  env_set TRANSLATE_WORKER_MAX_MB          "$(env_get TRANSLATE_WORKER_MAX_MB 50)"
  env_set TRANSLATE_WORKER_RETENTION_HOURS "$(env_get TRANSLATE_WORKER_RETENTION_HOURS 24)"
  env_set TRANSLATE_WORKER_LOG_LEVEL       "$(env_get TRANSLATE_WORKER_LOG_LEVEL INFO)"
  # pdf2zh / babeldoc 在 import 期就会往 ~/.config 写东西，HOME 必须指到可写的地方
  env_set HOME            "${DATA_DIR}"
  env_set XDG_CACHE_HOME  "${DATA_DIR}/cache"
  env_set XDG_CONFIG_HOME "${DATA_DIR}/config"
  secure_env_file
}

write_service() {
  info "写入 systemd 单元 ${SERVICE_FILE}"
  cat >"${SERVICE_FILE}" <<UNIT
[Unit]
Description=pdf2zh 文档翻译 worker（TRANSLATION_WORKER_PROTOCOL v1）
Documentation=file://${APP_DIR}/README.md
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${RUN_USER}
Group=${RUN_USER}
EnvironmentFile=${ENV_FILE}
WorkingDirectory=${DATA_DIR}
ExecStart=${VENV_DIR}/bin/python -m pdf2zh_worker
Restart=always
RestartSec=5
# 翻译子进程收摊要几秒，别让 systemd 急着 SIGKILL
TimeoutStopSec=60
KillMode=mixed

# —— 安全加固：这台机器只干一件事，能关的都关掉 ——
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ProtectKernelTunables=true
ProtectControlGroups=true
RestrictSUIDSGID=true
ReadWritePaths=${DATA_DIR}
LimitNOFILE=65535

[Install]
WantedBy=multi-user.target
UNIT
  systemctl daemon-reload
}

do_warmup() {
  info "预热 babeldoc 模型/字体资源（首次可能要下几百 MB，慢是正常的）…"
  install -d -m 750 -o "${RUN_USER}" -g "${RUN_USER}" "${DATA_DIR}/cache" "${DATA_DIR}/config"
  if run_as_worker "${VENV_DIR}/bin/python" -m pdf2zh_worker --warmup; then
    ok "资源就绪"
  else
    warn "预热没成功（多半是网络）。服务照样能起，第一单会边跑边下载。"
  fi
}

health_check() {
  local port token url body
  port="$(env_get TRANSLATE_WORKER_PORT 8791)"
  token="$(env_get TRANSLATE_WORKER_TOKEN)"
  url="http://127.0.0.1:${port}/healthz"
  if ! command -v curl >/dev/null 2>&1; then warn "没有 curl，跳过健康检查"; return 0; fi
  body="$(curl -fsS -m 10 -H "Authorization: Bearer ${token}" "${url}" 2>/dev/null || true)"
  if [[ -z "${body}" ]]; then
    warn "healthz 没响应：${url}"
    return 1
  fi
  if [[ "${body}" == *'"queue"'* ]]; then
    ok "healthz 正常，token 校验通过"
    dim "    ${body}"
    return 0
  fi
  warn "healthz 通了但没回 queue 字段 —— token 对不上：${body}"
  return 1
}

print_summary() {
  local port token
  port="$(env_get TRANSLATE_WORKER_PORT 8791)"
  token="$(env_get TRANSLATE_WORKER_TOKEN)"
  echo
  printf '%s\n' "${C_BOLD}———— 主应用 admin「设置 → 翻译服务 → worker 集群」照这个填 ————${C_RESET}"
  echo
  echo "  Base URL   https://<你的域名>          （nginx 443 反代到 127.0.0.1:${port}）"
  echo "  Token      ${token}"
  echo "  并发       $(env_get TRANSLATE_WORKER_CONCURRENCY 1)（要和这台的 TRANSLATE_WORKER_CONCURRENCY 一致）"
  echo
  dim "  本机自测：curl -H 'Authorization: Bearer <token>' http://127.0.0.1:${port}/healthz"
  dim "  nginx 反代示例见 ${APP_DIR}/deploy/nginx.conf.example"
  echo
  warn "别把 ${port} 直接暴露公网：只监听回环，公网只走 nginx 443。"
}

# ---------------------------------------------------------------- 命令

cmd_install() {
  require_root
  require_linux_systemd
  info "开始安装 ${APP_NAME}"
  ensure_user_and_dirs
  sync_code
  if [[ -d "${VENV_DIR}" ]]; then
    info "清掉旧 venv 重建"
    rm -rf "${VENV_DIR}"
  fi
  create_venv
  install_deps
  write_env_defaults
  chown -R "${RUN_USER}":"${RUN_USER}" "${DATA_DIR}"
  write_service

  info "上线前体检…"
  run_as_worker "${VENV_DIR}/bin/python" -m pdf2zh_worker --check \
    || die "体检没过，先照上面的报错修"

  do_warmup
  systemctl enable "${SERVICE_NAME}" >/dev/null 2>&1 || true
  systemctl restart "${SERVICE_NAME}"
  sleep 2
  if [[ "$(service_state)" != "active" ]]; then
    journalctl -u "${SERVICE_NAME}" -n 30 --no-pager || true
    die "服务没起来，日志在上面"
  fi
  ok "服务已启动"
  health_check || true
  print_summary
  ok "装好了。以后敲 ${C_BOLD}${APP_NAME}${C_RESET} 就能调出菜单。"
}

cmd_upgrade() {
  require_root
  is_installed || die "还没装过，先跑：sudo ${SCRIPT_PATH} install"
  info "升级 wrapper 源码与 pdf2zh-next…"
  sync_code
  "${VENV_DIR}/bin/python" -m pip install --upgrade pip >/dev/null
  "${VENV_DIR}/bin/python" -m pip install --upgrade "${APP_DIR}"
  "${VENV_DIR}/bin/python" -m pip install --upgrade pdf2zh-next
  ok "pdf2zh-next 现在是 $("${VENV_DIR}/bin/python" -c 'import pdf2zh_next; print(pdf2zh_next.__version__)')"
  write_service
  if has_systemd; then
    systemctl restart "${SERVICE_NAME}"
    sleep 2
    if [[ "$(service_state)" != "active" ]]; then
      journalctl -u "${SERVICE_NAME}" -n 30 --no-pager || true
      die "升级后服务没起来"
    fi
    health_check || true
  fi
  ok "升级完成"
}

cmd_uninstall() {
  require_root
  local purge=0
  [[ "${1:-}" == "--purge" ]] && purge=1 || true
  confirm "确定卸载 ${APP_NAME}？" || { info "取消"; return 0; }

  if has_systemd; then
    systemctl stop "${SERVICE_NAME}" 2>/dev/null || true
    systemctl disable "${SERVICE_NAME}" 2>/dev/null || true
    rm -f "${SERVICE_FILE}"
    systemctl daemon-reload
  fi
  rm -f "${CLI_LINK}"
  rm -rf "${APP_DIR}"
  ok "程序目录与服务已删除"

  local wipe=0
  if [[ ${purge} -eq 1 ]]; then
    wipe=1
  elif confirm "连数据目录 ${DATA_DIR} 和配置 ${CONF_DIR}（含 token）一起删？"; then
    wipe=1
  fi
  if [[ ${wipe} -eq 1 ]]; then
    rm -rf "${DATA_DIR}" "${CONF_DIR}"
    userdel "${RUN_USER}" 2>/dev/null || true
    groupdel "${RUN_USER}" 2>/dev/null || true
    ok "数据与配置已清空"
  else
    dim "保留：${DATA_DIR}（任务数据）、${ENV_FILE}（token 等配置）"
  fi
  ok "卸载完成"
}

cmd_status() {
  local state pdf2zh_ver usage
  echo
  printf '%s\n' "${C_BOLD}${APP_NAME} 状态${C_RESET}"
  if is_installed; then
    ok "已安装：${APP_DIR}"
    pdf2zh_ver="$("${VENV_DIR}/bin/python" -c 'import pdf2zh_next; print(pdf2zh_next.__version__)' 2>/dev/null || echo '导入失败')"
    echo "    pdf2zh-next  ${pdf2zh_ver}"
    echo "    Python       $("${VENV_DIR}/bin/python" -V 2>&1)"
  else
    warn "未安装"
  fi
  state="$(service_state)"
  if [[ "${state}" == "active" ]]; then ok "服务运行中"; else warn "服务状态：${state}"; fi
  if [[ -f "${ENV_FILE}" ]]; then
    echo "    监听         $(env_get TRANSLATE_WORKER_HOST 127.0.0.1):$(env_get TRANSLATE_WORKER_PORT 8791)"
    echo "    并发/排队    $(env_get TRANSLATE_WORKER_CONCURRENCY 1) / $(env_get TRANSLATE_WORKER_QUEUE_LIMIT 8)"
    echo "    单文件上限   $(env_get TRANSLATE_WORKER_MAX_MB 50) MB"
    echo "    产物保留     $(env_get TRANSLATE_WORKER_RETENTION_HOURS 24) 小时"
    usage="$(du -sh "${DATA_DIR}" 2>/dev/null | cut -f1 || true)"
    echo "    数据目录     ${DATA_DIR}${usage:+  (占用 ${usage})}"
  fi
  echo
  if [[ "${state}" == "active" ]]; then health_check || true; fi
  echo
}

cmd_logs() {
  has_systemd || die "没有 systemd，看不了 journal"
  info "Ctrl-C 退出"
  journalctl -u "${SERVICE_NAME}" -n 100 -f --no-pager
}

cmd_doctor() {
  local token free
  echo
  printf '%s\n' "${C_BOLD}体检${C_RESET}"
  if is_installed; then ok "venv 存在"; else warn "venv 不存在"; fi
  if [[ -f "${ENV_FILE}" ]]; then ok "配置文件存在"; else warn "配置文件缺失：${ENV_FILE}"; fi
  token="$(env_get TRANSLATE_WORKER_TOKEN)"
  if [[ ${#token} -ge 32 ]]; then
    ok "token 长度 ${#token}，达标"
  else
    warn "token 只有 ${#token} 位，worker 会拒绝启动（协议要求 ≥32）"
  fi
  if is_installed && [[ ${EUID} -eq 0 ]]; then
    run_as_worker "${VENV_DIR}/bin/python" -m pdf2zh_worker --check || warn "--check 没过"
  fi
  if [[ "$(service_state)" == "active" ]]; then ok "服务运行中"; else warn "服务未运行"; fi
  health_check || true
  free="$(df -h "${DATA_DIR}" 2>/dev/null | tail -n1 || true)"
  [[ -n "${free}" ]] && echo "    磁盘：${free}" || true
  echo
}

cmd_token() {
  require_root
  echo
  echo "  当前 token：$(env_get TRANSLATE_WORKER_TOKEN)"
  echo
  if confirm "重新生成一个新 token？（生成后必须去主应用 admin 同步改掉，否则派单全挂）"; then
    env_set TRANSLATE_WORKER_TOKEN "$(gen_token)"
    secure_env_file
    if has_systemd; then systemctl restart "${SERVICE_NAME}" || true; fi
    ok "新 token：$(env_get TRANSLATE_WORKER_TOKEN)"
    warn "现在就去主应用 admin →「翻译服务」把这台 worker 的 token 改掉。"
  fi
}

cmd_config() {
  require_root
  [[ -f "${ENV_FILE}" ]] || die "还没安装"
  local pair key label current value
  echo
  printf '%s\n' "${C_BOLD}改配置${C_RESET}（直接回车＝保持不变）"
  echo
  for pair in \
    "TRANSLATE_WORKER_PORT:监听端口" \
    "TRANSLATE_WORKER_CONCURRENCY:同时翻译任务数（要与主应用该 worker 行的并发一致）" \
    "TRANSLATE_WORKER_QUEUE_LIMIT:排队上限（满了 start 回 429）" \
    "TRANSLATE_WORKER_MAX_MB:单文件上限 MB" \
    "TRANSLATE_WORKER_RETENTION_HOURS:终态任务保留小时数" \
    "TRANSLATE_WORKER_LOG_LEVEL:日志级别 DEBUG/INFO/WARNING"
  do
    key="${pair%%:*}"; label="${pair#*:}"
    current="$(env_get "${key}")"
    value=""
    read -r -p "$(printf '  %s [%s]: ' "${label}" "${current}")" value || true
    if [[ -n "${value}" ]]; then env_set "${key}" "${value}"; fi
  done
  secure_env_file
  ok "已写入 ${ENV_FILE}"
  if has_systemd && confirm "现在重启服务让配置生效？"; then
    systemctl restart "${SERVICE_NAME}"
    sleep 2
    cmd_status
  fi
}

cmd_warmup() { require_root; is_installed || die "还没安装"; do_warmup; }

cmd_clear_cache() {
  require_root
  local was_active=0 removed=0 path
  echo
  dim "  pdf2zh 会把「原文 → 译文」缓存在本地。主应用下发的模型标识随真实路由变化，"
  dim "  换模型即换缓存键、不会复用旧模型译文——正常换模型不用清。只有主应用路由解析"
  dim "  一直失败、标识长期兜底成固定占位符时，不同模型才会挤同一个键，那时才需要清一次。"
  echo
  confirm "清掉翻译缓存？（模型/字体资源不动，不会重新下载几百 MB）" || { info "取消"; return 0; }

  if [[ "$(service_state)" == "active" ]]; then was_active=1; systemctl stop "${SERVICE_NAME}"; fi
  # 只删翻译结果库，别碰同目录下的模型权重
  for path in "${DATA_DIR}"/.cache/babeldoc/cache.v1.db* "${DATA_DIR}"/.cache/pdf2zh_next/cache.v1.db*; do
    if [[ -e "${path}" ]]; then rm -f "${path}"; removed=$((removed + 1)); fi
  done
  ok "清掉 ${removed} 个缓存文件"
  if [[ ${was_active} -eq 1 ]]; then systemctl start "${SERVICE_NAME}"; sleep 2; ok "服务已重启"; fi
}

cmd_service() {  # start|stop|restart
  require_root
  has_systemd || die "没有 systemd"
  systemctl "$1" "${SERVICE_NAME}"
  sleep 1
  ok "已 $1"
  cmd_status
}

# ---------------------------------------------------------------- 菜单

menu() {
  local state badge choice
  while true; do
    state="$(service_state)"
    case "${state}" in
      active)   badge="${C_GREEN}● 运行中${C_RESET}" ;;
      inactive) badge="${C_YELLOW}○ 已停止${C_RESET}" ;;
      *)        badge="${C_RED}○ ${state}${C_RESET}" ;;
    esac
    echo
    printf '%s\n' "${C_BOLD}════ pdf2zh 文档翻译 worker ════${C_RESET}   ${badge}"
    if is_installed; then
      dim "  ${APP_DIR}   端口 $(env_get TRANSLATE_WORKER_PORT 8791)   并发 $(env_get TRANSLATE_WORKER_CONCURRENCY 1)"
    else
      dim "  尚未安装"
    fi
    echo
    echo "   1) 安装 / 重装"
    echo "   2) 升级（wrapper + pdf2zh-next）"
    echo "   3) 查看状态与健康检查"
    echo "   4) 查看实时日志"
    echo "   5) 改配置（端口/并发/上限…）"
    echo "   6) 查看 / 轮换鉴权 token"
    echo "   7) 启动     8) 停止     9) 重启"
    echo "  10) 预热模型资源"
    echo "  11) 清理翻译缓存（保险丝，一般用不到）"
    echo "  12) 体检（doctor）"
    echo "  13) 卸载"
    echo "   0) 退出"
    echo
    choice=""
    read -r -p "  选择: " choice || return 0
    echo
    case "${choice}" in
      1)  cmd_install ;;
      2)  cmd_upgrade ;;
      3)  cmd_status ;;
      4)  cmd_logs ;;
      5)  cmd_config ;;
      6)  cmd_token ;;
      7)  cmd_service start ;;
      8)  cmd_service stop ;;
      9)  cmd_service restart ;;
      10) cmd_warmup ;;
      11) cmd_clear_cache ;;
      12) cmd_doctor ;;
      13) cmd_uninstall ;;
      0|q|Q) return 0 ;;
      *)  warn "没有这个选项：${choice}" ;;
    esac
    echo
    read -r -p "  回车继续…" _ || true
  done
}

usage() {
  cat <<USAGE
${APP_NAME} —— pdf2zh 文档翻译 worker 安装/运维

  ${SCRIPT_PATH}                  打开菜单（默认）
  ${SCRIPT_PATH} install          安装并启动
  ${SCRIPT_PATH} upgrade          升级 wrapper 与 pdf2zh-next
  ${SCRIPT_PATH} uninstall [--purge]
  ${SCRIPT_PATH} status | doctor | logs
  ${SCRIPT_PATH} start | stop | restart
  ${SCRIPT_PATH} config | token | warmup

环境变量 ASSUME_YES=1 跳过所有确认（无人值守用）。
USAGE
}

main() {
  local cmd="${1:-menu}"
  case "${cmd}" in
    menu|"")            menu ;;
    install)            cmd_install ;;
    upgrade|update)     cmd_upgrade ;;
    uninstall|remove)   shift || true; cmd_uninstall "${1:-}" ;;
    status)             cmd_status ;;
    doctor|check)       cmd_doctor ;;
    logs|log)           cmd_logs ;;
    config)             cmd_config ;;
    token)              cmd_token ;;
    warmup)             cmd_warmup ;;
    clear-cache)        cmd_clear_cache ;;
    start|stop|restart) cmd_service "${cmd}" ;;
    -h|--help|help)     usage ;;
    *)                  usage; exit 1 ;;
  esac
}

# 被 source 进来时不自动执行，好让 tests/test_install_sh.sh 单独调里面的函数
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  main "$@"
fi
