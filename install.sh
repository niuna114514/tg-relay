#!/usr/bin/env bash
# tg-relay - 用 MTProto 协议号把频道消息转发到多个群
# Copyright (C) 2026 tg-relay contributors
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
# =============================================================================
#  tg-relay 一键安装 / 管理脚本
#
#  用法（在服务器上，root 身份）：
#
#      bash install.sh              # 打开管理菜单
#      bash install.sh install      # 直接走安装流程
#      bash install.sh status       # 看状态
#      bash install.sh log          # 看日志
#      bash install.sh menu         # 菜单（同无参数）
#      bash install.sh --dry-run    # 只打印会做什么，不落盘不改服务
#      bash install.sh --self-test  # 自检：生成配置并用 Python 校验一遍（不动线上）
#
#  设计原则（都是踩过坑之后定的）：
#
#   1. **幂等**：重复运行不会把已装好的东西搞坏；检测到已安装就切到"管理"语义。
#   2. **失败不静默**：每一步都有明确的 ✅/⚠️/❌，装完一定跑一次 --check。
#   3. **不碰 Telegram 会话**：脚本只负责装和配置；登录由本项目的 login.py 做，
#      而且**一次只能有一个进程持有 session** —— 所以脚本绝不会在服务运行时
#      去连 Telegram（账号自检走的是服务进程内的 API）。
#   4. **非交互也能装**：所有问题都能用 TG_* 环境变量预先回答，
#      便于自动化；没给变量才问人。
# =============================================================================

set -u
export LANG=${LANG:-C.UTF-8}

# ------------------------------ 常量 ----------------------------------------

APP_NAME="tg-relay"
APP_DIR="${TG_APP_DIR:-/opt/tg-relay}"
SERVICE="${TG_SERVICE:-tg-relay}"
SERVICE_FILE="/etc/systemd/system/${SERVICE}.service"
CMD_LINK="/usr/local/bin/tgrelay"
SELF_COPY="/usr/local/share/tg-relay/install.sh"
SCRIPT_VERSION="1.0.0"

# 依赖
APT_PKGS="python3 python3-venv python3-pip git curl tar ca-certificates"
PIP_MIRROR="${TG_PIP_MIRROR:-https://pypi.tuna.tsinghua.edu.cn/simple}"

# 配置答案（都可用环境变量覆盖，便于无人值守安装）
API_ID="${TG_API_ID:-}"
API_HASH="${TG_API_HASH:-}"
SOURCE="${TG_SOURCE:-}"
TARGETS_RAW="${TG_TARGETS:-}"
MODE="${TG_MODE:-repost}"          # repost | relay | both
MATERIALS="${TG_MATERIALS:-}"
REPOST_INTERVAL="${TG_REPOST_INTERVAL:-300}"
DAILY_LIMIT="${TG_DAILY_LIMIT:-60}"
GROUP_INTERVAL="${TG_GROUP_INTERVAL:-300}"
WEB="${TG_WEB:-yes}"
WEB_PORT="${TG_WEB_PORT:-8123}"
WEB_TOKEN="${TG_WEB_TOKEN:-}"
BOT="${TG_BOT:-no}"
BOT_TOKEN="${TG_BOT_TOKEN:-}"
BOT_ADMINS="${TG_BOT_ADMINS:-}"
PREMIUM="${TG_PREMIUM:-no}"
REPO_URL="${TG_REPO:-}"

DRY_RUN=no
for arg in "$@"; do
    [ "$arg" = "--dry-run" ] && DRY_RUN=yes
done

# ------------------------------ 输出 ----------------------------------------

if [ -t 1 ]; then
    C_RED=$'\033[31m'; C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'
    C_BLUE=$'\033[36m'; C_BOLD=$'\033[1m'; C_DIM=$'\033[2m'; C_OFF=$'\033[0m'
else
    C_RED=""; C_GREEN=""; C_YELLOW=""; C_BLUE=""; C_BOLD=""; C_DIM=""; C_OFF=""
fi

hr()      { printf '%s\n' "${C_DIM}────────────────────────────────────────────────────────────${C_OFF}"; }
title()   { printf '\n%s\n' "${C_BOLD}${C_BLUE}$*${C_OFF}"; }
ok()      { printf '%s\n' "  ${C_GREEN}✅${C_OFF} $*"; }
warn()    { printf '%s\n' "  ${C_YELLOW}⚠️ ${C_OFF} $*"; }
err()     { printf '%s\n' "  ${C_RED}❌${C_OFF} $*" >&2; }
info()    { printf '%s\n' "  ${C_DIM}·${C_OFF} $*"; }
note()    { printf '%s\n' "    ${C_DIM}$*${C_OFF}"; }

die() { err "$*"; exit 1; }

pause_any() {
    [ -t 0 ] || return 0
    printf '\n%s' "${C_DIM}按回车继续…${C_OFF}"
    read -r _ || true
}

confirm() {
    # confirm "问题" [默认 y/n]
    local question="$1" default="${2:-n}" answer
    if [ ! -t 0 ]; then
        [ "$default" = "y" ]
        return
    fi
    if [ "$default" = "y" ]; then
        printf '%s' "  ${C_YELLOW}?${C_OFF} ${question} [Y/n] "
    else
        printf '%s' "  ${C_YELLOW}?${C_OFF} ${question} [y/N] "
    fi
    read -r answer || answer=""
    answer="${answer:-$default}"
    case "$answer" in [Yy]*) return 0 ;; *) return 1 ;; esac
}

ask() {
    # ask "提示" "默认值"  -> echo 结果
    local prompt="$1" default="${2:-}" answer
    if [ ! -t 0 ]; then
        printf '%s' "$default"
        return
    fi
    if [ -n "$default" ]; then
        printf '%s' "  ${C_YELLOW}?${C_OFF} ${prompt} ${C_DIM}[${default}]${C_OFF} " >&2
    else
        printf '%s' "  ${C_YELLOW}?${C_OFF} ${prompt} " >&2
    fi
    read -r answer || answer=""
    printf '%s' "${answer:-$default}"
}

ask_secret() {
    local prompt="$1" answer
    if [ ! -t 0 ]; then return 0; fi
    printf '%s' "  ${C_YELLOW}?${C_OFF} ${prompt} " >&2
    read -rs answer || answer=""
    printf '\n' >&2
    printf '%s' "$answer"
}

