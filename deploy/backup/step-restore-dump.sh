#!/usr/bin/env bash
# Проверка ночной копии в хранилище: step-restore-dump.sh <b2|r2>.
# Последний ночной дамп — во временную базу, сверка с числами строк на момент
# дампа; архив файлов загрузок — целый, файлов столько же. Для R2 — вся его
# проверка, для B2 — вторая половина после архива на минуту (step-restore-b2.sh).
source /usr/local/lib/seo-backup/lib.sh
rclone_env

target=${1:?укажите хранилище: b2 или r2}
name=${target^^}
dir="$SCRATCH/dumpcheck"
data="$dir/data"
temp_stop "$data"
rm -rf "$dir"
mkdir -p "$dir"
guard_target_dir "$data"
trap 'temp_stop "$data"; rm -rf "$dir"' EXIT

dump=$(rclone lsf "$target:db" --files-only | sort | tail -n 1)
[ -n "$dump" ] || die "в $name нет ни одного ночного дампа"
files=$(rclone lsf "$target:uploads" --files-only | sort | tail -n 1)
[ -n "$files" ] || die "в $name нет ни одного архива файлов загрузок"
log "скачиваю из $name: $dump, $files"
rclone copyto "$target:db/$dump" "$dir/seo.dump"
rclone copyto "$target:uploads/$files" "$dir/uploads.tar"

# Числа строк на момент дампа — из журнала; нет — с живой базы.
expected=$(psql -XAtq -v ON_ERROR_STOP=1 -v file="$dump" -v target="$target" <<'SQL'
SELECT details -> 'counts' FROM backup_runs
WHERE kind = 'dump' AND target = :'target' AND status = 'ok' AND details ->> 'file' = :'file'
ORDER BY started_at DESC LIMIT 1;
SQL
)
if [ -z "$expected" ] || [ "$expected" = null ]; then
    expected=$(table_counts)
fi

started=$(date +%s)
initdb -D "$data" --username="$PGUSER" --auth-local=trust --encoding=UTF8 >/dev/null
temp_start "$data"
temp_psql -d postgres -c "CREATE DATABASE restore_check" >/dev/null
pg_restore --no-owner --no-acl --exit-on-error -h "$TEMP_SOCKET" -p "$TEMP_PORT" -U "$PGUSER" \
    -d restore_check "$dir/seo.dump"
actual=$(table_counts temp_psql -d restore_check)
mismatches=$(compare_counts "$expected" "$actual")
took=$(($(date +%s) - started))

# Файлы загрузок: архив читается целиком, файлов — сколько было при упаковке.
in_archive=$(tar -tf "$dir/uploads.tar" | grep -vc '/$' || true)
packed=$(psql -XAtq -v ON_ERROR_STOP=1 -v file="$files" -v target="$target" <<'SQL'
SELECT details ->> 'counts' FROM backup_runs
WHERE kind = 'uploads' AND target = :'target' AND status = 'ok' AND details ->> 'file' = :'file'
ORDER BY started_at DESC LIMIT 1;
SQL
)
size=$(($(stat -c %s "$dir/seo.dump") + $(stat -c %s "$dir/uploads.tar")))
tables=$(jq length <<<"$expected")
jq -cn --arg dump "$dump" --arg files "$files" --argjson tables "$tables" \
    --argjson seconds "$took" --argjson mismatches "$mismatches" \
    --argjson in_archive "$in_archive" --arg packed "$packed" \
    '{dump: $dump, files_archive: $files, tables: $tables, restore_seconds: $seconds,
      mismatches: $mismatches, files_in_archive: $in_archive, files_packed: $packed}' \
    >"$STEP_OUT/details"
echo "$size" >"$STEP_OUT/size"
[ "$mismatches" = "[]" ] \
    || die "дамп $dump расходится с базой: $(jq -r 'join("; ")' <<<"$mismatches")"
if [ -n "$packed" ] && [ "$in_archive" != "$packed" ]; then
    die "в архиве $files файлов $in_archive, а упаковано было $packed"
fi
echo "дамп $dump из $name восстановлен за $took с: $tables таблиц сходятся; в архиве файлов\
 загрузок $in_archive файлов, все читаются" >"$STEP_OUT/message"
