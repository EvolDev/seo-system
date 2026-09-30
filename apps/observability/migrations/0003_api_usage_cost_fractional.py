# Стоимость в api_usage — центы с долями (ADR-040, schema.sql 1.5).
#
# Postgres не меняет тип колонки, на которой построено представление:
# v_monthly_spend снимаем и создаём заново тем же текстом (0002_views).

from django.db import migrations, models

MONTHLY_SPEND = """
    CREATE VIEW v_monthly_spend AS
    SELECT date_trunc('month', created_at)::date AS month,
           provider AS item, currency, sum(cost_cents) AS cost_cents
    FROM api_usage
    GROUP BY 1, 2, 3
    UNION ALL
    SELECT date_trunc('month', created_at)::date, 'llm:' || model, currency, sum(cost_cents)
    FROM llm_calls
    GROUP BY 1, 2, 3
    UNION ALL
    SELECT date_trunc('month', published_at)::date, 'placements',
           coalesce(currency, 'EUR'), sum(price_paid_cents)
    FROM placements
    WHERE status = 'published' AND published_at IS NOT NULL
    GROUP BY 1, 3;
"""


class Migration(migrations.Migration):
    dependencies = [
        ("observability", "0002_views"),
    ]

    operations = [
        migrations.RunSQL(sql="DROP VIEW v_monthly_spend", reverse_sql=MONTHLY_SPEND),
        migrations.AlterField(
            model_name="apiusage",
            name="cost_cents",
            field=models.DecimalField(
                blank=True,
                decimal_places=4,
                max_digits=14,
                null=True,
                verbose_name="стоимость, центы",
            ),
        ),
        migrations.RunSQL(sql=MONTHLY_SPEND, reverse_sql="DROP VIEW v_monthly_spend"),
    ]
