#!/usr/bin/env bash
# Восстановить базу из архива B2 в ПУСТУЮ папку — авария, новый сервер
# (docs/17-OPERATIONS.md):
#   restore.sh <папка> ["ГГГГ-ММ-ДД ЧЧ:ММ"]
# Без времени — до последнего изменения в архиве; со временем — на эту минуту
# (по TZ контейнера). В непустую папку и в папку живой базы не пишет.
# После — запустить базу на этой папке: она догонит архив и откроется.
source /usr/local/lib/seo-backup/lib.sh

dir=${1:-}
when=${2:-}
guard_target_dir "$dir"

backup=LATEST
target_line=""
if [ -n "$when" ]; then
    target=$(date -d "$when" '+%Y-%m-%d %H:%M:%S%z') || die "не понимаю время «$when»"
    target_epoch=$(date -d "$when" +%s)
    # Снимок, законченный до нужной минуты: от него архив дойдёт до неё.
    backup=$(wal-g backup-list --detail --json | jq -r --argjson t "$target_epoch" '
        [ .[] | select((.finish_time | sub("\\.[0-9]+"; "") | fromdateiso8601) < $t) ]
        | max_by(.finish_time) | .backup_name // empty')
    [ -n "$backup" ] || die "нет полного снимка раньше $when — так далеко архив не хранит"
    target_line="recovery_target_time = '$target'"
fi

log "восстановление из B2: снимок $backup → $dir${when:+, на $when}"
wal-g backup-fetch "$dir" "$backup"
{
    echo "restore_command = 'wal-g wal-fetch \"%f\" \"%p\"'"
    echo "recovery_target_action = 'promote'"
    [ -n "$target_line" ] && echo "$target_line"
} >>"$dir/postgresql.auto.conf"
touch "$dir/recovery.signal"
chmod 700 "$dir"
log "готово: запустите базу на этой папке — она догонит архив${when:+ до $when} и откроется"
