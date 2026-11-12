# Пульт: ручные команды и обслуживание сервера.
# shellcheck disable=SC2016  # $POSTGRES_USER и т. п. раскрывает оболочка контейнера

op_psql() {
    clear
    echo "Консоль базы. Выход — \\q. Осторожно: правки здесь — сразу в рабочей базе."
    dc exec postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"' || true
}

op_django_shell() {
    clear
    dc exec app python manage.py shell || true
}

op_manage() {
    local command
    command=$(ui_input "Команда manage.py, например: queue_check" "")
    [ -n "$command" ] || return 0
    # shellcheck disable=SC2086  # команда — словами, как её вписали
    run_logged manage dc exec -T app python manage.py $command
}

op_app_shell() {
    clear
    dc exec app bash || dc exec app sh || true
}

op_compose() {
    local command
    command=$(ui_input "Команда docker compose для проекта $(project), например: ps" "ps")
    [ -n "$command" ] || return 0
    # shellcheck disable=SC2086
    run_logged compose dc $command
}

op_manual() {
    local choice
    choice=$(ui_menu "Ручные команды" "Для тех, кто знает, что делает." \
        psql "Консоль базы (psql)" \
        shell "Консоль Django (manage.py shell)" \
        manage "Команда manage.py" \
        bash "Оболочка в контейнере приложения" \
        compose "Команда docker compose")
    case "$choice" in
        psql) op_psql ;;
        shell) op_django_shell ;;
        manage) op_manage ;;
        bash) op_app_shell ;;
        compose) op_compose ;;
    esac
}

# --- Обслуживание --------------------------------------------------------------

disk_text() {
    echo "Диск:"
    df -h / | awk 'NR > 1 {print "  занято " $3 " из " $2 " (" $5 "), свободно " $4}'
    echo
    echo "Docker:"
    docker system df | sed 's/^/  /'
    echo
    echo "Снимки базы на сервере (backups/):"
    find "$BACKUP_DIR" -maxdepth 1 -name '*.dump' -printf '  %TY-%Tm-%Td %TH:%TM  %s  %f\n' 2>/dev/null \
        | sort -r | awk '{ $3 = sprintf("%.0f МБ", $3 / 1048576); print }' | head -20
    echo
    echo "Копии тома базы перед откатом:"
    docker volume ls -q --filter "name=$(project)_pgdata_before_" | sed 's/^/  /'
}

op_disk() {
    local text
    text=$(disk_text 2>&1)
    if [ "$INTERACTIVE" = 1 ]; then
        ui_show "Место на диске" "$text"
    else
        printf '%s\n' "$text"
    fi
}

# Образы приложения: оставить запущенный и три последних, остальное — удалить.
prune_images() {
    local keep running
    running=$(running_image)
    keep=$(docker image ls "$APP_IMAGE" --format '{{.Tag}}' | grep -v '^latest$' | head -n 3)
    say "Старые образы приложения"
    docker image ls "$APP_IMAGE" --format '{{.Tag}}' | grep -v '^latest$' | while read -r tag; do
        if ! grep -qx "$tag" <<<"$keep" && [ "$APP_IMAGE:$tag" != "$running" ]; then
            docker image rm "$APP_IMAGE:$tag" >/dev/null && echo "  удалён $tag"
        fi
    done
    say "Неиспользуемые слои и кеш сборки"
    docker image prune -f
    docker builder prune -f --filter 'until=168h'
    done_msg "готово"
}

op_prune() {
    ui_confirm "Удалить старые образы приложения (кроме запущенного и трёх последних) и кеш сборки старше недели? Данные и тома не трогаются." \
        || return 0
    run_logged prune prune_images
}

op_drop_aside() {
    local volumes
    volumes=$(docker volume ls -q --filter "name=$(project)_pgdata_before_")
    if [ -z "$volumes" ]; then
        ui_show "Копии тома базы" "Копий тома базы перед откатом нет."
        return 0
    fi
    ui_confirm "Удалить копии тома базы, сделанные перед откатом?"$'\n\n'"$volumes" || return 0
    # shellcheck disable=SC2086
    run_logged drop-aside docker volume rm $volumes
}

op_reboot() {
    ui_confirm_word "Перезагрузить сервер? Сайт будет недоступен 1–3 минуты, всё поднимется само." \
        ПЕРЕЗАГРУЗИТЬ || return 0
    sudo reboot
}

op_maintenance() {
    local items=(disk "Место на диске, снимки, копии тома"
        prune "Удалить старые образы и кеш сборки"
        aside "Удалить копии тома базы перед откатом"
        history "История выкладок")
    [ "$MODE" = server ] && items+=(reboot "Перезагрузить сервер")
    local choice
    choice=$(ui_menu "Обслуживание" "Что сделать?" "${items[@]}")
    case "$choice" in
        disk) op_disk ;;
        prune) op_prune ;;
        aside) op_drop_aside ;;
        history) op_history ;;
        reboot) op_reboot ;;
    esac
}
