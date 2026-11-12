# Пульт: службы — запуск, остановка, перезапуск, журналы.

SERVICES=(app worker beat postgres redis caddy backup)

start_all() {
    ver "$(current_version)" dc up -d
    done_msg "запущено"
}

restart_services() {
    ver "$(current_version)" dc up -d --no-deps --force-recreate "$@"
    done_msg "перезапущено: $*"
}

restart_all() {
    ver "$(current_version)" dc up -d --force-recreate
    done_msg "перезапущено всё"
}

op_start() {
    run_logged start start_all
}

op_stop() {
    ui_confirm "Остановить всю систему? Сайт будет недоступен, пока не запустите снова. Данные останутся на месте." \
        || return 0
    run_logged stop dc stop
}

op_restart_app() {
    run_logged restart-app restart_services app
}

op_restart_queue() {
    run_logged restart-queue restart_services worker beat
}

op_restart_all() {
    ui_confirm "Перезапустить всё, вместе с базой? Сайт будет недоступен около минуты." || return 0
    run_logged restart-all restart_all
}

service_label() {
    case "$1" in
        app) echo "Сайт (приложение)" ;;
        worker) echo "Очередь: воркер" ;;
        beat) echo "Очередь: расписание" ;;
        postgres) echo "База" ;;
        redis) echo "Redis" ;;
        caddy) echo "Caddy: HTTPS и вход снаружи" ;;
        backup) echo "Бэкапы" ;;
    esac
}

errors_text() {
    dc logs --since 24h --no-color 2>&1 \
        | grep -E '"level": "(ERROR|CRITICAL)"|ERROR|Traceback|FATAL' | tail -n 80 || true
}

op_errors() {
    local text
    text=$(errors_text)
    if [ "$INTERACTIVE" = 1 ]; then
        ui_show "Ошибки за сутки" "${text:-Ошибок нет.}"
    else
        printf '%s\n' "${text:-Ошибок нет.}"
    fi
}

op_logs() {
    local args=() service
    for service in "${SERVICES[@]}"; do
        args+=("$service" "$(service_label "$service")")
    done
    args+=(errors "Ошибки за сутки — всех служб")
    service=$(ui_menu "Журналы служб" "Чей журнал открыть? Выход из журнала — Ctrl+C." "${args[@]}")
    [ -n "$service" ] || return 0
    if [ "$service" = errors ]; then
        op_errors
        return 0
    fi
    clear
    # Ctrl+C закрывает журнал, а не пульт.
    trap 'true' INT
    dc logs --tail 200 -f "$service" || true
    trap - INT
}

op_services() {
    local choice
    choice=$(ui_menu "Службы" "Что сделать?" \
        start "Запустить всё" \
        restart_app "Перезапустить сайт" \
        restart_queue "Перезапустить очередь (после правки задач или .env)" \
        restart_all "Перезапустить всё, вместе с базой" \
        stop "Остановить всё")
    case "$choice" in
        start) op_start ;;
        restart_app) op_restart_app ;;
        restart_queue) op_restart_queue ;;
        restart_all) op_restart_all ;;
        stop) op_stop ;;
    esac
}
