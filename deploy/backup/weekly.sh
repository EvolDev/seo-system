#!/usr/bin/env bash
# Еженедельная проверка восстановления: бэкап, который не восстанавливали, не
# существует. Обе проверки — во временные базы в /scratch, живую не трогают.
source /usr/local/lib/seo-backup/lib.sh

status=0
"$LIB/run-recorded.sh" restore_test b2 "$LIB/step-restore-b2.sh" || status=1
"$LIB/run-recorded.sh" restore_test r2 "$LIB/step-restore-r2.sh" || status=1
exit "$status"
