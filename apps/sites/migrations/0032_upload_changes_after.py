"""Журнал загрузки помнит, что она записала: отмена видит правки после загрузки.

Пользователь (09.11.2026): отмена загрузки возвращает изменённые ею строки целиком,
и правки, сделанные в них уже после загрузки, откатываются молча. У правки в
журнале теперь и `after` — изменённые колонки, как их записала загрузка; отмена
сравнивает с тем, что в строке сейчас (ADR-060, уточнение).
"""

from django.db import migrations, models

FUNCTION_SQL = """
CREATE OR REPLACE FUNCTION log_upload_change() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    upload bigint := NULLIF(current_setting('seo.upload_id', true), '')::bigint;
BEGIN
    IF upload IS NULL THEN
        RETURN NULL;
    END IF;
    IF TG_OP = 'INSERT' THEN
        INSERT INTO upload_changes (upload_id, table_name, row_id, op)
        VALUES (upload, TG_TABLE_NAME, NEW.id, 'I');
    ELSIF TG_OP = 'UPDATE' THEN
        -- Что записала загрузка — только изменённые колонки: по ним отмена узнаёт
        -- правки, сделанные в строке уже после загрузки.
        INSERT INTO upload_changes (upload_id, table_name, row_id, op, before, after)
        SELECT upload, TG_TABLE_NAME, OLD.id, 'U', to_jsonb(OLD),
               coalesce(jsonb_object_agg(n.key, n.value), '{}'::jsonb)
        FROM jsonb_each(to_jsonb(NEW)) AS n
        WHERE to_jsonb(OLD) -> n.key IS DISTINCT FROM n.value;
    ELSE
        INSERT INTO upload_changes (upload_id, table_name, row_id, op, before)
        VALUES (upload, TG_TABLE_NAME, OLD.id, 'D', to_jsonb(OLD));
    END IF;
    RETURN NULL;
END
$$;
"""

OLD_FUNCTION_SQL = """
CREATE OR REPLACE FUNCTION log_upload_change() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    upload bigint := NULLIF(current_setting('seo.upload_id', true), '')::bigint;
BEGIN
    IF upload IS NULL THEN
        RETURN NULL;
    END IF;
    IF TG_OP = 'INSERT' THEN
        INSERT INTO upload_changes (upload_id, table_name, row_id, op)
        VALUES (upload, TG_TABLE_NAME, NEW.id, 'I');
    ELSE
        INSERT INTO upload_changes (upload_id, table_name, row_id, op, before)
        VALUES (upload, TG_TABLE_NAME, OLD.id, left(TG_OP, 1), to_jsonb(OLD));
    END IF;
    RETURN NULL;
END
$$;
"""


class Migration(migrations.Migration):
    dependencies = [
        ("sites", "0031_upload_kind_collaborator"),
    ]

    operations = [
        migrations.AddField(
            model_name="uploadchange",
            name="after",
            field=models.JSONField(blank=True, null=True, verbose_name="записано"),
        ),
        migrations.RunSQL(FUNCTION_SQL, OLD_FUNCTION_SQL),
    ]