banner() {
    printf '\n'
    printf '%s\n' "${C_BOLD}${C_BLUE}  tg-relay ${SCRIPT_VERSION}  ·  协议号频道 → 多群转发${C_OFF}"
    printf '%s\n' "${C_DIM}  安装 / 配置 / 管理${C_OFF}"
    hr
}

# ------------------------------ 环境检查 ------------------------------------

require_root() {
    if [ "$(id -u)" != "0" ]; then
        die "需要 root 权限运行：sudo bash $0"
    fi
}

detect_os() {
    if [ -r /etc/os-release ]; then
        # shellcheck disable=SC1091
        . /etc/os-release
        OS_NAME="${PRETTY_NAME:-$ID}"
        OS_ID="${ID:-unknown}"
    else
        OS_NAME="$(uname -s)"; OS_ID="unknown"
    fi

    if command -v apt-get >/dev/null 2>&1; then PKG=apt
    elif command -v dnf    >/dev/null 2>&1; then PKG=dnf
    elif command -v yum    >/dev/null 2>&1; then PKG=yum
    elif command -v apk    >/dev/null 2>&1; then PKG=apk
    else PKG=none; fi

    info "系统：${OS_NAME}（包管理器：${PKG}）"
}

need_cmd() { command -v "$1" >/dev/null 2>&1; }

install_deps() {
    title "安装系统依赖"
    if [ "$PKG" = "none" ]; then
        warn "认不出包管理器，请自行确保已安装：python3(>=3.9) python3-venv pip git curl tar"
        return 0
    fi

    local python_ok=no
    if need_cmd python3; then
        local ver
        ver="$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null || echo 0.0)"
        # Python 3.9 起支持本项目用到的语法
        if [ "$(printf '%s\n3.9\n' "$ver" | sort -V | head -1)" = "3.9" ]; then
            ok "python3 $ver 已就绪"
            python_ok=yes
        else
            warn "python3 版本偏低（$ver），建议 3.9 以上"
        fi
    fi

    local missing=""
    need_cmd git || missing="$missing git"
    need_cmd curl || missing="$missing curl"
    [ "$python_ok" = yes ] || missing="$missing python3"
    python3 -c 'import venv' >/dev/null 2>&1 || missing="$missing python3-venv"
    python3 -m pip --version >/dev/null 2>&1 || missing="$missing python3-pip"

    if [ -z "$missing" ]; then
        ok "依赖齐全"
        return 0
    fi

    if [ "$DRY_RUN" = yes ]; then
        info "[dry-run] 会安装：${missing# }"
        return 0
    fi

    note "缺少：${missing# }"
    if ! confirm "现在用 ${PKG} 安装它们？" y; then
        warn "跳过依赖安装 —— 后面步骤可能失败"
        return 0
    fi

    case "$PKG" in
        apt)
            # Debian 11 的 bullseye-security 源可能已过期（返回 404），
            # 所以 update 失败只警告、不中断：install 照样能用。
            info "更新软件源索引…"
            if ! DEBIAN_FRONTEND=noninteractive apt-get update -qq 2>&1 | tail -3; then
                warn "apt-get update 有报错（源过期很常见），继续尝试安装"
            fi
            DEBIAN_FRONTEND=noninteractive apt-get install -y $APT_PKGS || die "apt 安装失败"
            ;;
        dnf) dnf install -y python3 python3-pip git curl tar || die "dnf 安装失败" ;;
        yum) yum install -y python3 python3-pip git curl tar || die "yum 安装失败" ;;
        apk) apk add --no-cache python3 py3-pip git curl tar || die "apk 安装失败" ;;
    esac
    ok "系统依赖安装完成"
}

# ------------------------------ 代码来源 ------------------------------------

# 脚本所在目录（支持 bash <(curl ...) 的进程替换写法）
script_dir() {
    local path="${BASH_SOURCE[0]:-}"
    if [ -n "$path" ] && [ -f "$path" ]; then
        (cd "$(dirname "$path")" && pwd)
        return
    fi
    pwd
}

looks_like_project() {
    [ -d "$1/tgrelay" ] && [ -f "$1/requirements.txt" ]
}

fetch_code() {
    title "获取代码"
    local src_dir
    src_dir="$(script_dir)"

    local have_local=no
    looks_like_project "$src_dir" && have_local=yes
    looks_like_project "$APP_DIR" && {
        ok "已存在一份代码：${APP_DIR}"
        if [ "$have_local" = yes ] && [ "$src_dir" != "$APP_DIR" ]; then
            if confirm "用当前目录的代码覆盖更新 ${APP_DIR}？" n; then
                copy_code "$src_dir" && ok "已从本地目录更新"
            fi
        fi
        return 0
    }

    local mode=""
    if [ "$have_local" = yes ]; then
        mode="local"
    elif [ -n "$REPO_URL" ]; then
        mode="git"
    elif [ -t 0 ]; then
        echo
        info "没找到本地代码，选择获取方式："
        echo "      1) 从 git 仓库克隆（需要仓库地址）"
        echo "      2) 从压缩包安装（需要 tar.gz 直链）"
        echo "      3) 我先手动把代码放到 ${APP_DIR}，跳过"
        case "$(ask '选哪个？' 1)" in
            1) mode="git" ;;
            2) mode="tar" ;;
            *) mode="skip" ;;
        esac
    else
        die "没有本地代码，也没给 TG_REPO 地址"
    fi

    case "$mode" in
        local) copy_code "$src_dir" ;;
        git)
            [ -n "$REPO_URL" ] || REPO_URL="$(ask 'git 仓库地址（https 或 git@）' '')"
            [ -n "$REPO_URL" ] || die "没给仓库地址"
            if [ "$DRY_RUN" = yes ]; then
                info "[dry-run] git clone $REPO_URL -> $APP_DIR"
                return 0
            fi
            need_cmd git || die "没有 git，先装依赖"
            git clone --depth 1 "$REPO_URL" "$APP_DIR" || die "克隆失败（私有仓库请用带令牌的 HTTPS 地址）"
            ok "已克隆到 ${APP_DIR}"
            ;;
        tar)
            local url
            url="$(ask 'tar.gz 直链' '')"
            [ -n "$url" ] || die "没给地址"
            if [ "$DRY_RUN" = yes ]; then
                info "[dry-run] 下载 $url 解包到 $APP_DIR"
                return 0
            fi
            mkdir -p "$APP_DIR"
            curl -fsSL "$url" -o /tmp/tg-relay.tar.gz || die "下载失败"
            tar -xzf /tmp/tg-relay.tar.gz -C "$APP_DIR" --strip-components=1 || die "解包失败"
            rm -f /tmp/tg-relay.tar.gz
            ok "已解包到 ${APP_DIR}"
            ;;
        skip)
            if ! looks_like_project "$APP_DIR"; then
                die "${APP_DIR} 里没看到 tgrelay/ 目录，先放好代码再重跑"
            fi
            ;;
    esac
}

