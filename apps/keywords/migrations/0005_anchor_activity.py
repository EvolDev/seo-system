"""Отключённые анкоры видны в справочнике; статистика выбирает активные."""

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
OLD_VIEW = """
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
    k.share
FROM keywords k
LEFT JOIN placement_links pl ON pl.keyword_id = k.id
LEFT JOIN placements p ON p.id = pl.placement_id
WHERE k.is_active
GROUP BY k.id;
"""


class Migration(migrations.Migration):
    dependencies = [("keywords", "0004_anchor_shares"), ("sites", "0027_offer_link_type")]
    operations = [
        migrations.RunSQL(VIEW, reverse_sql="DROP VIEW v_keyword_coverage;\n" + OLD_VIEW),
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AddField(
                    model_name="keywordcoverage",
                    name="is_active",
                    field=models.BooleanField("активен", default=True),
                ),
            ]
        ),
    ]
