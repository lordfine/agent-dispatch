#!/usr/bin/env bash
# 全量测试排队：一台测试机同一时间只跑一个全量，多个会话靠这把锁排队。变量名只用英文。
# 用法：
#   bash 全量排队.sh --status
#   bash 全量排队.sh [--no-wait | --wait-minutes N] [--label 说明] -- <全量命令及参数>
# --no-wait：名额被占就立即退出 75，不跑（大档票交付前用）。默认最多等 180 分钟（集成前、发版前用）。
# 命令运行时有环境变量 FULL_TEST_RUN_ID（每次不同），日志、junit、临时目录用它拼，不用固定名。
# 锁文件默认 /tmp/dispatch-full-test.lock，可用环境变量 FULL_TEST_LOCK 改；同一台机器上的会话要用同一个。
set -u

lock_file="${FULL_TEST_LOCK:-/tmp/dispatch-full-test.lock}"
holder_file="${lock_file}.holder"
mode="wait"
wait_minutes=180
label=""

if ! command -v flock >/dev/null 2>&1; then
  echo "这台机器没有 flock，无法排队（WSL 和 Linux 自带）" >&2
  exit 2
fi

show_holder() {
  if [ -s "$holder_file" ]; then cat "$holder_file"; else echo "（没有登记占用信息）"; fi
}

while [ $# -gt 0 ]; do
  case "$1" in
    --status)
      exec 8>>"$lock_file"
      if flock -n 8; then echo "空闲"; else echo "占用中："; show_holder; fi
      exit 0 ;;
    --no-wait) mode="nowait"; shift ;;
    --wait-minutes)
      case "${2:-}" in ''|*[!0-9]*) echo "--wait-minutes 后面要跟分钟数" >&2; exit 2 ;; esac
      wait_minutes="$2"; shift 2 ;;
    --label) label="${2:-}"; shift 2 ;;
    --) shift; break ;;
    *) echo "未知参数：$1（要跑的命令写在 -- 后面）" >&2; exit 2 ;;
  esac
done

if [ $# -eq 0 ]; then
  echo "缺少要跑的全量命令，写在 -- 后面" >&2
  exit 2
fi

exec 9>>"$lock_file"
if ! flock -n 9; then
  if [ "$mode" = "nowait" ]; then
    echo "全量名额被占，这次不跑："
    show_holder
    exit 75
  fi
  echo "全量名额被占，排队等待（最多 ${wait_minutes} 分钟）："
  show_holder
  if ! flock -w $((wait_minutes * 60)) 9; then
    echo "等了 ${wait_minutes} 分钟仍被占，这次不跑" >&2
    exit 75
  fi
fi

FULL_TEST_RUN_ID="$(date +%Y%m%d-%H%M%S)-$$"
export FULL_TEST_RUN_ID
printf '开始：%s\n运行号：%s\n目录：%s\n说明：%s\n命令：%s\n' "$(date '+%F %T')" "$FULL_TEST_RUN_ID" "$PWD" "$label" "$*" > "$holder_file"
echo "拿到全量名额，运行号 $FULL_TEST_RUN_ID"

"$@" 9>&-
status=$?

: > "$holder_file"
echo "全量命令结束，退出码 $status"
exit "$status"
