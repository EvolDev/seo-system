"""Страны выгрузок Ahrefs и регионов «Площадок» (ADR-045).

Код — две буквы строчными, как его пишет Ahrefs: `us`, `gb`, `in`. Названия
— из django-countries: по-русски для экрана, по-английски для поиска (в
Ahrefs страны названы по-английски). Короткие названия и привычные
сокращения добавлены здесь — «США», а не «Соединенные Штаты Америки».
Флаги — из того же пакета, одной картинкой на все страны
(`static/flags/sprite-hq.png` и `sprite-hq.css`, классы `flag-u flag-_s`):
флаги-эмодзи Windows не рисует, а пользователи сидят в Opera и Edge под
Windows; одна картинка вместо 250 — список стран открывается сразу с флагами.
"""

from dataclasses import dataclass
from functools import cache
from pathlib import Path

import django_countries
from django.utils import translation
from django.utils.html import format_html
from django.utils.safestring import SafeString, mark_safe
from django_countries import countries

# Короче официального — так их называют в работе; и где перевод пакета
# в родительном падеже или со скобками («Мьянмы», «Сан - Марино»).
SHORT = {
    "us": "США",
    "ae": "ОАЭ",
    "kr": "Южная Корея",
    "mm": "Мьянма",
    "lr": "Либерия",
    "nc": "Новая Каледония",
    "sm": "Сан-Марино",
    "va": "Ватикан",
    "cd": "ДР Конго",
    "ps": "Палестина",
    "fm": "Микронезия",
    "vg": "Британские Виргинские острова",
    "vi": "Виргинские острова США",
    "cc": "Кокосовые острова",
    "fk": "Фолклендские острова",
    "sx": "Синт-Мартен",
    "mf": "Сен-Мартен",
    "sh": "Остров Святой Елены",
}
# Значок «все страны» вместо флага — у «Все страны» и «Все».
GLOBE = "globe"
# Как ещё ищут страну: сокращения и названия, которых нет в справочнике.
ALIASES = {
    "us": "usa америка соединённые штаты united states",
    "gb": "uk англия британия great britain",
    "ae": "uae эмираты",
    "cz": "czech republic",
    "kr": "korea корея",
    "ru": "russian federation",
}


@dataclass(frozen=True)
class Country:
    code: str  # us
    name: str  # США — по-русски, для экрана
    search: str  # всё, по чему страну находит поиск, строчными

    @property
    def label(self) -> str:
        return f"{self.name} · {self.code.upper()}"

    @property
    def flag(self) -> str | None:
        return flag_code(self.code)


@cache
def _flag_codes() -> frozenset[str]:
    # Флаги, что есть в пакете: по ним же собрана общая картинка.
    folder = Path(django_countries.__file__).parent / "static" / "flags"
    return frozenset(path.stem for path in folder.glob("*.gif") if len(path.stem) == 2)


def flag_code(code: str | None) -> str | None:
    """Код для флага (`data-flag`) или None — флага у пакета нет, покажем без него."""
    if not code:
        return None
    code = code.strip().lower()
    return code if code in _flag_codes() else None


def flag_html(code: str | None) -> SafeString:
    """Флаг строкой HTML: `us` → кусок общей картинки, `globe` — «все страны», нет — пусто."""
    if code == GLOBE:
        return mark_safe('<span class="seo-flag seo-flag-globe" aria-hidden="true"></span>')
    known = flag_code(code)
    if known is None:
        return mark_safe("")
    return format_html(
        '<span class="seo-flag flag-sprite flag-{} flag-_{}" aria-hidden="true"></span>',
        known[0],
        known[1],
    )


@cache
def _all() -> tuple[Country, ...]:
    # Справочник один на процесс: названия не меняются, язык экрана — русский.
    with translation.override("en"):
        english = {code.lower(): str(name) for code, name in countries}
    with translation.override("ru"):
        russian = {code.lower(): str(name) for code, name in countries}
    result = []
    for code, official in russian.items():
        name = SHORT.get(code, official)
        terms = [code, name, official, english.get(code, ""), ALIASES.get(code, "")]
        result.append(Country(code, name, " ".join(terms).lower().replace("ё", "е")))
    return tuple(sorted(result, key=lambda c: c.name))


def all_countries() -> tuple[Country, ...]:
    """Все страны по алфавиту русских названий."""
    return _all()


def get(code: str | None) -> Country | None:
    if not code:
        return None
    code = code.strip().lower()
    return next((country for country in _all() if country.code == code), None)


def name(code: str) -> str:
    """Название по коду; незнакомый код — сам код заглавными: так его видно и не теряется."""
    country = get(code)
    return country.name if country is not None else code.upper()


def is_known(code: str) -> bool:
    return get(code) is not None
