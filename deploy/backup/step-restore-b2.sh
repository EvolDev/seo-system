#!/usr/bin/env bash
# Проверка архива B2: поднять базу из полного снимка и архива изменений на
# минуту перед проверкой — во временную папку, не трогая живую, — и сверить
# с живой число строк каждой таблицы. Заодно — что ночной дамп в B2 читается.
source /usr/local/lib/seo-backup/lib.sh
rclone_env

dir="$SCRATCH/pitr"
temp_stop "$dir"
rm -rf "$dir" "$SCRATCH/b2check"
guard_target_dir "$dir"
trap 'temp_stop "$dir"; rm -rf "$dir" "$dir.log" "$SCRATCH/b2check"' EXIT

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

# Ночной дамп в B2 скачивается, расшифровывается и читается.
mkdir -p "$SCRATCH/b2check"
dump=$(rclone lsf b2:db --files-only | sort | tail -n 1)
[ -n "$dump" ] || die "в B2 нет ни одного ночного дампа"
rclone copyto "b2:db/$dump" "$SCRATCH/b2check/seo.dump"
pg_restore -l "$SCRATCH/b2check/seo.dump" >/dev/null || die "дамп $dump из B2 не читается"

tables=$(jq length <<<"$expected")
jq -cn --arg point "$point" --arg backup "$backup" --arg dump "$dump" \
    --argjson tables "$tables" --argjson seconds "$took" --argjson mismatches "$mismatches" \
    '{point: $point, base_backup: $backup, tables: $tables, recovery_seconds: $seconds,
      mismatches: $mismatches, b2_dump: $dump}' >"$STEP_OUT/details"
echo "$size" >"$STEP_OUT/size"
[ "$mismatches" = "[]" ] \
    || die "после восстановления расходятся таблицы: $(jq -r 'join("; ")' <<<"$mismatches")"
echo "из архива B2 база поднялась на минуту перед проверкой за $took с: $tables таблиц\
 сходятся; дамп $dump читается" >"$STEP_OUT/message"