copy_code() {
    local from="$1"
    if [ "$DRY_RUN" = yes ]; then
        info "[dry-run] 复制 $from -> $APP_DIR（保留 .env / config.yaml / data）"
        return 0
    fi
    mkdir -p "$APP_DIR"
    # 用 tar 管道复制，排除运行时文件和凭据（绝不覆盖线上配置）
    ( cd "$from" && tar -cf - \
        --exclude='.git' --exclude='__pycache__' --exclude='.pytest_cache' \
        --exclude='.venv' --exclude='data' --exclude='.env' \
        --exclude='config.yaml' --exclude='publish' . ) \
      | ( cd "$APP_DIR" && tar -xf - )
}

setup_venv() {
    title "Python 虚拟环境"
    if [ "$DRY_RUN" = yes ]; then
        info "[dry-run] python3 -m venv ${APP_DIR}/.venv && pip install -r requirements.txt"
        return 0
    fi
    need_cmd python3 || die "没有 python3"

    if [ -x "$APP_DIR/.venv/bin/python" ]; then
        ok "虚拟环境已存在"
    else
        info "创建虚拟环境…"
        python3 -m venv "$APP_DIR/.venv" || die "创建虚拟环境失败（Debian 上通常需要 apt install python3-venv）"
    fi

    info "安装 Python 依赖（第一次会比较慢）…"
    "$APP_DIR/.venv/bin/python" -m pip install --upgrade pip -q 2>/dev/null || true
    if ! "$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"; then
        warn "默认源安装失败，改用国内镜像重试…"
        "$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt" -i "$PIP_MIRROR" \
            || die "依赖安装失败（可以设 TG_PIP_MIRROR 换源）"
    fi
    ok "依赖安装完成"
}

# ------------------------------ 引导式配置 ----------------------------------

gen_token() {
    if need_cmd openssl; then
        openssl rand -base64 36 | tr -d '/+=\n' | cut -c1-40
    else
        head -c 48 /dev/urandom | base64 | tr -d '/+=\n' | cut -c1-40
    fi
}

explain_api() {
    echo
    info "先去 https://my.telegram.org/apps 拿 api_id / api_hash："
    note "1) 用你的 Telegram 号登录（要收验证码）"
    note "2) 点 API development tools，随便填个应用名"
    note "3) 复制 api_id（一串数字）和 api_hash（32 位十六进制）"
    note "⚠️ 这两个是「应用」的身份，不是账号密码；但也不要外传。"
}

guided_config() {
    title "配置向导"

    # ---- 1. 凭据 ----
    if [ -z "$API_ID" ] || [ -z "$API_HASH" ]; then
        explain_api
        [ -n "$API_ID" ]   || API_ID="$(ask 'api_id（数字）' '')"
        [ -n "$API_HASH" ] || API_HASH="$(ask 'api_hash（32 位十六进制）' '')"
    fi
    [ -n "$API_ID" ]   || die "api_id 不能为空"
    [ -n "$API_HASH" ] || die "api_hash 不能为空"
    case "$API_ID" in ''|*[!0-9]*) die "api_id 必须是数字" ;; esac
    ok "凭据已收集（api_id=${API_ID}）"

    # ---- 2. 源频道 ----
    if [ -z "$SOURCE" ]; then
        echo
        info "源频道：要转发的那个频道，填 @用户名 或 -100 开头的数字 ID"
        note "机器人的 API 读不到任意频道的历史，所以必须用协议号 —— 这也是本项目的由来"
        SOURCE="$(ask '源频道' '')"
    fi
    [ -n "$SOURCE" ] || die "源频道不能为空"

    # ---- 3. 目标群 ----
    if [ -z "$TARGETS_RAW" ]; then
        echo
        info "目标群：可以填多个，用逗号或空格分隔"
        note "这个号必须**已经加入**这些群，否则发了会失败（自检会告诉你）"
        TARGETS_RAW="$(ask '目标群' '')"
    fi
    [ -n "$TARGETS_RAW" ] || die "至少要有一个目标群"

    # ---- 4. 运行模式 ----
    if [ -z "${TG_MODE:-}" ] && [ -t 0 ]; then
        echo
        info "运行模式："
        echo "      1) 只做定时重发（把固定的几条内容长期摆在群里）"
        echo "      2) 只做实时转发（源发什么就跟着转什么）"
        echo "      3) 两个都要"
        case "$(ask '选哪个？' 1)" in
            2) MODE=relay ;;
            3) MODE=both ;;
            *) MODE=repost ;;
        esac
    fi
    ok "模式：$MODE"

    # ---- 5. 重发素材（只有需要时才问）----
    if [ "$MODE" = "repost" ] || [ "$MODE" = "both" ]; then
        if [ -z "$MATERIALS" ]; then
            echo
            info "重发素材：源频道里的第几条消息？"
            note "单个：6      一段：6-12     几段：6-12,20-25"
            MATERIALS="$(ask '素材消息 ID' '')"
        fi
        [ -n "$MATERIALS" ] || die "开了重发就必须给素材 ID"

        REPOST_INTERVAL="$(ask '轮次间隔（秒）' "$REPOST_INTERVAL")"
        DAILY_LIMIT="$(ask '每天每群最多发几条' "$DAILY_LIMIT")"

        # ★ 素材条数直接影响"重复特征"，这里就地给一次风险提示
        local material_count
        material_count="$(count_materials "$MATERIALS")"
        info "素材 $material_count 条 × 每群每天 $DAILY_LIMIT 条 → 每条每天在同一个群出现约 $(( DAILY_LIMIT / (material_count > 0 ? material_count : 1) )) 次"
    fi

    GROUP_INTERVAL="$(ask '同一个群两条消息之间至少隔几秒' "$GROUP_INTERVAL")"

    # ---- 6. 网页面板 ----
    echo
    if confirm "开启网页面板（浏览器/API 管理，跑在本机 127.0.0.1，要靠 SSH 隧道访问）？" y; then
        WEB=yes
        WEB_PORT="$(ask '面板端口' "$WEB_PORT")"
        [ -n "$WEB_TOKEN" ] || WEB_TOKEN="$(gen_token)"
        ok "面板令牌已生成（下面会打印，记一下）"
    else
        WEB=no
        note "不开面板也可以：文字面板和命令行都能看状态，只是改配置要手动编辑 config.yaml + 重启"
    fi

    # ---- 7. 操控 Bot ----
    echo
    if [ "$BOT" = "yes" ]; then
        :
    elif confirm "开启 Telegram 操控 Bot？（在手机上发 /status 看状态、改间隔、开关重发）" n; then
        BOT=yes
    fi
    if [ "$BOT" = "yes" ]; then
        note "去 @BotFather 发 /newbot 拿 token（形如 123456:AA…）"
        [ -n "$BOT_TOKEN" ]  || BOT_TOKEN="$(ask 'bot token' '')"
        [ -n "$BOT_ADMINS" ] || BOT_ADMINS="$(ask '你的 Telegram 数字 ID（不给也行，第一次发消息会自动认管理员）' '')"
    fi

    # ---- 8. Premium ----
    if [ "$PREMIUM" = "yes" ]; then
        :
    elif confirm "这个号开了 Telegram Premium？（放宽限速，并能转发「禁止转发」的频道内容）" n; then
        PREMIUM=yes
    fi
    ok "配置收集完成"
}

