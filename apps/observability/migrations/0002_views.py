# Представления — текстом из schema.sql, сырым SQL, а не через ORM (ADR-003, ADR-029).

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("observability", "0001_initial"),
        ("placements", "0001_initial"),
    ]

    operations = [
        # Валюты не складываются: API и LLM — в долларах, размещения — в евро.
        migrations.RunSQL(
            sql="""
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
            """,
            reverse_sql="DROP VIEW v_monthly_spend",
        ),
    ]
