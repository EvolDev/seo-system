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
if ! { has_ok_run dump b2 && has_ok_run dump r2 && has_ok_run uploads b2 \
    && has_ok_run uploads r2; }; then
    "$LIB/nightly.sh" || true
fi
if ! { has_ok_run restore_test b2 && has_ok_run restore_test r2; }; then
    "$LIB/weekly.sh" || true
fi
log "бэкапы по расписанию: $LIB/crontab"
exec supercronic "$LIB/crontab"
