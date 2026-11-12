# Пульт: бэкапы и восстановление (E10-03, ADR-081; docs/17-OPERATIONS.md).
# Сами копии, проверки и восстановление делают скрипты deploy/backup/ в
# контейнере backup — пульт их зовёт и показывает итог.

backup_now() {
    say "Ночная копия сейчас: дамп базы и файлы загрузок → B2 и R2"
    dc exec -T backup "$BACKUP_LIB/nightly.sh"
    done_msg "копия сделана — «Служебное → Бэкапы»"
}

base_now() {
    say "Полный снимок базы → B2"
    dc exec -T backup "$BACKUP_LIB/run-recorded.sh" base b2 "$BACKUP_LIB/step-base.sh"
    done_msg "снимок сделан"
}

restore_test_now() {
    say "Проверка восстановления: архив B2 и дамп R2 — во временные базы"
    dc exec -T backup "$BACKUP_LIB/weekly.sh"
    done_msg "проверка прошла"
}

op_backup_now() {
    run_logged backup-now backup_now
}

op_base_now() {
    run_logged base-now base_now
}

op_restore_test() {
    run_logged restore-test restore_test_now
}

op_backup_list() {
    local text
    text=$(db_sql <<'SQL' | column -t -s '|'
SELECT to_char(started_at, 'DD.MM HH24:MI'),
       CASE kind WHEN 'base' THEN 'снимок' WHEN 'dump' THEN 'дамп'
                 WHEN 'uploads' THEN 'файлы' ELSE 'проверка' END,
       upper(target),
       CASE status WHEN 'ok' THEN '✓' WHEN 'failed' THEN '✗' ELSE '…' END,
       coalesce(pg_size_pretty(size_bytes), ''),
       left(coalesce(message, ''), 70)
FROM backup_runs ORDER BY started_at DESC, id DESC LIMIT 30;
SQL
)
    text="$(archive_sql | db_sql)"$'\n\n'"${text:-Бэкапов ещё не было.}"
    if [ "$INTERACTIVE" = 1 ]; then
        ui_show "Последние бэкапы" "$text"
    else
        printf '%s\n' "$text"
    fi
}

# Минута по умолчанию — 10 минут назад, в часовом поясе системы.
minute_ago() {
    TZ=$(time_zone) date -d '-10 min' '+%Y-%m-%d %H:%M'
}

# --- Копия на минуту в прошлом: посмотреть и достать испорченное ----------

op_restore_copy() {
    local when
    when=$(ui_input "Копия базы на какую минуту? Формат ГГГГ-ММ-ДД ЧЧ:ММ (время системы)." \
        "$(minute_ago)")
    [ -n "$when" ] || return 0
    run_logged restore-copy restore_copy "$when"
    if [ "$INTERACTIVE" = 1 ] && ui_confirm "Открыть консоль базы-копии (psql)? Выход — \\q."; then
        clear
        dc exec backup sh -c "psql -h /scratch/sock -p 5433 -d \"\$PGDATABASE\"" || true
    fi
    if [ "$INTERACTIVE" = 1 ] && ui_confirm "Убрать копию на $when?"; then
        dc exec -T backup "$BACKUP_LIB/restore-copy.sh" --stop
    fi
}

restore_copy() {
    say "Копия базы на $1 — рядом с живой, живая не меняется"
    dc exec -T backup "$BACKUP_LIB/restore-copy.sh" "$1"
    done_msg "копия открыта: консоль — этот же пункт; убрать — «Убрать копию базы»"
}

op_restore_copy_stop() {
    run_logged restore-copy-stop dc exec -T backup "$BACKUP_LIB/restore-copy.sh" --stop
}

# --- Том базы ----------------------------------------------------------------

volume() {
    echo "$(project)_$1"
}

volume_empty() {
    local name=$1
    docker volume inspect "$name" >/dev/null 2>&1 || return 0
    [ -z "$(docker run --rm -v "$name:/v" --entrypoint sh "$PG_IMAGE" -c 'ls -A /v')" ]
}

# Восстановить базу (на минуту или до конца архива) и файлы загрузок в пустые
# тома: скрипты deploy/backup/ сами отказываются писать в непустую папку.
restore_volumes() {
    local when=${1:-} files=${2:-yes}
    say "База из архива B2${when:+ на $when} → том $(volume pgdata)"
    # Тома с метками compose: иначе он предупреждал бы о «чужих» томах.
    dc create --no-recreate postgres >/dev/null 2>&1 || true
    dc run --rm --no-deps -T -v "$(volume pgdata):/restore" \
        --entrypoint "$BACKUP_LIB/restore.sh" backup /restore ${when:+"$when"}
    if [ "$files" = yes ]; then
        say "Файлы загрузок из R2 → том $(volume uploads)"
        dc run --rm --no-deps -T -v "$(volume uploads):/restore" \
            --entrypoint "$BACKUP_LIB/restore-files.sh" backup /restore
    fi
}

# --- Откат всей базы на минуту -------------------------------------------------

