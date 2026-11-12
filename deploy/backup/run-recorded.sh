#!/usr/bin/env bash
# Шаг бэкапа с записью в журнал backup_runs («Служебное → Бэкапы»):
#   run-recorded.sh <что> <куда> <скрипт шага> [аргументы]
# Шаг — отдельный процесс: его ошибка не обрывает остальные шаги ночи, а
# становится строкой «✗ Ошибка» с последними строками вывода. Размер, сообщение
# и подробности шаг кладёт файлами в $STEP_OUT.
source /usr/local/lib/seo-backup/lib.sh

kind=$1
target=$2
shift 2
id=$(run_start "$kind" "$target")
STEP_OUT=$(mktemp -d "$SCRATCH/step.XXXXXX")
export STEP_OUT
log "начало: $kind → $target (запись $id)"
if "$@" >"$STEP_OUT/log" 2>&1; then
    status=ok
    message=$(cat "$STEP_OUT/message" 2>/dev/null || true)
else
    status=failed
    message=$(grep -v '^[[:space:]]*$' "$STEP_OUT/log" | tail -n 3 | cut -c1-300 | paste -sd ' ' -)
fi
cat "$STEP_OUT/log" >&2
run_finish "$id" "$status" "$(cat "$STEP_OUT/size" 2>/dev/null || true)" "$message" \
    "$(cat "$STEP_OUT/details" 2>/dev/null || true)"
rm -rf "$STEP_OUT"
log "конец: $kind → $target — $status"
[ "$status" = ok ]
