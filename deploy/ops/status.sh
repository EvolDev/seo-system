# Пульт: состояние системы — службы, версия, сайт, бэкапы, диск, память.

# Последние бэкапы — по строке на «что и куда»; строки R2 — когда он включён.
backups_summary_sql() {
    local r2=false
    r2_on && r2=true
    cat <<SQL
SELECT k.label,
       coalesce(to_char(r.started_at, 'DD.MM HH24:MI'), 'не было'),
       CASE r.status WHEN 'ok' THEN '✓' WHEN 'failed' THEN '✗ ОШИБКА' ELSE '' END
FROM (VALUES (1, 'base', 'b2', 'Полный снимок → B2'),
             (2, 'dump', 'b2', 'Ночной дамп → B2'),
             (3, 'dump', 'r2', 'Ночной дамп → R2'),
             (4, 'uploads', 'b2', 'Файлы загрузок → B2'),
             (5, 'uploads', 'r2', 'Файлы загрузок → R2'),
             (6, 'restore_test', 'b2', 'Проверка восстановления B2'),
             (7, 'restore_test', 'r2', 'Проверка восстановления R2'))
     AS k(n, kind, target, label)
LEFT JOIN LATERAL (
    SELECT started_at, status FROM backup_runs b
    WHERE b.kind = k.kind AND b.target = k.target AND b.status <> 'running'
    ORDER BY started_at DESC LIMIT 1
) r ON true
WHERE k.target <> 'r2' OR $r2
ORDER BY k.n;
SQL
}

archive_sql() {
    cat <<'SQL'
SELECT 'Архив изменений базы: ' ||
       CASE current_setting('archive_mode')
           WHEN 'off' THEN 'ВЫКЛЮЧЕН'
           ELSE 'последний файл ' || coalesce(to_char(last_archived_time, 'DD.MM HH24:MI:SS'), 'не было')
                || CASE WHEN last_failed_time > coalesce(last_archived_time, '-infinity')
                        THEN ' · ОШИБКА с ' || to_char(last_failed_time, 'DD.MM HH24:MI') ELSE '' END
       END
FROM pg_stat_archiver;
SQL
}

status_text() {
    local version image
    version=$(git -C "$ROOT" log -1 --format='%h · %cd · %s' --date=format:'%d.%m %H:%M' 2>/dev/null)
    image=$(running_image)
    echo "Режим: $MODE · проект $(project)"
    echo "Код: $version"
    echo "Запущено: ${image:-приложение не запущено}"
    echo
    echo "Службы:"
    dc ps --all --format '  {{.Service}}\t{{.State}}\t{{.Health}}\t{{.RunningFor}}' 2>/dev/null \
        | column -t -s $'\t' || echo "  не запущены"
    echo
    if site_ok; then
        echo "Сайт: открывается ($(site_url))"
    else
        echo "Сайт: НЕ ОТВЕЧАЕТ ($(site_url)/healthz)"
    fi
    echo
    archive_sql | db_sql 2>/dev/null || echo "База недоступна"
    echo "Бэкапы:"
    backups_summary_sql | db_sql 2>/dev/null | column -t -s '|' | sed 's/^/  /' || true
    echo
    echo "Диск сервера:"
    df -h / | awk 'NR > 1 {print "  занято " $3 " из " $2 " (" $5 "), свободно " $4}'
    echo "Память:"
    free -h | awk '/^Mem/ {print "  занято " $3 " из " $2 ", доступно " $7}'
}

op_status() {
    local text
    text=$(status_text 2>&1)
    if [ "$INTERACTIVE" = 1 ]; then
        ui_show "Состояние системы" "$text"
    else
        printf '%s\n' "$text"
    fi
}
