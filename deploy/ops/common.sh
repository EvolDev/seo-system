# Пульт SEO-системы (deploy/seo): общее — режим, docker compose, окна, журнал.
# Подключается через `source`. E10-02, ADR-081.
# shellcheck disable=SC2034  # переменные — для других частей пульта
# shellcheck disable=SC2016  # $POSTGRES_USER и т. п. раскрывает оболочка контейнера
#
# Режимы:
#   server     — на сервере: проект compose «seo», файл compose.prod.yaml;
#   rehearsal  — тестовая копия прода в локальной версии (8003, эмулятор S3);
#   local      — локальная версия: копия её базы, вход в меню сервера.
# Каждое действие — функция op_*: её зовёт меню, а `--run <действие>` — без меню
# (так пульт проверяется на тестовой копии и годится для автоматизации).

MODE=""
YES=0             # --yes: на подтверждения ответить «да» (только без меню)
INTERACTIVE=1     # 0 — `--run`: без окон, вывод — в терминал
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
LOG_DIR="$ROOT/deploy/logs"
BACKUP_DIR="$ROOT/backups"
APP_IMAGE=seo-system-prod
PG_IMAGE=seo-system-postgres:latest
BRANCH=${SEO_BRANCH:-main}
# Всё, что делают скрипты бэкапа (deploy/backup/), — в контейнере backup.
BACKUP_LIB=/usr/local/lib/seo-backup

project() {
    case "$MODE" in
        server) echo seo ;;
        rehearsal) echo seo-rehearsal ;;
        local) echo seo-system ;;
    esac
}

# docker compose нужного режима. Тестовая копия берёт свой файл окружения —
# путь в SEO_REHEARSAL_ENV (README, «Прод и тестовая копия прода»).
dc() {
    case "$MODE" in
        server)
            docker compose -p seo -f "$ROOT/compose.prod.yaml" "$@"
            ;;
        rehearsal)
            : "${SEO_REHEARSAL_ENV:?задайте SEO_REHEARSAL_ENV — путь к rehearsal.env}"
            docker compose -p seo-rehearsal -f "$ROOT/compose.prod.yaml" \
                -f "$ROOT/deploy/compose.rehearsal.yaml" --env-file "$SEO_REHEARSAL_ENV" "$@"
            ;;
        local)
            docker compose -p seo-system -f "$ROOT/compose.yaml" "$@"
            ;;
    esac
}

# Запрос к живой базе: SQL на вход, строки с « | » между колонками на выход.
db_sql() {
    dc exec -T postgres sh -c \
        'PGTZ="${TZ:-Europe/Moscow}" psql -XAtq -F " | " -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
}

# Часовой пояс системы — из настроек приложения.
time_zone() {
    dc exec -T app printenv TIME_ZONE 2>/dev/null || echo Europe/Moscow
}

# Второе хранилище (R2) включено? Спрашиваем контейнер backup: решает его
# BACKUP_R2_ENABLED и то же правило, что у скриптов бэкапа.
r2_on() {
    dc exec -T backup bash -c "source $BACKUP_LIB/lib.sh && r2_enabled" >/dev/null 2>&1
}

# Куда уходит ночная копия — для подписей.
copy_targets_text() {
    if r2_on; then
        echo "B2 и R2"
    else
        echo "B2"
    fi
}

# Адрес сайта для проверки снаружи (через Caddy).
site_url() {
    if [ "$MODE" = rehearsal ]; then
        echo "https://localhost:8003"
    else
        echo "https://$(dc exec -T caddy printenv SITE_ADDRESS 2>/dev/null)"
    fi
}

# Сайт отвечает «ok» через Caddy — так же, как людям.
# Секретный путь системы (ADR-081): снаружи открыт только он.
site_path() {
    dc exec -T app printenv SITE_PATH 2>/dev/null || true
}

# Адрес входа в систему — с секретным путём.
site_home() {
    echo "$(site_url)/$(site_path)/"
}

site_ok() {
    [ "$(curl -ks --max-time 10 "$(site_home)healthz" 2>/dev/null)" = ok ]
}

# Ждать, пока приложение здорово и сайт отвечает: до 3 минут.
wait_healthy() {
    local i health
    for i in $(seq 1 60); do
        health=$(dc ps --format '{{.Health}}' app 2>/dev/null || true)
        if [ "$health" = healthy ] && site_ok; then
            return 0
        fi
        sleep 3
    done
    return 1
}

# Версия: какой образ приложения запущен (seo-system-prod:abc1234).
running_image() {
    local id
    id=$(dc ps -q app 2>/dev/null || true)
    if [ -n "$id" ]; then
        docker inspect --format '{{.Config.Image}}' "$id" 2>/dev/null || true
    fi
}

# Команда с версией приложения для compose: ver <версия> dc up -d …
ver() {
    local version=$1
    shift
    (
        export APP_VERSION="$version"
        "$@"
    )
}