count_materials() {
    # "6-12,20" -> 8
    local spec="$1" total=0 part a b
    IFS=',' read -ra parts <<< "$spec"
    for part in "${parts[@]}"; do
        part="$(printf '%s' "$part" | tr -d ' ')"
        [ -z "$part" ] && continue
        case "$part" in
            *-*)
                a="${part%%-*}"; b="${part##*-}"
                if [ "$a" -gt 0 ] 2>/dev/null && [ "$b" -ge "$a" ] 2>/dev/null; then
                    total=$(( total + b - a + 1 ))
                fi
                ;;
            *)
                case "$part" in ''|*[!0-9]*) ;; *) total=$(( total + 1 )) ;; esac
                ;;
        esac
    done
    [ "$total" -gt 0 ] || total=1
    printf '%s' "$total"
}

write_env_file() {
    [ "$DRY_RUN" = yes ] && { info "[dry-run] 写 ${APP_DIR}/.env"; return 0; }
    umask 077
    cat > "$APP_DIR/.env" <<EOF
# tg-relay 凭据（chmod 600；这个文件等价于半把钥匙，别外传、别提交到 git）
TG_API_ID=${API_ID}
TG_API_HASH=${API_HASH}
TG_SESSION=data/relay.session
TG_PREMIUM=${PREMIUM}
EOF
    [ -n "$WEB_TOKEN" ] && echo "TG_WEB_TOKEN=${WEB_TOKEN}" >> "$APP_DIR/.env"
    [ -n "$BOT_TOKEN" ] && echo "TG_BOT_TOKEN=${BOT_TOKEN}" >> "$APP_DIR/.env"
    [ -n "$BOT_ADMINS" ] && echo "TG_BOT_ADMINS=${BOT_ADMINS}" >> "$APP_DIR/.env"
    chmod 600 "$APP_DIR/.env"
    ok "${APP_DIR}/.env 已写入（权限 600）"
}

write_config_file() {
    local targets_yaml=""
    local name
    IFS=', ' read -ra tlist <<< "$TARGETS_RAW"
    for name in "${tlist[@]}"; do
        [ -z "$name" ] && continue
        # label 一定要加引号：`@` 是 YAML 的保留起始字符，
        # 写成 `label: @群名` 会让整个文件解析失败
        # （in "<unicode string>", column N: found character '@' that cannot start any token）。
        # 项目里的 config_store._fmt() 早就为写回路径处理过这件事，这里同理。
        targets_yaml="${targets_yaml}
  - id: \"${name}\"
    label: \"${name}\"
    interval: [${GROUP_INTERVAL}, ${GROUP_INTERVAL}]"
    done

    # ---- 素材写成 ranges（区间）和 ids（离散）两段 ----
    # 用数组拼再 join，别用字符串接来接去 —— 第一版就是那么写的，
    # 结果拼出了 "[, 6]" 这种带前导逗号的非法 YAML。
    local ranges_list=() ids_list=() part a b
    if [ -n "$MATERIALS" ]; then
        IFS=',' read -ra mlist <<< "$MATERIALS"
        for part in "${mlist[@]}"; do
            part="$(printf '%s' "$part" | tr -d ' ')"
            [ -z "$part" ] && continue
            case "$part" in
                *-*)
                    a="${part%%-*}"; b="${part##*-}"
                    if [ "$a" -gt 0 ] 2>/dev/null && [ "$b" -ge "$a" ] 2>/dev/null; then
                        ranges_list+=("$part")
                    else
                        warn "素材范围 '${part}' 不合法，已跳过（正确写法如 6-12）"
                    fi
                    ;;
                *)
                    case "$part" in
                        ''|*[!0-9]*) warn "素材 '${part}' 不是数字，已跳过" ;;
                        *) ids_list+=("$part") ;;
                    esac
                    ;;
            esac
        done
    fi

    local ranges_yaml="[]" ids_yaml="[]"
    if [ "${#ranges_list[@]}" -gt 0 ]; then
        ranges_yaml=""
        for part in "${ranges_list[@]}"; do
            ranges_yaml="${ranges_yaml}
    - \"${part}\""
        done
    fi
    if [ "${#ids_list[@]}" -gt 0 ]; then
        ids_yaml="[$(printf '%s, ' "${ids_list[@]}" | sed 's/, $//')]"
    fi

    local repost_enabled=false
    case "$MODE" in repost|both) repost_enabled=true ;; esac

    # 统一成 true/false：YAML 1.1 里 `no` 也能解析成布尔，但写 true/false 更不容易看错
    local premium_yaml=false
    [ "$PREMIUM" = "yes" ] && premium_yaml=true

    if [ "$DRY_RUN" = yes ]; then
        info "[dry-run] 写 ${APP_DIR}/config.yaml（源 ${SOURCE}，目标：${TARGETS_RAW}，模式 ${MODE}）"
        return 0
    fi

    cat > "$APP_DIR/config.yaml" <<EOF
# 由 install.sh 的配置向导生成（${SCRIPT_VERSION}）
# 想改配置：重新跑 tgrelay 选「修改配置」，或者直接编辑本文件后重启服务
sources:
  - "${SOURCE}"

targets:${targets_yaml}

