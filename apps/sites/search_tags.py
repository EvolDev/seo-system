"""Поле поиска столбиком доменов над списком (`ModelAdmin.domain_search`)."""

from typing import Any

from django import template

from apps.sites.display import copy_column_html

register = template.Library()

# Выше поле не растёт — прокрутка: список под ним не уезжает вниз.
MAX_ROWS = 8


@register.simple_tag
def domain_search(cl: Any) -> dict[str, Any] | None:
    """Поле поиска и домены вставки, которых нет в списке.

    None — у списка поиск обычный, по слову. Ненайденные — с учётом фильтров
    списка: при выбранном статусе домен с другим статусом тоже «нет в списке».
    """
    admin = cl.model_admin
    field = getattr(admin, "domain_search", None)
    if field is None:
        return None
    text = cl.query.strip()
    missing: list[str] = []
    domains = admin.pasted_domains(text)
    if domains:
        found = set(cl.queryset.order_by().values_list(field, flat=True).distinct())
        missing = [domain for domain in domains if domain not in found]
    return {
        "text": text,
        "rows": min(max(len(text.splitlines()), 1), MAX_ROWS),
        "max_rows": MAX_ROWS,
        "missing": missing,
        "copy": copy_column_html(missing) if missing else "",
    }
