"""Серость площадки в Google (E2-06, ADR-054).

В карточке площадки две кнопки открывают Google в новой вкладке: запрос
`site:домен` — сколько страниц площадки в индексе, и `site:домен "casino" OR
"poker" OR …` — сколько из них на серые темы. Условия второго запроса — набор
на языке площадки (`GrayTermSet`, ADR-074): язык из карточки Collaborator,
нет его — по зоне домена, набора для языка нет — английский. Другой набор
человек выбирает сам, выбор держится до конца сессии (`choose`).

Оценку «About N results» с обеих страниц в карточку подставляет расширение
браузера (`browser-extension/`) или человек руками. Сервер в Google не ходит:
запрос не из браузера человека Google встречает капчей, а у Serper оценки нет
(Q20). Доля = серые / всего; каждый замер — новая строка `gray_scans`
(снапшот), способ `manual`: число видел человек в своём браузере.

Домен в запросе — без схемы и `www.` (так он хранится): `site:домен` берёт и
`www.`, и поддомены. С `https://домен/` площадка на `www.` показала бы ноль.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal
from urllib.parse import urlencode

from apps.content.domain_settings import GrayZones
from apps.sites import countries
from apps.sites.languages import language_name, site_language
from apps.sites.models import GrayScan, GrayTermSet, MetricSource, Site

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

# Набор, когда для языка площадки своего нет.
FALLBACK_LANGUAGE = "en"
# Флаг языка — страна, где на нём говорят; код языка и страны совпадает не всегда
# (pt, de — совпадает, en, uk — нет). Нет здесь — флаг по коду языка, если он есть.
LANGUAGE_FLAGS: dict[str, str] = {
    "en": "gb",
    "uk": "ua",
    "cs": "cz",
    "da": "dk",
    "el": "gr",
    "he": "il",
    "ja": "jp",
    "ko": "kr",
    "zh": "cn",
    "sv": "se",
    "et": "ee",
    "ka": "ge",
    "kk": "kz",
    "be": "by",
    "sr": "rs",
    "sl": "si",
    "hi": "in",
    "ur": "pk",
    "fa": "ir",
    "ar": "sa",
    "vi": "vn",
    "ms": "my",
    "bn": "bd",
    "ta": "in",
    "te": "in",
    "tl": "ph",
    "jv": "id",
    "la": "va",
    "ca": "es",
    "sq": "al",
    "hy": "am",
    "bs": "ba",
}


def language_flag(code: str) -> str | None:
    """Код страны для флага языка: `en` → `gb`, `pt` → `pt`; флага нет — None."""
    return countries.flag_code(LANGUAGE_FLAGS.get(code, code))


@dataclass(frozen=True)
class Choice:
    """Какой набор условий у карточки и почему — подпись рядом с выбором."""

    term_set: GrayTermSet | None
    reason: str
    picked: bool = False


def choose(site: Site, sets: Sequence[GrayTermSet], picked: str | None) -> Choice:
    """Набор для карточки: выбранный человеком в эту сессию, иначе по языку площадки.

    Язык — из карточки Collaborator, нет его — по зоне домена. Для языка нет
    набора — английский, нет и его — первый по названию языка.
    """
    by_language = {term_set.language: term_set for term_set in sets}
    if picked is not None and picked in by_language:
        return Choice(by_language[picked], "выбран вами", picked=True)
    language, how = site_language(site)
    if language is not None and language in by_language:
        return Choice(by_language[language], how)
    fallback = by_language.get(FALLBACK_LANGUAGE) or min(
        sets, key=lambda term_set: language_name(term_set.language), default=None
    )
    if fallback is None:
        return Choice(None, "")
    taken = f"взят «{language_name(fallback.language)}»"
    if language:
        return Choice(fallback, f"для языка «{language_name(language)}» набора нет — {taken}")
    return Choice(fallback, f"язык площадки не известен — {taken}")


# Вставили запрос целиком: `site:… ("casino" OR "poker")` — условия в кавычках.
WHOLE_QUERY = re.compile(r'"\s+OR\s+"')


def clean_terms(text: str) -> list[str]:
    """Условия из поля набора: по одному на строку или запрос Google целиком.

    Пробелы внутри условия схлопываются, повтор без учёта регистра пропускается.
    Ни одного условия — ValueError.
    """
    lines = re.findall(r'"([^"]+)"', text) if WHOLE_QUERY.search(text) else text.splitlines()
    terms: list[str] = []
    for line in lines:
        term = " ".join(line.split())
        if term and term.casefold() not in (seen.casefold() for seen in terms):
            terms.append(term)
    if not terms:
        raise ValueError("Впишите хотя бы одно условие.")
    return terms


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
    language: str | None = None  # набор условий серого запроса


def record(site: Site, reading: Reading) -> GrayScan:
    """Новый замер серости площадки — строка `gray_scans`."""
    breakdown = {
        "queries": {"total": reading.total_query, "gray": reading.gray_query},
        "language": reading.language,
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
