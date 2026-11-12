#!/usr/bin/env bash
# Проверка архива B2: поднять базу из полного снимка и архива изменений на
# минуту перед проверкой — во временную папку, не трогая живую, — и сверить
# с живой число строк каждой таблицы. Затем ночная копия в B2: дамп — во
# временную базу и сверка, архив файлов загрузок читается (step-restore-dump.sh).
source /usr/local/lib/seo-backup/lib.sh
rclone_env

dir="$SCRATCH/pitr"
temp_stop "$dir"
rm -rf "$dir"
guard_target_dir "$dir"
nightly=$(mktemp -d "$SCRATCH/nightly.XXXXXX")
trap 'temp_stop "$dir"; rm -rf "$dir" "$dir.log" "$nightly"' EXIT

# Точка отсчёта: числа строк живой базы и именованная точка восстановления.
point="restore-test-$(date +%Y%m%d-%H%M%S)"
expected=$(table_counts)
psql -XAtq -v ON_ERROR_STOP=1 -v point="$point" <<'SQL' >/dev/null
SELECT pg_create_restore_point(:'point');
SQL
# Журнал с точкой должен уйти в архив.
flush_archive

log "восстановление из B2 на точку $point"
started=$(date +%s)
backup=$(wal-g backup-list --detail --json | jq -r 'max_by(.start_time).backup_name')
wal-g backup-fetch "$dir" "$backup"
cat >>"$dir/postgresql.auto.conf" <<CONF
restore_command = 'wal-g wal-fetch "%f" "%p"'
recovery_target_name = '$point'
recovery_target_action = 'promote'
CONF
touch "$dir/recovery.signal"
temp_start "$dir"
wait_promoted "$dir"
took=$(($(date +%s) - started))

actual=$(table_counts temp_psql -d "$PGDATABASE")
mismatches=$(compare_counts "$expected" "$actual")
temp_stop "$dir"
size=$(du -sb "$dir" | cut -f1)
# Место — под вторую временную базу.
rm -rf "$dir"

# Ночная копия в B2 — та же проверка, что у R2; её итог — внутри этого.
log "ночная копия в B2: дамп во временную базу"
nightly_ok=yes
STEP_OUT=$nightly "$LIB/step-restore-dump.sh" b2 2>"$nightly/err" || nightly_ok=no
cat "$nightly/err" >&2
nightly_details=$(cat "$nightly/details" 2>/dev/null || echo null)
size=$((size + $(cat "$nightly/size" 2>/dev/null || echo 0)))

tables=$(jq length <<<"$expected")
jq -cn --arg point "$point" --arg backup "$backup" --argjson tables "$tables" \
    --argjson seconds "$took" --argjson mismatches "$mismatches" \
    --argjson nightly "$nightly_details" \
    '{point: $point, base_backup: $backup, tables: $tables, recovery_seconds: $seconds,
      mismatches: $mismatches, nightly: $nightly}' >"$STEP_OUT/details"
echo "$size" >"$STEP_OUT/size"
[ "$mismatches" = "[]" ] \
    || die "после восстановления расходятся таблицы: $(jq -r 'join("; ")' <<<"$mismatches")"
if [ "$nightly_ok" != yes ]; then
    # Причина — наша ошибка шага, а если упала сама программа — её последняя строка.
    reason=$(grep 'ОШИБКА: ' "$nightly/err" | tail -n 1 | sed 's/.*ОШИБКА: //' || true)
    [ -n "$reason" ] || reason=$(grep -v '^[[:space:]]*$' "$nightly/err" | tail -n 1 | cut -c1-200 || true)
    die "архив B2 восстановился, а ночная копия в B2 — нет: ${reason:-подробности в журнале}"
fi
echo "из архива B2 база поднялась на минуту перед проверкой за $took с: $tables таблиц\
 сходятся; $(cat "$nightly/message")" >"$STEP_OUT/message"
