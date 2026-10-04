"""Окно выгрузки над списком админки (E1-06, ADR-053): `{% export_tools cl %}`.

Кнопка «Выгрузить» и окно под ней: галочки колонок по разделам (снятые в
прошлый раз — сняты, их помнит сессия), сколько строк уйдёт в файл, Excel и
CSV. Колонки отдаёт админка списка — `export_choices(request)`; поведение окна
— `seo/export.js`.
"""

from typing import Any

from django import template
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.safestring import SafeString

from config import export

register = template.Library()


@register.simple_tag(takes_context=True)
def export_tools(context: template.Context, cl: Any) -> SafeString:
    request = context["request"]
    model_admin = cl.model_admin
    off = export.unchecked(request, export.list_key(model_admin))
    groups: dict[str, list[tuple[export.Choice, bool]]] = {}
    for choice in model_admin.export_choices(request):
        groups.setdefault(choice.group, []).append((choice, choice.key not in off))
    name = f"admin:{export.url_name(model_admin)}"
    query = cl.get_query_string()
    return render_to_string(
        "admin/seo_export_tools.html",
        {
            "xlsx_url": reverse(name, args=[export.Format.XLSX.value]) + query,
            "csv_url": reverse(name, args=[export.Format.CSV.value]) + query,
            "groups": list(groups.items()),
            "all_checked": all(checked for items in groups.values() for _, checked in items),
            "total": cl.result_count,
        },
        request=request,
    )
