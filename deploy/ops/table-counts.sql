-- Число строк в каждой таблице базы — сверка после переноса (docs/18-SERVER-SETUP.md §5).
-- Одна строка на таблицу: «таблица|строк». Тот же запрос — в deploy/backup/lib.sh.
SELECT table_name,
       (xpath('/row/c/text()',
              query_to_xml(format('SELECT count(*) AS c FROM %I.%I', table_schema, table_name),
                           false, true, '')))[1]::text::bigint
FROM information_schema.tables
WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
ORDER BY table_name;
