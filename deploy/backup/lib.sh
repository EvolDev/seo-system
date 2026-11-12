# Общее у скриптов бэкапа (E10-03, ADR-081). Подключается через `source`.
#
# Где что лежит:
# - B2 (Backblaze): wal-g/ — архив изменений базы и полные снимки (WAL-G,
#   шифрование libsodium, ключ BACKUP_WALG_KEY); files/ — ночной дамп и файлы
#   загрузок (rclone crypt, пароль BACKUP_FILES_PASSWORD);
# - R2 (Cloudflare): files/ — то же, что files/ в B2: копия у другой компании.
# Без двух ключей из менеджера паролей копии не прочитать никому, и нам тоже.
#
# Скрипты работают в контейнере backup от пользователя postgres: живая база —
# только по сети (postgres:5432) и её файлы только для чтения; временные базы
# проверки — в /scratch, свой сокет, без сетевого порта.

set -euo pipefail

# shellcheck disable=SC2034  # LIB, SCRATCH — для скриптов, которые подключают lib.sh
LIB=/usr/local/lib/seo-backup
SCRATCH=/scratch
LIVE_PGDATA=/var/lib/postgresql/data
# Временный сервер проверки: только сокет в /scratch, без TCP — спутать его с
# живой базой нельзя.
TEMP_SOCKET="$SCRATCH/sock"
TEMP_PORT=5433

log() {
    printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >&2
}

die() {
    log "ОШИБКА: $*"
    exit 1
}

# --- Журнал backup_runs в живой базе ---------------------------------------

# Начало записи: печатает её номер.
run_start() {
    local kind=$1 target=$2
    psql -XAtq -v ON_ERROR_STOP=1 -v kind="$kind" -v target="$target" <<'SQL'
INSERT INTO backup_runs (kind, target) VALUES (:'kind', :'target') RETURNING id;
SQL
}

# Конец записи: итог, размер (байт, можно пусто), сообщение, подробности (JSON).
run_finish() {
    local id=$1 status=$2 size=${3:-} message=${4:-} details=${5:-}
    psql -XAtq -v ON_ERROR_STOP=1 -v id="$id" -v status="$status" -v size="$size" \
        -v message="$message" -v details="$details" <<'SQL'
UPDATE backup_runs
SET status = :'status',
    finished_at = now(),
    size_bytes = NULLIF(:'size', '')::bigint,
    message = NULLIF(:'message', ''),
    details = NULLIF(:'details', '')::jsonb
WHERE id = :'id'::bigint;
SQL
}

# Удачные записи «что и куда» есть? Для первого запуска сервера.
has_ok_run() {
    local kind=$1 target=$2 count
    count=$(psql -XAtq -v ON_ERROR_STOP=1 -v kind="$kind" -v target="$target" <<'SQL'
SELECT count(*) FROM backup_runs WHERE kind = :'kind' AND target = :'target' AND status = 'ok';
SQL
)
    [ "$count" -gt 0 ]
}

# --- rclone: хранилища из переменных окружения, без файла настроек ----------

rclone_env() {
    : "${B2_ENDPOINT:?нет B2_ENDPOINT}" "${B2_BUCKET:?нет B2_BUCKET}"
    : "${R2_ENDPOINT:?нет R2_ENDPOINT}" "${R2_BUCKET:?нет R2_BUCKET}"
    : "${BACKUP_FILES_PASSWORD:?нет BACKUP_FILES_PASSWORD}"
    export RCLONE_CONFIG_B2RAW_TYPE=s3 RCLONE_CONFIG_B2RAW_PROVIDER=Other
    export RCLONE_CONFIG_B2RAW_ENDPOINT="$B2_ENDPOINT"
    export RCLONE_CONFIG_B2RAW_REGION="${B2_REGION:-}"
    export RCLONE_CONFIG_B2RAW_ACCESS_KEY_ID="${B2_KEY_ID:-}"
    export RCLONE_CONFIG_B2RAW_SECRET_ACCESS_KEY="${B2_APPLICATION_KEY:-}"
    export RCLONE_CONFIG_R2RAW_TYPE=s3 RCLONE_CONFIG_R2RAW_PROVIDER=Other
    export RCLONE_CONFIG_R2RAW_ENDPOINT="$R2_ENDPOINT"
    export RCLONE_CONFIG_R2RAW_REGION=auto
    export RCLONE_CONFIG_R2RAW_ACCESS_KEY_ID="${R2_ACCESS_KEY_ID:-}"
    export RCLONE_CONFIG_R2RAW_SECRET_ACCESS_KEY="${R2_SECRET_ACCESS_KEY:-}"
    # Ключи сервера могут не уметь перечислять корзины — не проверяем их.
    export RCLONE_S3_NO_CHECK_BUCKET=true
    local password
    password=$(rclone obscure "$BACKUP_FILES_PASSWORD")
    local name raw bucket
    for name in B2 R2; do
        raw="${name}RAW"
        bucket="${name}_BUCKET"
        export "RCLONE_CONFIG_${name}_TYPE=crypt"
        export "RCLONE_CONFIG_${name}_REMOTE=${raw,,}:${!bucket}/files"
        export "RCLONE_CONFIG_${name}_PASSWORD=$password"
        # Имена файлов видны (дата в имени) — шифруется содержимое.
        export "RCLONE_CONFIG_${name}_FILENAME_ENCRYPTION=off"
        export "RCLONE_CONFIG_${name}_DIRECTORY_NAME_ENCRYPTION=false"
    done
}

# --- Временный сервер Postgres для проверок и копий ------------------------

