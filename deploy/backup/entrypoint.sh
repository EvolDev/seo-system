#!/usr/bin/env bash
# Контейнер backup: дождаться базы, при первом запуске сервера сразу сделать
# снимок, ночную копию и проверку восстановления — защита с первого часа, а не
# с первой ночи; дальше — расписание (crontab).
source /usr/local/lib/seo-backup/lib.sh

until psql -XAtq -c "SELECT 1 FROM backup_runs LIMIT 1" >/dev/null 2>&1; do
    log "жду базу с таблицей backup_runs (миграции применены?)"
    sleep 30
done
has_ok_run base b2 || "$LIB/run-recorded.sh" base b2 "$LIB/step-base.sh" || true
# Хранилище без удачной копии или проверки — сразу: и на новом сервере, и когда
# второе хранилище только что включили.
missing_copy="" missing_test=""
for target in $(copy_targets); do
    { has_ok_run dump "$target" && has_ok_run uploads "$target"; } || missing_copy=yes
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
