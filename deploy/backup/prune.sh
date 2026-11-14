#!/usr/bin/env bash
# Чистка старых копий — для хранилища на самом сервере (BACKUP_KEEP_DAYS, ADR-081):
# у Backblaze старое удаляет правило корзины, а ключам сервера удалять не положено.
# Удаляет копии старше BACKUP_KEEP_DAYS дней: ночные дампы и архивы файлов загрузок;
# полные снимки и архив изменений — с запасом в сутки: WAL-G оставляет первый
# снимок ПОСЛЕ границы, а снимок — каждую ночь, так что база восстанавливается на
# любую минуту за последние N дней.
source /usr/local/lib/seo-backup/lib.sh
rclone_env

days=${BACKUP_KEEP_DAYS:-}
[[ "$days" =~ ^[0-9]+$ ]] && [ "$days" -ge 3 ] \
    || die "BACKUP_KEEP_DAYS — число дней, не меньше 3 (сейчас «${days}»)"
before=$(date -u -d "-$((days + 1)) days" +%Y-%m-%dT%H:%M:%SZ)

log "чистка: полные снимки и архив изменений раньше $before"
wal-g delete before FIND_FULL "$before" --confirm
for folder in db uploads; do
    log "чистка: ночные копии в $folder/ старше $days дней"
    rclone delete --min-age "${days}d" "b2:$folder"
done
log "чистка: готово"
