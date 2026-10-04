"""Серость площадки в Google (E2-06, ADR-054).

В карточке площадки две кнопки открывают Google в новой вкладке: запрос
`site:домен` — сколько страниц площадки в индексе, и `site:домен "casino" OR
"poker" OR …` — сколько из них на серые темы. Условия второго запроса —
настройка `GRAY_TERMS`, их дописывают в «Настройках».

Оценку «About N results» с обеих страниц в карточку подставляет расширение
браузера (`browser-extension/`) или человек руками. Сервер в Google не ходит:
запрос не из браузера человека Google встречает капчей, а у Serper оценки нет
(Q20). Доля = серые / всего; каждый замер — новая строка `gray_scans`
(снапшот), способ `manual`: число видел человек в своём браузере.

Домен в запросе — без схемы и `www.` (так он хранится): `site:домен` берёт и
`www.`, и поддомены. С `https://домен/` площадка на `www.` показала бы ноль.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal
from urllib.parse import urlencode

from apps.content.domain_settings import GrayZones
from apps.sites.models import GrayScan, MetricSource, Site

GOOGLE_SEARCH = "https://www.google.com/search"
# Google учитывает в запросе первые 32 слова, остальные молча отбрасывает. Это
# свойство Google, не наш порог. Считается ли OR словом — не знаем (E2-06).
GOOGLE_WORD_LIMIT = 32
# Сколько примеров серых страниц хранить у замера: первая страница выдачи.
SAMPLE_LIMIT = 10

Zone = Literal["green", "yellow", "red"]
ZONE_TITLES: dict[Zone, str] = {
    "green": "зелёная зона",
    "yellow": "жёлтая зона",
    "red": "красная зона",
}

# Откуда числа замера: расширение браузера или человек вписал руками.
SOURCE_EXTENSION = "extension"
SOURCE_TYPED = "typed"


def total_query(domain: str) -> str:
    """Запрос «всего страниц в индексе»: `site:домен`."""
    return f"site:{domain}"


def quoted(term: str) -> str:
    """Условие в кавычках: `delta 8` → `"delta 8"`. С кавычками внутри — как ввели."""
    return term if '"' in term else f'"{term}"'


def gray_query(domain: str, terms: Sequence[str]) -> str:
    """Запрос «серые страницы»: `site:домен "casino" OR "poker" OR …`."""
    return f"{total_query(domain)} " + " OR ".join(quoted(term) for term in terms)


def google_url(query: str) -> str:
    """Адрес выдачи Google. `hl=en` — «About N results» всегда по-английски."""
    return f"{GOOGLE_SEARCH}?{urlencode({'q': query, 'hl': 'en'})}"


def word_count(query: str) -> int:
    """Слов в запросе так, как их считает лимит Google: `site:домен` — одно, OR — не слово."""
    return sum(1 for word in query.replace('"', " ").split() if word != "OR")


def gray_ratio(total: int, gray: int) -> Decimal | None:
    """Доля серых в процентах, два знака. В индексе ноль страниц — доли нет.

    Обе цифры — оценки Google, и серых бывает «больше», чем всего: тогда
    доля — 100%, а замер помечается (`over_total` в `breakdown`).
    """
    if total <= 0:
        return None
    ratio = Decimal(min(gray, total) * 100) / Decimal(total)
    return ratio.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def zone_of(ratio: Decimal | None, zones: GrayZones | None) -> Zone | None:
    """Зона доли: меньше green — зелёная, до yellow включительно — жёлтая, больше — красная."""
    if ratio is None or zones is None:
        return None
    if ratio < Decimal(str(zones.green)):
        return "green"
    if ratio <= Decimal(str(zones.yellow)):
        return "yellow"
    return "red"


def percent_text(ratio: Decimal | None) -> str:
    """3.20 → «3,2 %», 0.35 → «0,35 %», 100.00 → «100 %»; доли нет — «—»."""
    if ratio is None:
        return "—"
    text = f"{ratio:.2f}".rstrip("0").rstrip(".")
    return f"{text.replace('.', ',')} %"


def count_text(number: int | None) -> str:
    """1230 → «1 230» с неразрывным пробелом."""
    return "—" if number is None else f"{number:,}".replace(",", " ")


@dataclass(frozen=True)
class Reading:
    """Что пришло из Google: два числа, запросы, по которым искали, и примеры."""

    total: int
    gray: int
    total_query: str
    gray_query: str
    sample_urls: Sequence[str] = ()
    source: str = SOURCE_TYPED


def record(site: Site, reading: Reading) -> GrayScan:
    """Новый замер серости площадки — строка `gray_scans`."""
    breakdown = {
        "queries": {"total": reading.total_query, "gray": reading.gray_query},
        "source": reading.source,
        "over_total": reading.gray > reading.total,
    }
    return GrayScan.objects.create(
        site=site,
        total_indexed=reading.total,
        gray_hits=reading.gray,
        ratio=gray_ratio(reading.total, reading.gray),
        breakdown=breakdown,
        sample_urls=list(reading.sample_urls[:SAMPLE_LIMIT]) or None,
        method=MetricSource.MANUAL,
    )