filters:
  keywords: []
  exclude: []
  regex: []
  min_length: 0
  media: []

rate:
  per_target_interval: [${GROUP_INTERVAL}, ${GROUP_INTERVAL}]
  cross_target_delay: [5, 15]
  global_per_minute: 20
  daily_cap: 200

premium: ${premium_yaml}

behavior:
  sync_edits: false
  sync_deletes: false
  catch_up_on_start: false      # 首次运行务必 false，否则会把源的历史消息全补发一遍
  catch_up_limit: 10
  album_window: 0.6
  queue_size: 200
  drop_on_queue_full: true
  forward_as_album: true

storage:
  db_path: data/relay.db
  log_path: data/relay.log

repost:
  enabled: ${repost_enabled}
  ranges: ${ranges_yaml}
  ids: ${ids_yaml}
  interval: ${REPOST_INTERVAL}
  daily_limit: ${DAILY_LIMIT}
  targets: []
  shuffle: true                 # 随机顺序，减少"固定周期"这种特征
  run_mode: loop
EOF
    ok "${APP_DIR}/config.yaml 已写入"
}

# ------------------------------ 登录 ---------------------------------------

run_login() {
    title "登录协议号"
    if [ "$DRY_RUN" = yes ]; then
        info "[dry-run] 需要的话执行 login.py"
        return 0
    fi
    if [ -f "$APP_DIR/data/relay.session" ]; then
        ok "已有会话文件 data/relay.session"
        return 0
    fi
    if [ ! -t 0 ]; then
        warn "非交互环境，跳过登录；请稍后手动执行："
        note "cd ${APP_DIR} && .venv/bin/python login.py"
        return 0
    fi

    echo
    info "接下来要输入手机号 + Telegram 收到的验证码（开了两步验证还要密码）"
    note "⚠️ 同一个 session 只能被一个进程持有。如果你在别的机器上也在跑，先停掉那边的。"
    confirm "现在登录？" y || { warn "跳过登录，服务会因为没会话而启动失败"; return 0; }

    ( cd "$APP_DIR" && .venv/bin/python login.py ) || warn "登录没成功，可以重跑本脚本的「登录」项"
}

# ------------------------------ systemd -------------------------------------

build_exec_flags() {
    local flags=""
    case "$MODE" in
        repost) flags="--repost-only" ;;
        relay)  flags="" ;;
        both)   flags="--repost" ;;
    esac
    [ "$WEB" = "yes" ] && flags="$flags --web --web-host 127.0.0.1 --web-port ${WEB_PORT}"
    [ "$BOT" = "yes" ] && flags="$flags --bot"
    printf '%s' "$flags"
}

write_service() {
    title "安装 systemd 服务"
    local flags
    flags="$(build_exec_flags)"

    if [ "$DRY_RUN" = yes ]; then
        info "[dry-run] 写 ${SERVICE_FILE}"
        note "ExecStart=${APP_DIR}/.venv/bin/python -m tgrelay${flags}"
        return 0
    fi

    cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=${APP_NAME}（协议号多群转发 + 定时重发 + 面板/Bot）
Documentation=file://${APP_DIR}/README.md
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${APP_DIR}
# .env 里的 TG_WEB_TOKEN / TG_BOT_TOKEN 是进程启动时读的环境变量，
# 所以这里必须 EnvironmentFile（应用自己也会读 .env，但那是另一条路径）
EnvironmentFile=-${APP_DIR}/.env
Environment=PYTHONUNBUFFERED=1
ExecStart=${APP_DIR}/.venv/bin/python -m tgrelay${flags}
Restart=always
RestartSec=15
# 优雅退出：程序自己会冲刷相册缓冲、停 worker
KillSignal=SIGTERM
TimeoutStopSec=30
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

    ok "已写入 ${SERVICE_FILE}"
    systemctl daemon-reload
    systemctl enable "$SERVICE" >/dev/null 2>&1 && ok "已设为开机自启"
}

start_service() {
    title "启动服务"
    if [ "$DRY_RUN" = yes ]; then
        info "[dry-run] systemctl restart ${SERVICE}"
        return 0
    fi
    systemctl restart "$SERVICE"
    info "等待启动…"
    local i state
    for i in $(seq 1 15); do
        sleep 2
        state="$(systemctl is-active "$SERVICE" 2>/dev/null || true)"
        [ "$state" = "active" ] && break
        if [ "$state" = "failed" ]; then break; fi
        printf '.'
    done
    printf '\n'

    state="$(systemctl is-active "$SERVICE" 2>/dev/null || true)"
    if [ "$state" = "active" ]; then
        ok "服务已运行"
        echo
        info "自检结果（源/目标能否解析、能否发言、群慢速）："
        journalctl -u "$SERVICE" --no-pager -n 30 2>/dev/null | grep -E "自检|慢速|额度|面板已启动|Bot" | tail -12 | sed 's/^/    /'
    else
        err "服务状态：${state}"
        echo
        info "最后 25 行日志："
        journalctl -u "$SERVICE" --no-pager -n 25 2>/dev/null | sed 's/^/    /'
        echo
        warn "常见原因："
        note "· 没登录（先跑「登录」）· api_id/api_hash 填错"
        note "· 目标群还没加入 · config.yaml 里的 @名字 写错"
    fi
}

print_summary() {
    echo
    hr
    printf '%s\n' "${C_BOLD}${C_GREEN}  安装完成${C_OFF}"
    hr
    echo
    printf '  %s\n' "${C_BOLD}接下来怎么用${C_OFF}"
    echo "    重新打开这个菜单：  ${C_BOLD}tgrelay${C_OFF}"
    echo "    看服务状态：        systemctl status ${SERVICE}"
    echo "    看实时日志：        journalctl -u ${SERVICE} -f"
    echo "    文字面板：          cd ${APP_DIR} && .venv/bin/python -m tgrelay --panel"

    if [ "$WEB" = "yes" ]; then
        echo
        printf '  %s\n' "${C_BOLD}网页面板${C_OFF}（只绑本机，用 SSH 隧道打开）"
        echo "    1) 在你自己的电脑上执行："
        echo "       ${C_BOLD}ssh -L ${WEB_PORT}:127.0.0.1:${WEB_PORT} root@<服务器IP>${C_OFF}"
        echo "    2) 然后浏览器打开（令牌已带在链接里）："
        echo "       ${C_BOLD}http://127.0.0.1:${WEB_PORT}/?token=${WEB_TOKEN}${C_OFF}"
        echo
        note "令牌也写在 ${APP_DIR}/.env 里（TG_WEB_TOKEN），别外传：它能完全控制这个号。"
        note "想对外直接访问，需要额外配 nginx + TLS + Basic Auth，见 README 第 6 节。"
    fi

    if [ "$BOT" = "yes" ]; then
        echo
        printf '  %s\n' "${C_BOLD}操控 Bot${C_OFF}"
        echo "    给 bot 发 /status 看状态，/help 看全部命令"
    fi

    echo
    printf '  %s\n' "${C_BOLD}改配置${C_OFF}"
    echo "    tgrelay  →  选「修改配置」（会重新走一遍向导，改完自动重启）"
    echo
    hr
}

