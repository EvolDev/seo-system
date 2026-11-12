#!/usr/bin/env bash
# Отправить файл ночной копии в хранилище: step-upload.sh <b2|r2> <db|uploads> <файл>.
# Шифрует rclone (crypt); имя файла видно, содержимое — нет.
source /usr/local/lib/seo-backup/lib.sh
rclone_env

target=$1
folder=$2
file=$3
kind=$([ "$folder" = db ] && echo dump || echo uploads)
if [ ! -s "$file" ]; then
    die "файл не собран: $(tail -n 3 "$(dirname "$file")/$kind.err" 2>/dev/null | paste -sd ' ' -)"
fi
name=$(basename "$file")
log "$name → $target:$folder"
rclone copyto "$file" "$target:$folder/$name"
stat -c %s "$file" >"$STEP_OUT/size"
echo "$name" >"$STEP_OUT/message"
counts=$(cat "$(dirname "$file")/$kind.counts" 2>/dev/null || echo null)
jq -cn --arg file "$name" --argjson counts "${counts:-null}" '{file: $file, counts: $counts}' \
    >"$STEP_OUT/details"
