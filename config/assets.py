"""Ссылки на нашу статику с версией — браузер не держит старую копию (E1-10).

Сервер отдаёт статику без заголовков кеширования, и браузер сам решает,
сколько держать файл: после правки скрипт приходил новый, а стили — старые,
и экран разъезжался (02.10.2026). К адресу добавляется `?v=<время изменения
файла>`: файл изменился — адрес другой, браузер берёт свежий.

- В шаблонах — `{% load seo_assets %}{% asset 'seo/offers.css' %}`.
- В `Media` админки — `Css("seo/offers.css")`, `Js("seo/site-card.js")`:
  Django рисует объекты с `__html__` как есть, и версия считается при
  каждой отрисовке страницы, а не при запуске сервера.
"""

import os
from dataclasses import dataclass

from django import template
from django.contrib.staticfiles import finders
from django.templatetags.static import static
from django.utils.html import format_html
from django.utils.safestring import SafeString

register = template.Library()


def versioned(path: str) -> str:
    """Адрес файла статики с версией по времени его изменения."""
    found = finders.find(path)
    if isinstance(found, list):
        found = found[0] if found else None
    version = int(os.path.getmtime(found)) if found else 0
    return f"{static(path)}?v={version}"


@register.simple_tag
def asset(path: str) -> str:
    return versioned(path)


# frozen — объекты сравниваются и хешируются: Media склеивает и убирает повторы.
@dataclass(frozen=True)
class Css:
    path: str

    def __html__(self) -> SafeString:
        return format_html('<link href="{}" media="all" rel="stylesheet">', versioned(self.path))


@dataclass(frozen=True)
class Js:
    path: str

    def __html__(self) -> SafeString:
        return format_html('<script src="{}"></script>', versioned(self.path))