# ------------------------------ 菜单各功能 ----------------------------------

show_status() {
    title "运行状态"
    if ! systemctl cat "$SERVICE" >/dev/null 2>&1; then
        warn "还没安装（找不到 ${SERVICE}.service）"
        return 0
    fi
    local state
    state="$(systemctl is-active "$SERVICE" 2>/dev/null || true)"
    case "$state" in
        active)   ok "服务：运行中" ;;
        inactive) warn "服务：已停止" ;;
        failed)   err  "服务：启动失败" ;;
        *)        warn "服务：$state" ;;
    esac
    echo
    info "启动参数："
    # systemctl show -p ExecStart 的格式是
    #   { path=/usr/bin/python ; argv[]=/usr/bin/python -m tgrelay --web ; ignore_errors=no ; ... }
    # 直接 grep -o 会把 path= 和 argv[]= 里的可执行文件各抓一次（显示成两个 python）。
    # 只取 argv[]= 那一段。
    systemctl show -p ExecStart --value "$SERVICE" 2>/dev/null \
        | tr ';' '\n' \
        | sed -n 's/^[[:space:]]*argv\[\]=//p' \
        | sed 's/[[:space:]]*$//' \
        | tr '\n' ' ' | sed 's/^/    /'
    echo
    info "运行时长：$(systemctl show -p ActiveEnterTimestamp --value "$SERVICE" 2>/dev/null)"
    info "重启次数：$(systemctl show -p NRestarts --value "$SERVICE" 2>/dev/null)"

    local py="$APP_DIR/.venv/bin/python"
    if [ -x "$py" ] && [ -f "$APP_DIR/config.yaml" ]; then
        echo
        info "配置摘要："
        ( cd "$APP_DIR" && "$py" - <<'PY' 2>/dev/null
import sys
try:
    from tgrelay.config import load_config
    c = load_config("config.yaml")
except Exception as exc:
    print(f"    读配置失败：{exc}")
    sys.exit(0)
print(f"    源：{', '.join(str(s) for s in c.sources)}")
for t in c.targets:
    print(f"    目标：{t.display}  间隔={t.interval or c.rate.per_target_interval}  每日额度={t.daily_limit or c.rate.daily_cap}")
r = c.repost
if r.enabled:
    ids = list(r.message_ids())
    print(f"    重发：开（{len(ids)} 条素材 {ids[:6]}{'…' if len(ids) > 6 else ''}，轮次间隔 {r.interval}s，日额度 {r.daily_limit}）")
else:
    print("    重发：关")
PY
        )
    fi
    echo
    info "最近日志："
    journalctl -u "$SERVICE" --no-pager -n 8 2>/dev/null | sed 's/^/    /'
}

show_logs() {
    title "日志"
    echo "    1) 最后 50 行"
    echo "    2) 实时跟踪（Ctrl+C 退出）"
    echo "    3) 只看错误"
    case "$(ask '选哪个？' 1)" in
        2) journalctl -u "$SERVICE" -f ;;
        3) journalctl -u "$SERVICE" --no-pager -n 100 | grep -E "ERROR|❌|失败" | tail -40 ;;
        *) journalctl -u "$SERVICE" --no-pager -n 50 ;;
    esac
}

account_check() {
    title "账号自检（是否被 Telegram 限制）"
    note "群权限里**看不出**账号级限制，只能真去问一次 @SpamBot"
    note "走的是服务进程内部的 API，不会新建 Telegram 连接（避免 session 冲突）"
    local py="$APP_DIR/.venv/bin/python"
    [ -x "$py" ] || py="python3"

    # 令牌和端口都用项目自己的发现逻辑去找 —— 别只盯 .env：
    # 真实部署里令牌经常放在 systemd 的 drop-in（xxx.service.d/*.conf）里，
    # .env 里根本没有。第一版就是只读 .env，于是明明能查却报"没有令牌"。
    local endpoint token port
    endpoint="$( cd "$APP_DIR" && "$py" - "$SERVICE" <<'PY' 2>/dev/null
import sys
from pathlib import Path
from tgrelay.panel import _discover_token, _discover_port
svc = sys.argv[1]
print("TOKEN=" + _discover_token(None, Path("."), svc))
print("PORT=" + str(_discover_port(None, svc)))
PY
    )"
    token="$(printf '%s\n' "$endpoint" | sed -n 's/^TOKEN=//p')"
    port="$(printf '%s\n' "$endpoint" | sed -n 's/^PORT=//p')"
    [ -n "$port" ] || port="${WEB_PORT:-8123}"

    if [ -z "$token" ]; then
        warn "没找到面板令牌（服务没开 --web？）"
        note "这种情况改在停机状态查（同一个 session 只能被一个进程持有）："
        note "systemctl stop ${SERVICE}"
        note "cd ${APP_DIR} && .venv/bin/python -m tgrelay --check-account"
        note "systemctl start ${SERVICE}"
        return 0
    fi

    info "正在问 @SpamBot（本机 127.0.0.1:${port}）…"
    local out
    out="$(curl -s -m 45 -X POST -H "Authorization: Bearer ${token}" \
            "http://127.0.0.1:${port}/api/account/check" 2>/dev/null || true)"
    if [ -z "$out" ]; then
        err "请求面板 API 失败（服务在跑吗？面板开了吗？）"
        return 0
    fi
    printf '%s' "$out" | "$py" -c '
import json,sys
raw = sys.stdin.read()
try:
    d = json.loads(raw)
except Exception:
    print("    原始返回：", raw[:200]); raise SystemExit
limited = d.get("limited")
icon = {True:"🚫", False:"✅", None:"❓"}.get(limited, "❓")
print("    " + icon + " " + str(d.get("message","")))
for line in (d.get("detail") or "").strip().splitlines()[:6]:
    print("      " + line[:100])
