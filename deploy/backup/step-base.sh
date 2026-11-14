#!/usr/bin/env bash
# Полный снимок базы в B2 (WAL-G): от него архив изменений восстанавливает
# базу на любую минуту. Файлы живой базы читаются, база при этом работает.
source /usr/local/lib/seo-backup/lib.sh

log "полный снимок базы → B2"
wal-g backup-push "$LIVE_PGDATA"
info=$(wal-g backup-list --detail --json | jq -c 'max_by(.start_time)')
jq -r '.compressed_size' <<<"$info" >"$STEP_OUT/size"
jq -r '"снимок \(.backup_name)"' <<<"$info" >"$STEP_OUT/message"
jq -c '{backup_name, start_time, finish_time, compressed_size, uncompressed_size}' \
    <<<"$info" >"$STEP_OUT/details"
