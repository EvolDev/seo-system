#!/usr/bin/env bash
# Еженедельная проверка восстановления: бэкап, который не восстанавливали, не
# существует. B2 — архив на минуту и ночной дамп, R2 (если включено) — ночной
# дамп; всё — во временные базы в /scratch, живую не трогают.
source /usr/local/lib/seo-backup/lib.sh

status=0
"$LIB/run-recorded.sh" restore_test b2 "$LIB/step-restore-b2.sh" || status=1
if r2_enabled; then
    "$LIB/run-recorded.sh" restore_test r2 "$LIB/step-restore-dump.sh" r2 || status=1
fi
exit "$status"
