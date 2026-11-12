# Пульт в локальной версии: копия её базы, вход в меню сервера, тестовая копия.
# shellcheck disable=SC2016  # $POSTGRES_USER и т. п. раскрывает оболочка контейнера
#
# Копия базы — pg_dump: базу только читает, работать в это время можно. Файл —
# backups/<дата>-local.dump; вернуть его — только вместе со мной/по инструкции:
# восстановление поверх рабочей базы пульт не делает.

# Адрес сервера — deploy/ops.local.conf (в git не идёт):
#   SEO_SSH=seo@203.0.113.10
#   SEO_SERVER_DIR=/srv/seo-system
LOCAL_CONF="$ROOT/deploy/ops.local.conf"

local_backup() {
    local file
    mkdir -p "$BACKUP_DIR"
    file="$BACKUP_DIR/$(date +%Y-%m-%d-%H%M)-local.dump"
    say "Копия локальной базы → $file"
    dc exec -T postgres sh -c 'pg_dump -Fc -U "$POSTGRES_USER" -d "$POSTGRES_DB"' >"$file"
    [ -s "$file" ] || {
        rm -f "$file"
        fail "копия пустая — запущена ли локальная версия (make up)?"
        return 1
    }
    # Копия читается: оглавление дампа разбирается тем же Postgres.
    local tables
    tables=$(dc exec -T postgres pg_restore -l <"$file" | grep -c ' TABLE DATA ' || true)
    [ "$tables" -gt 0 ] || {
        fail "копия не читается"
        return 1
    }
    done_msg "копия готова: $(du -h "$file" | cut -f1), таблиц с данными: $tables"
}

op_local_backup() {
    run_logged local-backup local_backup
}

local_list_text() {
    find "$BACKUP_DIR" -maxdepth 1 -name '*.dump' -printf '%TY-%Tm-%Td %TH:%TM  %s  %f\n' 2>/dev/null \
        | sort -r | awk '{ $3 = sprintf("%6.1f МБ", $3 / 1048576); print }' | head -40
}

op_local_list() {
    local text
    text=$(local_list_text)
    if [ "$INTERACTIVE" = 1 ]; then
        ui_show "Копии локальной базы — папка backups/" "${text:-Копий ещё нет.}"
    else
        printf '%s\n' "${text:-Копий ещё нет.}"
    fi
}

op_local_prune() {
    local old
    old=$(find "$BACKUP_DIR" -maxdepth 1 -name '*-local.dump' -printf '%T@ %p\n' 2>/dev/null \
        | sort -rn | tail -n +11 | cut -d' ' -f2-)
    if [ -z "$old" ]; then
        ui_show "Копии локальной базы" "Копий больше десяти нет — удалять нечего."
        return 0
    fi
    # shellcheck disable=SC2086  # список файлов — словами
    ui_confirm "Удалить копии локальной базы, кроме десяти последних?"$'\n\n'"$(basename -a $old)" \
        || return 0
    # shellcheck disable=SC2086
    rm -f $old
    done_msg "удалено: $(wc -w <<<"$old")"
    pause
}

op_server_ssh() {
    if [ ! -f "$LOCAL_CONF" ]; then
        ui_show "Сервер" "Сервер ещё не настроен: нет deploy/ops.local.conf с адресом (появится на шаге настройки сервера)."
        return 0
    fi
    # shellcheck source=/dev/null
    source "$LOCAL_CONF"
    clear
    ssh -t "${SEO_SSH:?нет SEO_SSH в deploy/ops.local.conf}" \
        "cd ${SEO_SERVER_DIR:-/srv/seo-system} && ./deploy/seo server" || true
}

op_rehearsal() {
    if [ -z "${SEO_REHEARSAL_ENV:-}" ]; then
        ui_show "Тестовая копия" "Не задан SEO_REHEARSAL_ENV — путь к файлу окружения тестовой копии (README, «Прод и тестовая копия прода»)."
        return 0
    fi
    "$ROOT/deploy/seo" rehearsal
}

local_menu() {
    local choice
    while true; do
        choice=$(ui_menu "SEO-система · локальная версия" "Работа в локальной версии не прерывается." \
            backup "Сделать копию локальной базы" \
            list "Копии локальной базы" \
            prune "Удалить старые копии (кроме десяти последних)" \
            server "Меню сервера (по SSH)" \
            rehearsal "Тестовая копия прода (8003)" \
            quit "Выход")
        case "$choice" in
            backup) op_local_backup ;;
            list) op_local_list ;;
            prune) op_local_prune ;;
            server) op_server_ssh ;;
            rehearsal) op_rehearsal ;;
            quit | "") return 0 ;;
        esac
    done
}
