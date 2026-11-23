#!/usr/bin/env bash
# Geekbench 6 单核成绩测试：Ubuntu 虚拟机与 GitHub Actions runner 通用。
#
# 用法：
#   bash geekbench_single.sh
#   GB_VERSION=6.6.0 RUNS=2 UPLOAD=0 bash geekbench_single.sh
#
# 环境变量：
#   GB_VERSION  版本号，默认 6.6.0
#   RUNS        重复次数（取最高分），默认 1
#   UPLOAD      1 = 上传结果到 browser.geekbench.com，默认 0（尝试 --no-upload）
#   WORK        工作目录，默认 ./.geekbench
#
# 注意：Geekbench 没有“只跑单核”的 CLI 参数，一次运行会跑完整 CPU 测试
#       （约 6~10 分钟），单核分数从输出里提取。
set -euo pipefail

GB_VERSION="${GB_VERSION:-6.6.0}"
RUNS="${RUNS:-1}"
UPLOAD="${UPLOAD:-0}"
WORK="${WORK:-$PWD/.geekbench}"
RESULT_FILE="${RESULT_FILE:-$PWD/geekbench-result.txt}"

echo "==== Geekbench 6 单核测试 ===="
echo "版本       : $GB_VERSION"
echo "重复次数   : $RUNS"
echo "上传结果   : $UPLOAD"
if [ -r /proc/cpuinfo ]; then
  echo "CPU        : $(grep -m1 'model name' /proc/cpuinfo | cut -d: -f2- | sed 's/^ *//')"
fi
echo "逻辑核心   : $(nproc 2>/dev/null || echo n/a)"
echo "内核       : $(uname -sr)"
echo

mkdir -p "$WORK"
cd "$WORK"

TARBALL="Geekbench-${GB_VERSION}-Linux.tar.gz"
DIR="Geekbench-${GB_VERSION}-Linux"
if [ ! -f "$DIR/geekbench_x86_64" ] && [ ! -f "$DIR/geekbench_aarch64" ] && [ ! -f "$DIR/geekbench6" ]; then
  echo "下载 $TARBALL ..."
  curl -fL --retry 3 --retry-delay 2 -o "$TARBALL" "https://cdn.geekbench.com/$TARBALL"
  echo "解压 ..."
  tar xzf "$TARBALL"
fi

BIN=""
for cand in "$DIR/geekbench_x86_64" "$DIR/geekbench_aarch64" "$DIR/geekbench6"; do
  if [ -f "$cand" ]; then
    BIN="$cand"
    break
  fi
done
if [ -z "$BIN" ]; then
  echo "未找到 Geekbench 可执行文件，目录内容：" >&2
  ls -l "$DIR" >&2
  exit 1
fi
chmod +x "$BIN"
BIN="$(basename "$BIN")"

cd "$DIR"
for i in $(seq 1 "$RUNS"); do
  echo "---- 第 $i/$RUNS 次 ----"
  set +e
  if [ "$UPLOAD" = "1" ]; then
    timeout 1800 ./"$BIN" --cpu 2>&1 | tee "gb-run-$i.log"
  else
    timeout 1800 ./"$BIN" --cpu --no-upload 2>&1 | tee "gb-run-$i.log"
  fi
  rc=${PIPESTATUS[0]}
  set -e
  if [ "$rc" -ne 0 ] && [ "$UPLOAD" != "1" ]; then
    echo "带 --no-upload 运行失败（rc=$rc），改为默认参数重试 ..."
    timeout 1800 ./"$BIN" --cpu 2>&1 | tee "gb-run-$i.log"
  fi
done

# 从日志中提取分数（取多次运行的最高值）
best_single=0
best_multi=0
for f in gb-run-*.log; do
  s=$(grep -aoE 'Single-Core Score[[:space:]]+[0-9]+' "$f" | head -1 | grep -oE '[0-9]+$' || true)
  m=$(grep -aoE 'Multi-Core Score[[:space:]]+[0-9]+' "$f" | head -1 | grep -oE '[0-9]+$' || true)
  if [ -n "${s:-}" ] && [ "$s" -gt "$best_single" ]; then
    best_single=$s
  fi
  if [ -n "${m:-}" ] && [ "$m" -gt "$best_multi" ]; then
    best_multi=$m
  fi
done

# 兜底：日志没解析到时，从 Geekbench 生成的 JSON 结果里找
if [ "$best_single" -eq 0 ]; then
  best_single=$(python3 - <<'PY' || echo 0
import glob, json
best = 0
for path in glob.glob("*.json") + glob.glob("*.geekbench6"):
    try:
        data = json.load(open(path, encoding="utf-8", errors="ignore"))
    except Exception:
        continue
    stack = [data]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            if str(cur.get("name", "")).lower() == "single-core":
                score = cur.get("score")
                if isinstance(score, (int, float)):
                    best = max(best, int(score))
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
print(best)
PY
)
fi

echo
echo "==== 结果 ===="
echo "Single-Core : $best_single"
echo "Multi-Core  : $best_multi"
echo "最高单核分  : $best_single"

{
  echo "GB_VERSION=$GB_VERSION"
  echo "runs=$RUNS"
  echo "cpu=$(grep -m1 'model name' /proc/cpuinfo 2>/dev/null | cut -d: -f2- | sed 's/^ *//' || echo unknown)"
  echo "nproc=$(nproc 2>/dev/null || echo 0)"
  echo "single_core=$best_single"
  echo "multi_core=$best_multi"
} > "$RESULT_FILE"

if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
  {
    echo "## Geekbench 6 单核"
    echo ""
    echo "| 项目 | 分数 |"
    echo "|---|---|"
    echo "| Single-Core | **$best_single** |"
    echo "| Multi-Core | $best_multi |"
  } >> "$GITHUB_STEP_SUMMARY"
fi

echo "结果已写入 $RESULT_FILE"
