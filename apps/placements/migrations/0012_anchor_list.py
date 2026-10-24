# Список анкоров размещения — регион (ADR-075). Размещения, у ссылок которых уже
# выбраны анкоры, получают список этих анкоров (до регионов он один — «США»).
# Остальные остаются пустыми: карточка угадает регион по языку площадки, и
# бразильские площадки в работе встанут на Бразилию, когда её список загрузят.

from typing import Any

import django.db.models.deletion
from django.db import migrations, models

FILL = """
UPDATE placements p
SET anchor_list_id = linked.anchor_list_id
FROM (
    SELECT pl.placement_id, min(k.anchor_list_id) AS anchor_list_id
    FROM placement_links pl
    JOIN keywords k ON k.id = pl.keyword_id
    GROUP BY pl.placement_id
) linked
WHERE linked.placement_id = p.id;
"""


def fill(apps: Any, schema_editor: Any) -> None:
    schema_editor.execute(FILL)


def clear(apps: Any, schema_editor: Any) -> None:
    schema_editor.execute("UPDATE placements SET anchor_list_id = NULL;")


class Migration(migrations.Migration):
    dependencies = [
        ("keywords", "0007_anchor_list_required"),
        ("placements", "0011_price"),
    ]

    operations = [
        migrations.AddField(
            model_name="placement",
            name="anchor_list",
            field=models.ForeignKey(
                blank=True,
                db_index=False,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="placements",
                to="keywords.anchorlist",
                verbose_name="список анкоров",
            ),
        ),
        migrations.RunPython(fill, clear),
    ]