# Папка под восстановление: только пустая, только в /scratch или /restore и
# никогда — папка живой базы. Восстановление поверх работающей базы невозможно.
guard_target_dir() {
    local dir=$1 real live
    [ -n "$dir" ] || die "не задана папка для восстановления"
    real=$(realpath -m "$dir")
    live=$(realpath -m "$LIVE_PGDATA")
    case "$real" in
        "$live" | "$live"/*) die "$real — папка живой базы, восстанавливать в неё нельзя" ;;
        "$SCRATCH"/* | /restore | /restore/*) ;;
        *) die "$real — восстанавливать можно только в /scratch/… или /restore" ;;
    esac
    mkdir -p "$real"
    if [ -n "$(ls -A "$real")" ]; then
        die "$real не пустая — восстановление только в пустую папку"
    fi
}

# Запустить временный сервер на папке: без архива (иначе он писал бы в архив
# живой базы), без сетевого порта. Дополнительные -c — аргументами.
temp_start() {
    local dir=$1
    shift
    mkdir -p "$TEMP_SOCKET"
    chmod 700 "$dir"
    # max_connections не меньше, чем у живой базы (100 по умолчанию): иначе
    # Postgres не продолжает восстановление из её архива.
    local options="-c port=$TEMP_PORT -c listen_addresses='' -c unix_socket_directories=$TEMP_SOCKET"
    options+=" -c archive_mode=off -c shared_buffers=128MB"
    local extra
    for extra in "$@"; do
        options+=" $extra"
    done
    local logfile
    logfile="$(dirname "$dir")/$(basename "$dir").log"
    if ! pg_ctl -D "$dir" -o "$options" -l "$logfile" -w -t 600 start >&2; then
        # Причина — в логе сервера: в журнал бэкапов идут его последние строки.
        grep -E "FATAL|ERROR|DETAIL|PANIC" "$logfile" | tail -n 3 >&2 || true
        return 1
    fi
}

temp_stop() {
    local dir=$1
    if [ -f "$dir/postmaster.pid" ]; then
        pg_ctl -D "$dir" -m fast -w stop >&2 || true
    fi
}

temp_psql() {
    psql -XAtq -v ON_ERROR_STOP=1 -h "$TEMP_SOCKET" -p "$TEMP_PORT" -U "${PGUSER:-postgres}" "$@"
}

# Довести архив до текущей минуты: свежая фиксация (после любой минуты в прошлом
# в журнале есть запись — иначе Postgres дойдёт до конца архива раньше цели и
# остановится) и переключение файла журнала; ждать, пока он уйдёт в B2.
flush_archive() {
    psql -XAtq -v ON_ERROR_STOP=1 -c "SELECT txid_current()" >/dev/null
    local wal archived=""
    wal=$(psql -XAtq -v ON_ERROR_STOP=1 -c "SELECT pg_walfile_name(pg_switch_wal())")
    for _ in $(seq 1 120); do
        archived=$(psql -XAtq -c "SELECT coalesce(last_archived_wal, '') FROM pg_stat_archiver")
        if [[ "$archived" > "$wal" || "$archived" == "$wal" ]]; then
            return 0
        fi
        sleep 2
    done
    die "файл журнала $wal не ушёл в архив за 4 минуты (последний ушедший — ${archived:-нет})"
}

# Ждать, пока временная база догонит архив до цели и откроется на запись. Упала
# по дороге — сразу сказать почему, а не ждать.
wait_promoted() {
    local dir=$1 minutes=${2:-30} state=""
    local logfile
    logfile="$(dirname "$dir")/$(basename "$dir").log"
    for _ in $(seq 1 $((minutes * 12))); do
        if ! pg_ctl -D "$dir" status >/dev/null 2>&1; then
            grep -E "FATAL|PANIC" "$logfile" | tail -n 2 >&2 || true
            die "временная база остановилась, не дойдя до нужной минуты"
        fi
        state=$(temp_psql -d "$PGDATABASE" -c "SELECT pg_is_in_recovery()" 2>/dev/null || echo "?")
        if [ "$state" = f ]; then
            return 0
        fi
        sleep 5
    done
    die "база не догнала архив за $minutes минут"
}

# Число строк каждой таблицы: JSON {таблица: строк}. Без аргументов — живая
# база, с аргументами — их передать psql (временный сервер).
TABLE_COUNTS_SQL="SELECT coalesce(json_object_agg(table_name, (xpath('/row/c/text()',
    query_to_xml(format('SELECT count(*) AS c FROM %I.%I', table_schema, table_name),
    false, true, '')))[1]::text::bigint ORDER BY table_name), '{}')
FROM information_schema.tables WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"

table_counts() {
    if [ "$#" -eq 0 ]; then
        psql -XAtq -v ON_ERROR_STOP=1 -c "$TABLE_COUNTS_SQL"
    else
        "$@" -c "$TABLE_COUNTS_SQL"
    fi
}

# Сверка двух наборов: таблицы нет, пустая вместо полной или разница больше 1 % —
# расхождение. Печатает JSON-список расхождений, пустой — «[]».
compare_counts() {
    local expected=$1 actual=$2
    jq -cn --argjson a "$expected" --argjson b "$actual" '
        [ $a | to_entries[]
          | (.value) as $want | ($b[.key]) as $got
          | select($got == null or ($want > 0 and $got == 0)
                   or ((($want - $got) | fabs) > ($want * 0.01 + 5)))
          | "\(.key): \($want) → \($got // "нет таблицы")" ]'
}
