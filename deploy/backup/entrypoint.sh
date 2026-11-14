#!/usr/bin/env bash
# Контейнер backup: дождаться базы, при первом запуске сервера сразу сделать
# снимок, ночную копию и проверку восстановления — защита с первого часа, а не
# с первой ночи; дальше — расписание (crontab).
source /usr/local/lib/seo-backup/lib.sh

# Хранилище B2 ещё не настроено (новый сервер) — ждать молча, а не падать каждую
# ночь: dcp up -d поднимает и эту службу (docs/18-SERVER-SETUP.md §11).
if [ -z "${B2_BUCKET:-}" ]; then
    log "бэкапы выключены: в .env нет B2_BUCKET — настройка в docs/18-SERVER-SETUP.md §11"
    # Не exec: sleep главным процессом контейнера не слышит «стоп», и остановка
    # ждала бы 10 с. Оболочка с ловушкой выходит сразу.
    trap 'exit 0' TERM INT
    sleep infinity &
    wait
fi

until psql -XAtq -c "SELECT 1 FROM backup_runs LIMIT 1" >/dev/null 2>&1; do
    log "жду базу с таблицей backup_runs (миграции применены?)"
    sleep 30
done
# Есть ли копии в самом хранилище: журнал помнит прежнее хранилище, а после
# переезда (с хранилища на сервере — в Backblaze) новое пустое.
storage_has_base() {
    local count
    count=$(wal-g backup-list --json 2>/dev/null | jq 'length' 2>/dev/null || true)
    [ "${count:-0}" -gt 0 ] 2>/dev/null
}
storage_has_copy() {
    ( rclone_env && [ -n "$(rclone lsf "$1:db" --files-only 2>/dev/null | head -n 1)" ] )
}

# Нет полного снимка — сразу, и тогда же копия и проверка: от снимка без них
# пользы мало, а у нового хранилища нет и их.
fresh=""
if ! { has_ok_run base b2 && storage_has_base; }; then
    "$LIB/run-recorded.sh" base b2 "$LIB/step-base.sh" || true
    fresh=yes
fi
# Хранилище без удачной копии или проверки — сразу: и на новом сервере, и когда
# второе хранилище только что включили.
missing_copy=$fresh missing_test=$fresh
for target in $(copy_targets); do
    { has_ok_run dump "$target" && has_ok_run uploads "$target" && storage_has_copy "$target"; } \
        || missing_copy=yes
    has_ok_run restore_test "$target" || missing_test=yes
done
if [ -n "$missing_copy" ]; then
    "$LIB/nightly.sh" || true
fi
if [ -n "$missing_test" ]; then
    "$LIB/weekly.sh" || true
fi
log "бэкапы по расписанию: $LIB/crontab"
exec supercronic "$LIB/crontab"