# Версия для compose: запущенный образ, иначе последний собранный.
current_version() {
    local image
    image=$(running_image)
    if [ -n "$image" ]; then
        echo "${image##*:}"
    else
        echo latest
    fi
}

short_sha() {
    git -C "$ROOT" rev-parse --short=7 "${1:-HEAD}"
}

# Даты в журналах, истории и именах снимков — в часовом поясе системы.
use_system_time_zone() {
    TZ=$(time_zone)
    export TZ
}

# --- Вывод и окна ------------------------------------------------------------

say() {
    printf '\n\033[1m▶ %s\033[0m\n' "$*"
}

warn() {
    printf '\033[33m! %s\033[0m\n' "$*" >&2
}

fail() {
    printf '\033[31m✗ %s\033[0m\n' "$*" >&2
    return 1
}

done_msg() {
    printf '\033[32m✓ %s\033[0m\n' "$*"
}

# Окна whiptail — когда есть терминал. Смотрим на ввод и на поток ошибок: меню
# и ввод зовутся внутри $(…), их вывод перехвачен ради выбора, и окно рисуется
# через поток ошибок.
has_whiptail() {
    [ "$INTERACTIVE" = 1 ] && [ -t 0 ] && [ -t 2 ] && command -v whiptail >/dev/null
}

# Меню: ui_menu "заголовок" "текст" тег подпись [тег подпись…] → выбранный тег.
ui_menu() {
    local title=$1 text=$2
    shift 2
    if has_whiptail; then
        whiptail --title "$title" --ok-button "Выбрать" --cancel-button "Назад" \
            --menu "$text" 24 78 14 "$@" 3>&1 1>&2 2>&3 || true
        return
    fi
    # Без whiptail — нумерованный список.
    printf '\n%s\n%s\n' "$title" "$text" >&2
    local tags=() i=1 choice
    while [ "$#" -gt 0 ]; do
        printf '  %s) %s\n' "$1" "$2" >&2
        tags+=("$1")
        shift 2
        i=$((i + 1))
    done
    read -rp "Выбор: " choice
    printf '%s' "$choice"
}

ui_confirm() {
    local text=$1
    if [ "$INTERACTIVE" = 0 ]; then
        [ "$YES" = 1 ] && return 0
        fail "нужно подтверждение — запустите с --yes"
        return 1
    fi
    if has_whiptail; then
        whiptail --title "Подтверждение" --yes-button "Да" --no-button "Нет" \
            --yesno "$text" 20 78
        return
    fi
    local answer
    printf '%s\n' "$text"
    read -rp "Продолжить? (да/нет): " answer
    [ "$answer" = да ]
}

# Опасное действие: подтвердить, впечатав слово.
ui_confirm_word() {
    local text=$1 word=$2 answer
    if [ "$INTERACTIVE" = 0 ]; then
        [ "$YES" = 1 ] && return 0
        fail "нужно подтверждение — запустите с --yes"
        return 1
    fi
    if has_whiptail; then
        answer=$(whiptail --title "Опасное действие" --inputbox \
            "$text"$'\n\n'"Чтобы продолжить, впишите: $word" 20 78 3>&1 1>&2 2>&3) || return 1
    else
        printf '%s\n' "$text"
        read -rp "Чтобы продолжить, впишите $word: " answer
    fi
    [ "$answer" = "$word" ]
}

ui_input() {
    local text=$1 default=${2:-}
    if [ "$INTERACTIVE" = 0 ]; then
        printf '%s' "$default"
        return
    fi
    if has_whiptail; then
        whiptail --title "Ввод" --inputbox "$text" 14 78 "$default" 3>&1 1>&2 2>&3 || true
        return
    fi
    local answer
    read -rp "$text [$default]: " answer
    printf '%s' "${answer:-$default}"
}

# Показать текст: в окне с прокруткой или просто в терминал.
ui_show() {
    local title=$1 text=$2
    if has_whiptail; then
        whiptail --title "$title" --scrolltext --ok-button "Назад" --msgbox "$text" 26 100
    else
        printf '\n%s\n%s\n' "$title" "$text"
    fi
}

pause() {
    if [ "$INTERACTIVE" = 1 ]; then
        printf '\n'
        read -rp "Enter — назад в меню " _
    fi
}

# Долгое действие — в терминал и в журнал deploy/logs/<дата>-<имя>.log.
run_logged() {
    local name=$1
    shift
    mkdir -p "$LOG_DIR"
    local log
    log="$LOG_DIR/$(date +%Y-%m-%d-%H%M%S)-$name.log"
    local status=0
    [ "$INTERACTIVE" = 1 ] && clear
    { "$@"; } 2>&1 | tee -a "$log" || status=$?
    printf '\nЖурнал: %s\n' "$log"
    pause
    return "$status"
}
