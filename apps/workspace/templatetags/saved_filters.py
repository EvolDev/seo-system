"""Блок «Мои фильтры» над колонкой фильтров списка (E9-10, ADR-050).

`{% saved_filters_column cl block.super %}` в `admin/change_list.html`: берёт
колонку фильтров, которую нарисовала тема, и ставит блок первым внутри неё.
Колонку не копируем из шаблона темы — блок переживёт обновление пакета.
Нет колонки (у списка нет фильтров) или это окно выбора записи — блока нет.
"""

import re
from typing import Any

from django import template
from django.template.loader import render_to_string
from django.utils.safestring import SafeString, mark_safe

from apps.workspace.saved_filters import NAME_MAX, clean_query, same_query, screen_of, sets_of

register = template.Library()

# Открывающий тег колонки фильтров — у Django и у темы Admin Interface один.
COLUMN = re.compile(r'<nav id="changelist-filter"[^>]*>')


@register.simple_tag(takes_context=True)
def saved_filters_column(context: template.Context, cl: Any, column: str) -> SafeString:
    request = context.get("request")
    found = COLUMN.search(column)
    if request is None or found is None or context.get("is_popup"):
        # Разметка колонки — от шаблона темы, уже безопасная.
        return mark_safe(column)
    screen = screen_of(cl.opts.app_label, cl.opts.model_name)
    sets = list(sets_of(int(request.user.pk), screen))
    current = clean_query(request.META.get("QUERY_STRING", ""))
    selected = next((item.pk for item in sets if same_query(item.query, current)), None)
    block = render_to_string(
        "workspace/saved_filters.html",
        {
            "screen": screen,
            "user_id": request.user.pk,
            "sets": sets,
            "selected": selected,
            "list_path": request.path,
            "name_max": NAME_MAX,
        },
        request=request,
    )
    at = found.end()
    # Обе части — отрисованные шаблоны, значения в них уже экранированы.
    return mark_safe(column[:at] + block + column[at:])
