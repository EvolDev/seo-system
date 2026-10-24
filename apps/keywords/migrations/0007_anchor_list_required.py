# Список анкоров обязателен (ADR-075): текст анкора уникален внутри списка, а
# не продукта — «convert» есть и у США, и у Бразилии. Позиция в
# v_keyword_coverage — по стране списка. Списки заводят загрузки, поэтому у
# таблицы — триггеры журнала загрузок (ADR-060): отмена загрузки убирает и список.

import django.db.models.deletion
from django.db import migrations, models

VIEW = """
CREATE OR REPLACE VIEW v_keyword_coverage AS
SELECT
    k.id,
    k.product_id,
    k.keyword,
    k.tool,
    k.volume,
    k.target_url,
    (SELECT position FROM keyword_positions kp
      WHERE kp.keyword_id = k.id AND kp.country = upper(al.country)
      ORDER BY checked_at DESC LIMIT 1) AS last_position,
    COUNT(pl.id) FILTER (WHERE p.status = 'placed') AS links_placed,
    COUNT(pl.id) FILTER (WHERE p.status IN ('in_work','ordered','writing')) AS links_waiting,
    k.global_volume,
    k.page_type,
    k.anchor_type::text AS anchor_type,
    k.share,
    k.is_active,
    k.anchor_list_id
FROM keywords k
JOIN anchor_lists al ON al.id = k.anchor_list_id
LEFT JOIN placement_links pl ON pl.keyword_id = k.id
LEFT JOIN placements p ON p.id = pl.placement_id
GROUP BY k.id, al.id;
"""
OLD_VIEW = """
DROP VIEW v_keyword_coverage;
CREATE VIEW v_keyword_coverage AS
SELECT
    k.id,
    k.product_id,
    k.keyword,
    k.tool,
    k.volume,
    k.target_url,
    (SELECT position FROM keyword_positions kp
      WHERE kp.keyword_id = k.id AND kp.country = 'US'
      ORDER BY checked_at DESC LIMIT 1) AS last_position,
    COUNT(pl.id) FILTER (WHERE p.status = 'placed') AS links_placed,
    COUNT(pl.id) FILTER (WHERE p.status IN ('in_work','ordered','writing')) AS links_waiting,
    k.global_volume,
    k.page_type,
    k.anchor_type::text AS anchor_type,
    k.share,
    k.is_active
FROM keywords k
LEFT JOIN placement_links pl ON pl.keyword_id = k.id
LEFT JOIN placements p ON p.id = pl.placement_id
GROUP BY k.id;
"""
TRIGGERS = """
CREATE TRIGGER anchor_lists_upload_log AFTER INSERT OR DELETE ON anchor_lists
    FOR EACH ROW EXECUTE FUNCTION log_upload_change();
CREATE TRIGGER anchor_lists_upload_log_update AFTER UPDATE ON anchor_lists
    FOR EACH ROW WHEN (OLD.* IS DISTINCT FROM NEW.*) EXECUTE FUNCTION log_upload_change();
"""
DROP_TRIGGERS = """
DROP TRIGGER anchor_lists_upload_log ON anchor_lists;
DROP TRIGGER anchor_lists_upload_log_update ON anchor_lists;
"""


class Migration(migrations.Migration):
    dependencies = [
        ("keywords", "0006_anchor_lists"),
    ]

    operations = [
        migrations.AlterField(
            model_name="keyword",
            name="anchor_list",
            field=models.ForeignKey(
                db_index=False,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="keywords",
                to="keywords.anchorlist",
                verbose_name="список анкоров",
            ),
        ),
        migrations.RemoveConstraint(
            model_name="keyword",
            name="keywords_product_id_keyword_key",
        ),
        migrations.AddConstraint(
            model_name="keyword",
            constraint=models.UniqueConstraint(
                fields=("anchor_list", "keyword"),
                name="keywords_anchor_list_id_keyword_key",
                violation_error_message="Такой анкор в этом списке уже есть.",
            ),
        ),
        migrations.RunSQL(VIEW, reverse_sql=OLD_VIEW),
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AddField(
                    model_name="keywordcoverage",
                    name="anchor_list",
                    field=models.ForeignKey(
                        db_constraint=False,
                        on_delete=django.db.models.deletion.DO_NOTHING,
                        related_name="+",
                        to="keywords.anchorlist",
                        verbose_name="список анкоров",
                    ),
                ),
            ]
        ),
        migrations.RunSQL(TRIGGERS, reverse_sql=DROP_TRIGGERS),
    ]
