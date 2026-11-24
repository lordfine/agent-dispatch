#!/usr/bin/env bash
# 全量测试排队：一台测试机同一时间只跑一个全量，多个会话靠这把锁排队。变量名只用英文。
# 用法：
#   bash 全量排队.sh --status
#   bash 全量排队.sh [--no-wait | --wait-minutes N] [--label 说明] [--preflight 预检脚本] [--allow-merge-in-progress] -- <全量命令及参数>
# --no-wait：名额被占就立即退出 75，不跑（大档票交付前用）。默认最多等 180 分钟（集成前、发版前用）。
# 命令运行时有环境变量 FULL_TEST_RUN_ID（每次不同），日志、junit、临时目录用它拼，不用固定名。
# 锁文件默认 /tmp/dispatch-full-test.lock，可用环境变量 FULL_TEST_LOCK 改；同一台机器上的会话要用同一个。
# 起跑前预检（排队前跑，没过直接退出 3，不占名额）：
#   1. 当前目录有未提交的合并（MERGE_HEAD 存在）就拒跑——全量只在合并提交之后判（实战项目批14：依赖 HEAD 的测试在未提交合流状态下必挂）。
#      确实要在合并中途跑，加 --allow-merge-in-progress。
#   2. 项目预检：--preflight <脚本>；不写时自动找当前目录的 .dispatch/全量预检.sh 或 .dispatch/全量预检.py。
#      项目在里面查测试库迁移版本是否等于 head、构建占位、依赖是否装齐等，没过就非零退出并打印怎么补。
set -u

lock_file="${FULL_TEST_LOCK:-/tmp/dispatch-full-test.lock}"
holder_file="${lock_file}.holder"
mode="wait"
wait_minutes=180
label=""
preflight=""
allow_merge=0

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
    --preflight)
      if [ -z "${2:-}" ]; then echo "--preflight 后面要跟预检脚本路径" >&2; exit 2; fi
      preflight="$2"; shift 2 ;;
    --allow-merge-in-progress) allow_merge=1; shift ;;
    --) shift; break ;;
    *) echo "未知参数：$1（要跑的命令写在 -- 后面）" >&2; exit 2 ;;
  esac
done

if [ $# -eq 0 ]; then
  echo "缺少要跑的全量命令，写在 -- 后面" >&2
  exit 2
fi

merge_head_file="$(git rev-parse --git-path MERGE_HEAD 2>/dev/null || true)"
if [ "$allow_merge" -ne 1 ] && [ -n "$merge_head_file" ] && [ -f "$merge_head_file" ]; then
  echo "当前目录有未提交的合并（MERGE_HEAD 存在），这次不跑：先提交合并再排全量；确实要跑加 --allow-merge-in-progress" >&2
  exit 3
fi

if [ -z "$preflight" ]; then
  for candidate in .dispatch/全量预检.sh .dispatch/全量预检.py; do
    if [ -f "$candidate" ]; then preflight="$candidate"; break; fi
  done
fi
if [ -n "$preflight" ]; then
  if [ ! -f "$preflight" ]; then echo "预检脚本不存在：$preflight" >&2; exit 2; fi
  echo "跑起跑前预检：$preflight"
  case "$preflight" in
    *.py) runner="$(command -v python3 || command -v python)"; "$runner" "$preflight" ;;
    *) bash "$preflight" ;;
  esac
  preflight_status=$?
  if [ "$preflight_status" -ne 0 ]; then
    echo "预检没过（退出码 $preflight_status），这次不跑全量，按上面的提示补好再排" >&2
    exit 3
  fi
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
