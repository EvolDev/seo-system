#!/usr/bin/env bash
# Копия базы на минуту в прошлом — рядом с живой, чтобы достать испорченное
# или удалённое, не трогая работу остальных (docs/17-OPERATIONS.md):
#   restore-copy.sh "ГГГГ-ММ-ДД ЧЧ:ММ"   — поднять копию
#   restore-copy.sh --stop               — остановить и удалить
# Копия — временный сервер в контейнере backup, только сокет, без сети:
#   docker compose exec backup psql -h /scratch/sock -p 5433 -d <база>
source /usr/local/lib/seo-backup/lib.sh

dir="$SCRATCH/copy"
if [ "${1:-}" = --stop ]; then
    temp_stop "$dir"
    rm -rf "$dir" "$dir.log"
    log "копия остановлена и удалена"
    exit 0
fi
[ -n "${1:-}" ] || die "укажите время: restore-copy.sh \"2026-11-20 14:05\""
target=$(date -d "$1" +%s) || die "не понимаю время «$1»"
[ "$target" -le "$(date +%s)" ] || die "$1 — ещё не наступило"
temp_stop "$dir"
rm -rf "$dir" "$dir.log"
# Изменения до текущей минуты — в архив: иначе последних минут в нём ещё нет.
flush_archive
"$LIB/restore.sh" "$dir" "$1"
temp_start "$dir"
wait_promoted "$dir"
log "копия на $1 открыта: psql -h $TEMP_SOCKET -p $TEMP_PORT -d $PGDATABASE; убрать — restore-copy.sh --stop"
