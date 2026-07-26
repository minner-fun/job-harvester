#!/usr/bin/env bash
# 提交前的敏感信息检查。**这是公开仓库**，凭证、主机标识、个人资料都不该进历史。
#
#   scripts/audit-staged.sh          # 检查已暂存的改动
#   scripts/audit-staged.sh --all    # 检查工作树全部被跟踪的文件
#
# 命中任何一条即以非 0 退出，方便串在提交前面：
#   scripts/audit-staged.sh && git commit ...
#
# 敏感模式从 .env 现有的值动态生成 —— 硬编码一份清单，改了配置就会漏检。
set -uo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR" || exit 1

SELF="scripts/audit-staged.sh"

if [ "${1:-}" = "--all" ]; then
    CONTENT="$(git grep -In '' -- . ":(exclude)$SELF" 2>/dev/null)"
    SCOPE="工作树"
else
    # 只看新增行：被删除的行（- 前缀）正是我们想要的结果，不该报警。
    # 同时排除本脚本自身 —— 它的正则定义会命中自己。
    CONTENT="$(git diff --cached -- . ":(exclude)$SELF" 2>/dev/null \
               | grep -E '^\+' | grep -vE '^\+\+\+')"
    SCOPE="已暂存的新增内容"
fi

if [ -z "$CONTENT" ]; then
    echo "没有需要检查的内容（$SCOPE 为空）"
    exit 0
fi

FOUND=0
report() { echo "  ⚠ $1"; FOUND=1; }

# --- 从 .env 提取真实值，逐个反查 -------------------------------------
if [ -f .env ]; then
    while IFS= read -r line; do
        key="${line%%=*}"; val="${line#*=}"
        val="$(printf '%s' "$val" | tr -d '"'"'"'' | tr -d '\r')"
        # 跳过空值与不敏感的通用配置
        case "$key" in ''|\#*|USER_AGENT|REQUEST_DELAY|CONCURRENCY|HTTP_TIMEOUT) continue ;; esac
        [ ${#val} -lt 4 ] && continue
        # DATABASE_URL 只取密码段，整串包含 127.0.0.1 之类会误报
        if [ "$key" = "DATABASE_URL" ]; then
            val="$(printf '%s' "$val" | sed -n 's|.*://[^:]*:\([^@]*\)@.*|\1|p')"
            [ -z "$val" ] && continue
        fi
        if printf '%s' "$CONTENT" | grep -qF -- "$val"; then
            report "$SCOPE 中出现了 .env 里 $key 的值"
        fi
    done < <(grep -vE '^\s*#' .env 2>/dev/null)
fi

# --- 通用模式 ---------------------------------------------------------
check() {
    if printf '%s' "$CONTENT" | grep -qiE -- "$1"; then
        report "$2"
    fi
}
check '(BEGIN [A-Z ]*PRIVATE KEY|ssh-rsa AAAA)'        '疑似私钥'
check '\b([0-9]{1,3}\.){3}[0-9]{1,3}\b.*(ssh|host|server)' '疑似公网主机地址'
check '(password|passwd|secret|api[_-]?key|token)\s*=\s*["'"'"']?[A-Za-z0-9/+_-]{16,}' '疑似硬编码凭证'
check '/(home|root|Users)/[a-z0-9_.-]+/' '绝对家目录路径（换成相对路径或占位符）'

echo
if [ "$FOUND" -eq 1 ]; then
    echo "❌ 检查未通过 —— 这是公开仓库，请先处理上面的问题"
    exit 1
fi
echo "✓ $SCOPE 未发现敏感信息"