'
}

open_panel() {
    title "文字面板"
    note "全屏面板：本机 API 客户端，不连 Telegram；按 q 退出"
    ( cd "$APP_DIR" && .venv/bin/python -m tgrelay --panel --service "$SERVICE" ) || warn "面板打开失败"
}

update_code() {
    title "更新代码"
    local src_dir
    src_dir="$(script_dir)"
    if looks_like_project "$src_dir" && [ "$src_dir" != "$APP_DIR" ]; then
        confirm "用当前目录的代码覆盖 ${APP_DIR}？（不覆盖 .env / config.yaml / data）" y || return 0
        copy_code "$src_dir" && ok "代码已更新"
    elif [ -n "$REPO_URL" ] || confirm "从 git 仓库拉取更新？" n; then
        [ -n "$REPO_URL" ] || REPO_URL="$(ask 'git 仓库地址' '')"
        if [ -d "$APP_DIR/.git" ]; then
            ( cd "$APP_DIR" && git pull --ff-only ) && ok "已拉取更新" || warn "git pull 失败，手动处理一下"
        else
            warn "${APP_DIR} 不是 git 仓库，无法自动更新"
        fi
    else
        return 0
    fi
    setup_venv
    confirm "现在重启服务让新代码生效？" y && start_service
}

uninstall_app() {
    title "卸载"
    warn "这会停止并删除服务；配置文件和数据是否删除会另外问你"
    confirm "确定卸载 ${APP_NAME}？" n || return 0
    systemctl stop "$SERVICE" 2>/dev/null || true
    systemctl disable "$SERVICE" >/dev/null 2>&1 || true
    rm -f "$SERVICE_FILE"
    systemctl daemon-reload
    ok "服务已卸载"

    if confirm "连同代码、配置和数据一起删除（含会话文件）？" n; then
        rm -rf "$APP_DIR"
        ok "已删除 ${APP_DIR}"
    else
        info "保留了 ${APP_DIR}（含 .env / config.yaml / data）"
    fi
    if confirm "删除命令 ${CMD_LINK}？" y; then
        rm -f "$CMD_LINK"
        rm -rf "$(dirname "$SELF_COPY")"
        ok "已删除命令"
    fi
}

install_command() {
    [ "$DRY_RUN" = yes ] && return 0
    mkdir -p "$(dirname "$SELF_COPY")"
    cp -f "${BASH_SOURCE[0]}" "$SELF_COPY" 2>/dev/null || return 0
    chmod +x "$SELF_COPY"
    cat > "$CMD_LINK" <<EOF
#!/usr/bin/env bash
exec bash ${SELF_COPY} "\$@"
EOF
    chmod +x "$CMD_LINK"
}

# ------------------------------ 主流程 --------------------------------------

do_install() {
    banner
    require_root
    detect_os
    install_deps

    if [ "$DRY_RUN" = no ]; then
        mkdir -p "$APP_DIR"
    fi
    fetch_code
    install_command
    setup_venv
    guided_config

    # 先把必须的东西写下来，再做需要联网的步骤，这样中途失败也能重跑
    write_env_file
    write_config_file

    if [ "$DRY_RUN" = yes ]; then
        title "dry-run 结束"
        info "上面是将会执行的动作；加 --self-test 可以用 Python 校验生成的配置"
        return 0
    fi

    # ★ 重发但素材为空的话，程序会启动失败 —— 提前拦住
    if [ "$MODE" != "relay" ] && [ ! -s "$APP_DIR/config.yaml" ]; then
        die "config.yaml 没写成功"
    fi

    run_login
    write_service
    start_service
    print_summary
}

self_test() {
    banner
    title "自检：生成配置并用 Python 校验"
    local tmp
    tmp="$(mktemp -d)"
    local saved_dir="$APP_DIR"
    APP_DIR="$tmp"
    DRY_RUN=no

    API_ID="${API_ID:-1234567}"
    API_HASH="${API_HASH:-0123456789abcdef0123456789abcdef}"
    SOURCE="${SOURCE:-@example_channel}"
    TARGETS_RAW="${TARGETS_RAW:-@example_group_a,@example_group_b}"
    MODE="${MODE:-repost}"
    MATERIALS="${MATERIALS:-6-12,20}"
    WEB_TOKEN="${WEB_TOKEN:-test-token-not-real}"

    write_env_file >/dev/null
    write_config_file >/dev/null

    echo
    info "生成的 config.yaml："
    sed 's/^/    /' "$tmp/config.yaml" | head -40

    echo
    info "用项目自己的解析器校验："
    local py="$saved_dir/.venv/bin/python"
    [ -x "$py" ] || py="python3"
    ( cd "$saved_dir" && "$py" - <<PY
import sys
sys.path.insert(0, "${saved_dir}")
from tgrelay.config import load_config
c = load_config("${tmp}/config.yaml")
print(f"    ✅ 解析成功")
print(f"       源：{list(c.sources)}")
for t in c.targets:
    print(f"       目标：{t.display} interval={t.interval} daily_limit={t.daily_limit}")
print(f"       重发：enabled={c.repost.enabled} 素材={list(c.repost.message_ids())} "
      f"间隔={c.repost.interval} 日额度={c.repost.daily_limit} shuffle={c.repost.shuffle}")
assert list(c.sources) == ["@example_channel"], "源写错了"
assert len(c.targets) == 2, "目标数量不对"
assert list(c.repost.message_ids()) == list(range(6, 13)) + [20], "素材展开不对"
assert c.repost.enabled is True
print("    ✅ 断言全部通过")
PY
    )
    local rc=$?
    APP_DIR="$saved_dir"
    rm -rf "$tmp"
    return $rc
}

read_current_settings() {
    """把线上现有配置读回来当默认值。

    不这么做的话，「修改配置」每次都要从零重填一遍 —— 改一个间隔要重答七八个问题，
    很容易手滑把源频道写错。
    """
    local py="$APP_DIR/.venv/bin/python"
    [ -x "$py" ] || py="python3"

    # 运行模式从 systemd 的启动参数读（那才是真正生效的）
    local exec_line
    exec_line="$(systemctl show -p ExecStart --value "$SERVICE" 2>/dev/null || true)"
    case "$exec_line" in
        *--repost-only*) MODE="repost" ;;
        *--repost*)      MODE="both" ;;
        *)               MODE="relay" ;;
    esac
    WEB="no";  case "$exec_line" in *--web*) WEB="yes" ;; esac
    BOT="no";  case "$exec_line" in *--bot*) BOT="yes" ;; esac
    local p
    p="$(printf '%s' "$exec_line" | grep -oP -- '--web-port[= ]\K[0-9]+' || true)"
    [ -n "$p" ] && WEB_PORT="$p"

    local out
    out="$( cd "$APP_DIR" && "$py" - <<'PY' 2>/dev/null