op_restore_rollback() {
    local when
    when=$(ui_input "Откатить ВСЮ базу на минуту (ГГГГ-ММ-ДД ЧЧ:ММ, время системы):" "$(minute_ago)")
    [ -n "$when" ] || return 0
    ui_confirm_word "Вся база вернётся на $when. Работа ВСЕХ после этой минуты пропадёт из базы (останется в архиве и в снимке перед откатом).

Достать одну-две записи проще копией на минуту — пункт «Копия базы на минуту».

Порядок: снимок базы и копия её тома → остановка → база из архива на $when → запуск. Сайт будет недоступен несколько минут." \
        ОТКАТИТЬ || return 0
    run_logged restore-rollback restore_rollback "$when"
}

restore_rollback() {
    local when=$1 aside
    aside="$(project)_pgdata_before_$(date +%Y%m%d_%H%M)"
    pre_dump "before-db-rollback"
    say "Изменения до текущей минуты — в архив"
    dc exec -T backup bash -c "source $BACKUP_LIB/lib.sh && flush_archive"
    say "Останавливаю всё, кроме базы"
    dc stop app worker beat backup caddy
    say "Копия тома базы как есть → $aside (вернуть, если что-то пойдёт не так)"
    dc stop postgres
    docker volume create "$aside" >/dev/null
    docker run --rm -v "$(volume pgdata):/from:ro" -v "$aside:/to" --entrypoint sh "$PG_IMAGE" \
        -c 'cp -a /from/. /to/'
    say "Освобождаю том базы"
    # Том держат и остановленные контейнеры: база и backup (читает её файлы).
    dc rm -f -s postgres backup >/dev/null
    docker volume rm "$(volume pgdata)" >/dev/null
    if ! restore_volumes "$when" no; then
        fail "восстановление не удалось — возвращаю прежнюю базу из $aside"
        docker volume rm "$(volume pgdata)" >/dev/null 2>&1 || true
        dc create --no-recreate postgres >/dev/null 2>&1 || true
        docker run --rm -v "$aside:/from:ro" -v "$(volume pgdata):/to" --entrypoint sh "$PG_IMAGE" \
            -c 'cp -a /from/. /to/'
        ver "$(current_version)" dc up -d
        return 1
    fi
    say "Запуск: база догоняет архив до $when и открывается"
    ver "$(current_version)" dc up -d
    if wait_healthy; then
        done_msg "база — на $when. Копия прежнего тома: $aside (удалить — «Обслуживание»)"
        return 0
    fi
    fail "сайт не открылся — смотрите «Журналы служб»; прежняя база — в томе $aside"
}

# --- Восстановление после аварии: новый сервер, пустые тома --------------------

op_restore_full() {
    if ! volume_empty "$(volume pgdata)"; then
        ui_show "Восстановление после аварии" "На этом сервере уже есть база (том $(volume pgdata) не пустой).

Этот пункт — только для нового сервера после аварии. Вернуть базу на минуту в прошлом — «Откатить всю базу на минуту»."
        return 1
    fi
    local when
    when=$(ui_input "Восстановить до последнего изменения в архиве — оставьте пусто. На минуту в прошлом — ГГГГ-ММ-ДД ЧЧ:ММ:" "")
    ui_confirm_word "Новый сервер: база из архива B2${when:+ на $when} и файлы загрузок из R2 — в пустые тома, затем запуск.

Старый сервер, если оживёт, НЕ включать: оба писали бы в один архив." ВОССТАНОВИТЬ \
        || return 0
    run_logged restore-full restore_full "$when"
}

restore_full() {
    local version
    version=$(short_sha)
    if [ "$MODE" = rehearsal ]; then
        # На тестовой копии хранилища — эмулятор: он должен работать.
        dc up -d s3
    fi
    say "Сборка образов версии $version"
    ver "$version" dc build app postgres
    docker tag "$APP_IMAGE:$version" "$APP_IMAGE:latest"
    restore_volumes "${1:-}" yes
    say "Запуск: база догоняет архив и открывается"
    ver "$version" dc up -d
    if wait_healthy; then
        done_msg "система восстановлена и работает. Переключите запись DNS домена на этот сервер"
        return 0
    fi
    fail "сайт не открылся — смотрите «Журналы служб»"
}

op_backups() {
    local choice
    choice=$(ui_menu "Бэкапы" "Ночью копии делаются сами; здесь — сейчас, вручную." \
        list "Последние бэкапы и архив изменений" \
        now "Сделать копию сейчас (дамп и файлы → B2 и R2)" \
        base "Полный снимок базы сейчас (→ B2)" \
        test "Проверить восстановление сейчас")
    case "$choice" in
        list) op_backup_list ;;
        now) op_backup_now ;;
        base) op_base_now ;;
        test) op_restore_test ;;
    esac
}

op_restore() {
    local choice
    choice=$(ui_menu "Восстановление" "Копия на минуту — безопасно, живая база не меняется. Остальное — с подтверждением." \
        copy "Копия базы на минуту — посмотреть, достать удалённое" \
        copy_stop "Убрать копию базы" \
        rollback "Откатить ВСЮ базу на минуту (работа после пропадёт)" \
        full "Восстановление после аварии — новый сервер")
    case "$choice" in
        copy) op_restore_copy ;;
        copy_stop) op_restore_copy_stop ;;
        rollback) op_restore_rollback ;;
        full) op_restore_full ;;
    esac
}
