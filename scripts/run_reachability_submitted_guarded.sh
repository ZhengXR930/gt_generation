#!/usr/bin/env bash
set -euo pipefail

REPO=/data00/home/zhengxinran/Documents/trae_projects/test/gt_generation
QUEUE=${QUEUE:-/tmp/reachability_gt_submitted_missing_queue_current.tsv}
LOG=${LOG:-$REPO/poc_generation/poc_results/reachability_submitted_guarded_$(date +%Y%m%d_%H%M%S).log}
MIN_AVAIL_KB=${MIN_AVAIL_KB:-$((1600 * 1024 * 1024))}
TIMEOUT=${TIMEOUT:-240}
MAX_HITS_PER_EVENT=${MAX_HITS_PER_EVENT:-16}
DEBUGGER_IMAGE=${DEBUGGER_IMAGE:-gt-memory-env:latest}
LOCK=/tmp/gt_reachability_submitted_guarded.lock

cd "$REPO"

if ! mkdir "$LOCK" 2>/dev/null; then
  echo "[$(date -Is)] another reachability runner appears active: $LOCK"
  exit 0
fi
trap 'rmdir "$LOCK" 2>/dev/null || true' EXIT

mkdir -p "$(dirname "$LOG")"
echo "[$(date -Is)] START queue=$QUEUE log=$LOG min_avail_kb=$MIN_AVAIL_KB timeout=$TIMEOUT" | tee -a "$LOG"

if [ ! -s "$QUEUE" ]; then
  echo "[$(date -Is)] STOP queue missing or empty: $QUEUE" | tee -a "$LOG"
  exit 0
fi

processed=0
skipped=0
while IFS=$'\t' read -r model sample backend dedup; do
  [ -n "${model:-}" ] || continue
  sample_dir="$REPO/poc_generation/poc_results/$model/$sample"
  if [ -f "$sample_dir/reachability_eval.json" ]; then
    skipped=$((skipped + 1))
    echo "[$(date -Is)] SKIP existing $model/$sample skipped=$skipped" | tee -a "$LOG"
    continue
  fi

  avail=$(df -Pk /data00 | awk 'NR==2 {print $4}')
  if [ "${avail:-0}" -lt "$MIN_AVAIL_KB" ]; then
    echo "[$(date -Is)] STOP low disk avail_kb=$avail threshold=$MIN_AVAIL_KB processed=$processed skipped=$skipped" | tee -a "$LOG"
    exit 0
  fi

  processed=$((processed + 1))
  echo "[$(date -Is)] START_SAMPLE index=$processed model=$model sample=$sample backend=$backend dedup=$dedup avail_kb=$avail" | tee -a "$LOG"
  PYTHONPATH=evaluator python3 evaluator/reachability/eval_batch.py \
    --model "$model" \
    --sample-id "$sample" \
    --timeout "$TIMEOUT" \
    --debugger-image "$DEBUGGER_IMAGE" \
    --max-hits-per-event "$MAX_HITS_PER_EVENT" >> "$LOG" 2>&1 || true
  echo "[$(date -Is)] END_SAMPLE index=$processed model=$model sample=$sample" | tee -a "$LOG"

  docker ps -aq --filter status=exited | xargs -r docker rm -f >> "$LOG" 2>&1 || true
  df -h /data00 >> "$LOG" 2>&1 || true
  docker system df >> "$LOG" 2>&1 || true
done < "$QUEUE"

echo "[$(date -Is)] DONE processed=$processed skipped=$skipped" | tee -a "$LOG"