from pathlib import Path
from tgrelay.config import load_config
c = load_config("config.yaml")
print("SOURCE=" + (str(c.sources[0]) if c.sources else ""))
print("TARGETS_RAW=" + ",".join(str(t.id) for t in c.targets))
if c.targets:
    iv = c.targets[0].interval or c.rate.per_target_interval
    print("GROUP_INTERVAL=" + str(int(iv[0])))
r = c.repost
spec = []
for start, end in r.ranges:
    spec.append(str(start) if start == end else f"{start}-{end}")
spec.extend(str(i) for i in r.ids)
print("MATERIALS=" + ",".join(spec))
print("REPOST_INTERVAL=" + str(int(r.interval or 300)))
print("DAILY_LIMIT=" + str(int(r.daily_limit)))
print("PREMIUM=" + ("yes" if c.premium else "no"))
PY
    )"

    # 令牌也一起读回来：真实部署里它们常在 systemd 的 drop-in 里而不在 .env，
    # 不读回来的话「修改配置」按回车会把 .env 里的令牌清空。
    local saved_tokens
    saved_tokens="$( cd "$APP_DIR" && "$py" - "$SERVICE" <<'PY' 2>/dev/null
import sys
from pathlib import Path
from tgrelay.panel import _discover_env
svc = sys.argv[1]
for name in ("TG_WEB_TOKEN", "TG_BOT_TOKEN", "TG_BOT_ADMINS"):
    print(name + "=" + _discover_env(name, svc, Path(".")))
PY
    )"
    local line
    while IFS= read -r line; do
        case "$line" in
            TG_WEB_TOKEN=*)  WEB_TOKEN="${line#TG_WEB_TOKEN=}" ;;
            TG_BOT_TOKEN=*)  BOT_TOKEN="${line#TG_BOT_TOKEN=}" ;;
            TG_BOT_ADMINS=*) BOT_ADMINS="${line#TG_BOT_ADMINS=}" ;;
        esac
    done <<< "$saved_tokens"

    local key value
    while IFS='=' read -r key value; do
        case "$key" in
            SOURCE)          [ -n "$value" ] && SOURCE="$value" ;;
            TARGETS_RAW)     [ -n "$value" ] && TARGETS_RAW="$value" ;;
            GROUP_INTERVAL)  [ -n "$value" ] && GROUP_INTERVAL="$value" ;;
            MATERIALS)       MATERIALS="$value" ;;
            REPOST_INTERVAL) [ -n "$value" ] && REPOST_INTERVAL="$value" ;;
            DAILY_LIMIT)     [ -n "$value" ] && DAILY_LIMIT="$value" ;;
            PREMIUM)         [ -n "$value" ] && PREMIUM="$value" ;;
        esac
    done <<< "$out"

    info "已读回现有配置：源 ${SOURCE:-?}，目标 ${TARGETS_RAW:-?}，模式 ${MODE}，素材 ${MATERIALS:-无}"
}

menu() {
    while true; do
        banner
        local installed=no
        systemctl cat "$SERVICE" >/dev/null 2>&1 && installed=yes
        if [ "$installed" = yes ]; then
            local state
            state="$(systemctl is-active "$SERVICE" 2>/dev/null || echo unknown)"
            printf '  当前状态：%s    %s\n' "$state" "$APP_DIR"
        else
            printf '  %s\n' "${C_DIM}未安装 —— 选 1 开始安装${C_OFF}"
        fi
        hr
        echo "   1. 安装 / 重装"
        echo "   2. 修改配置（引导式，改完自动重启）"
        echo "   3. 查看状态"
        echo "   4. 查看日志"
        echo "   5. 更新代码"
        echo "   6. 重启服务"
        echo "   7. 停止服务"
        echo "   8. 账号自检（是否被 Telegram 限制）"
        echo "   9. 打开文字面板"
        echo "  10. 卸载"
        hr
        echo "   0. 退出"
        echo
        case "$(ask '请输入选项' 0)" in
            1) do_install ;;
            2)
                require_root
                if ! systemctl cat "$SERVICE" >/dev/null 2>&1; then
                    warn "还没安装"; pause_any; continue
                fi
                # 先读回当前值当默认值，再走向导（否则改一个数要重填全部）
                read_current_settings
                if [ -f "$APP_DIR/.env" ]; then
                    API_ID="$(grep -oP 'TG_API_ID=\K.*'   "$APP_DIR/.env" | tr -d '"' || true)"
                    API_HASH="$(grep -oP 'TG_API_HASH=\K.*' "$APP_DIR/.env" | tr -d '"' || true)"
                    WEB_TOKEN="$(grep -oP 'TG_WEB_TOKEN=\K.*' "$APP_DIR/.env" | tr -d '"' || true)"
                    BOT_TOKEN="$(grep -oP 'TG_BOT_TOKEN=\K.*' "$APP_DIR/.env" | tr -d '"' || true)"
                    BOT_ADMINS="$(grep -oP 'TG_BOT_ADMINS=\K.*' "$APP_DIR/.env" | tr -d '"' || true)"
                fi
                guided_config
                write_env_file
                write_config_file
                write_service
                start_service
                ;;
            3) show_status ;;
            4) show_logs ;;
            5) require_root; update_code ;;
            6) require_root; systemctl restart "$SERVICE" && ok "已重启" ;;
            7) require_root; systemctl stop "$SERVICE" && ok "已停止" ;;
            8) account_check ;;
            9) open_panel ;;
            10) require_root; uninstall_app ;;
            0) echo; printf '  再见\n\n'; exit 0 ;;
            *) warn "没有这个选项" ;;
        esac
        pause_any
    done
}

# ------------------------------ 入口 ----------------------------------------

case "${1:-menu}" in
    install)    do_install ;;
    menu)       menu ;;
    status)     show_status ;;
    log|logs)   show_logs ;;
    account)    account_check ;;
    panel)      open_panel ;;
    uninstall)  require_root; uninstall_app ;;
    --self-test|self-test) self_test ;;
    --dry-run)  do_install ;;
    -h|--help|help)
        sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//'
        ;;
    *)          menu ;;
esac
