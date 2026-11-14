#!/usr/bin/env bash
# Ночная копия: дамп базы и файлы загрузок — в B2 и, если включено второе
# хранилище, в R2 у другой компании (BACKUP_R2_ENABLED), зашифрованные. Каждая
# отправка — своя строка журнала.
source /usr/local/lib/seo-backup/lib.sh

day=$(date +%F-%H%M)
dir="$SCRATCH/nightly"
rm -rf "$dir"
mkdir -p "$dir"
dump="$dir/seo-$day.dump"
files="$dir/uploads-$day.tar"

# Числа строк — рядом с дампом: проверка восстановления сверит с ними.
table_counts >"$dir/dump.counts" 2>"$dir/dump.err" || true
log "ночной дамп базы"
pg_dump -Fc -f "$dump" 2>>"$dir/dump.err" || rm -f "$dump"
log "архив файлов загрузок"
find /uploads -type f | wc -l >"$dir/uploads.counts"
tar -C /uploads -cf "$files" . 2>"$dir/uploads.err" || rm -f "$files"

status=0
for target in $(copy_targets); do
    "$LIB/run-recorded.sh" dump "$target" "$LIB/step-upload.sh" "$target" db "$dump" || status=1
    "$LIB/run-recorded.sh" uploads "$target" "$LIB/step-upload.sh" "$target" uploads "$files" \
        || status=1
done
rm -rf "$dir"
# Хранилище на самом сервере — старое удаляем сами (у Backblaze — правило корзины).
if [ -n "${BACKUP_KEEP_DAYS:-}" ]; then
    "$LIB/prune.sh" || status=1
fi
exit "$status"
