#!/usr/bin/env bash
# Файлы загрузок из ночной копии в ПУСТУЮ папку — авария, новый сервер:
#   restore-files.sh <папка> [r2|b2]   (по умолчанию R2)
source /usr/local/lib/seo-backup/lib.sh
rclone_env

dir=${1:-}
target=${2:-r2}
guard_target_dir "$dir"
name=$(rclone lsf "$target:uploads" --files-only | sort | tail -n 1)
[ -n "$name" ] || die "в $target нет архива файлов загрузок"
log "файлы загрузок из $target: $name → $dir"
rclone cat "$target:uploads/$name" | tar -C "$dir" -xf -
log "готово: $(find "$dir" -type f | wc -l) файлов"
