"""Разбор значений из прайсов: деньги, числа, тип ссылки, услуга, валюта, домен.

Чистые функции без базы. Не разобралось — `ValueError` с понятным текстом:
строка уходит в «Ошибки», загрузка не падает. Ячейка из xlsx приходит
числом, строкой или датой, из csv — всегда строкой.

Записи, которые встречаются в прайсах: `$1 200`, `$170.00`, `311,81`,
`1,200.50`, `31.9K`, `1 072` с неразрывным пробелом, `Do-Follow` /
`Do Follow` / `Dofollow`, адрес вместо домена.
"""

import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from urllib.parse import urlsplit

from apps.sites.domains import normalize_domain
from apps.sites.models import PlacementType
from apps.sites.uploads.files import is_blank, show

# Пробелы-разделители разрядов: обычный, неразрывный, узкий неразрывный, тонкий.
_SPACES = str.maketrans("", "", " \xa0  ")
# Ячейки «цены нет»: прочерки и «n/a».
_NONE_MARKS = {"-", "—", "–", "n/a", "na", "нет", "none", "null"}
_FREE_MARKS = {"free", "бесплатно", "0"}
# Запятая — разделитель разрядов, только если группы по три цифры: `1,200`, `84,307,672`.
_COMMA_GROUPED = re.compile(r"\d{1,3}(,\d{3})+")
_DOT_GROUPED = re.compile(r"\d{1,3}(\.\d{3}){2,}")
_NUMBER = re.compile(r"\d+(\.\d+)?")
_SUFFIXES = {
    "k": Decimal(1000),
    "m": Decimal(1_000_000),
    "к": Decimal(1000),
    "м": Decimal(1_000_000),
}

# Валюта по знаку или коду — в заголовке колонки или в самих значениях.
CURRENCY_MARKS: tuple[tuple[str, str], ...] = (
    ("$", "USD"),
    ("usd", "USD"),
    ("долл", "USD"),
    ("€", "EUR"),
    ("eur", "EUR"),
    ("евро", "EUR"),
    ("£", "GBP"),
    ("gbp", "GBP"),
    ("₴", "UAH"),
    ("uah", "UAH"),
    ("грн", "UAH"),
    ("₽", "RUB"),
    ("rub", "RUB"),
    ("руб", "RUB"),
)
_CURRENCY_CHARS = re.compile(r"[$€£₴₽]|usd|eur|gbp|uah|rub|грн|руб|евро|долл\w*", re.IGNORECASE)

_DOFOLLOW = {"dofollow", "do follow", "do-follow", "df", "follow"}
_NOFOLLOW = {"nofollow", "no follow", "no-follow", "nf"}

# «Both» — за эту цену и публикация, и вставка: пишем публикацией, она в
# приоритете (Q21, ответ пользователя 01.10.2026).
BOTH = "both"
_SERVICES = {
    "guest post": PlacementType.GUEST_POST,
    "guest posts": PlacementType.GUEST_POST,
    "guestpost": PlacementType.GUEST_POST,
    "guest-post": PlacementType.GUEST_POST,
    "gp": PlacementType.GUEST_POST,
    "post": PlacementType.GUEST_POST,
    "article": PlacementType.GUEST_POST,
    "sponsored post": PlacementType.GUEST_POST,
    "публикация": PlacementType.GUEST_POST,
    "статья": PlacementType.GUEST_POST,
    "гостевой пост": PlacementType.GUEST_POST,
    BOTH: PlacementType.GUEST_POST,
    "link insertion": PlacementType.LINK_INSERTION,
    "link insert": PlacementType.LINK_INSERTION,
    "linkinsert": PlacementType.LINK_INSERTION,
    "linkinsertion": PlacementType.LINK_INSERTION,
    "link-insertion": PlacementType.LINK_INSERTION,
    "li": PlacementType.LINK_INSERTION,
    "niche edit": PlacementType.LINK_INSERTION,
    "niche edits": PlacementType.LINK_INSERTION,
    "вставка": PlacementType.LINK_INSERTION,
    "вставка ссылки": PlacementType.LINK_INSERTION,
}


def is_none_mark(value: object) -> bool:
    return is_blank(value) or (isinstance(value, str) and value.strip().lower() in _NONE_MARKS)


def currency_in(text: str) -> str | None:
    """Валюта по знаку или коду в тексте: «Цена, $» → USD, «…, EUR» → EUR."""
    lowered = text.lower()
    for mark, code in CURRENCY_MARKS:
        if mark in lowered:
            return code
    return None


def parse_money(value: object, *, free_is_zero: bool = False) -> int | None:
    """Сумма → целые центы: `$1 200` → 120000, `311,81` → 31181, `18.2` → 1820.

    `free_is_zero` — для написания и анонса: «free», «бесплатно» → 0.
    Валюту функция не определяет — её берёт загрузка для всего файла.
    """
    if is_none_mark(value):
        return None
    if isinstance(value, bool):
        raise ValueError(f"не сумма: {show(value)!r}")
    if isinstance(value, int | float):
        # str() даёт кратчайшую запись float — 18.2, а не 18.19999…
        amount = Decimal(str(value))
    elif isinstance(value, str):
        cleaned = value.strip().lower()
        if free_is_zero and cleaned in _FREE_MARKS:
            return 0
        amount = _decimal(_CURRENCY_CHARS.sub("", cleaned).translate(_SPACES), value)
    else:
        raise ValueError(f"не сумма: {show(value)!r}")
    if not amount.is_finite() or amount < 0:
        raise ValueError(f"не сумма: {show(value)!r}")
    return int((amount * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def parse_number(value: object) -> int | None:
    """Целое из `21 400`, `1 072`, `31.9K`, `84,307,672`, `35.0` — с округлением."""
    if is_none_mark(value):
        return None
    if isinstance(value, bool):
        raise ValueError(f"не число: {show(value)!r}")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(Decimal(str(value)).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    if not isinstance(value, str):
        raise ValueError(f"не число: {show(value)!r}")
    cleaned = value.strip().lower().translate(_SPACES)
    factor = Decimal(1)
    if cleaned[-1:] in _SUFFIXES:
        factor = _SUFFIXES[cleaned[-1]]
        cleaned = cleaned[:-1]
    amount = _decimal(cleaned, value) * factor
    return int(amount.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def parse_link_type(value: object) -> str | None:
    """«Do-Follow», «Do Follow», «Dofollow» → dofollow; «No-Follow» → nofollow."""
    if is_none_mark(value):
        return None
    cleaned = " ".join(show(value).lower().replace("_", " ").split())
    if cleaned in _DOFOLLOW:
        return "dofollow"
    if cleaned in _NOFOLLOW:
        return "nofollow"
    raise ValueError(f"тип ссылки не распознан: {show(value)!r}")


def parse_service(value: object) -> PlacementType | None:
    """Услуга строки: «Guest post», «Link insertion», «Both» (→ публикация)."""
    if is_none_mark(value):
        return None
    cleaned = " ".join(show(value).lower().split())
    if cleaned in _SERVICES:
        return _SERVICES[cleaned]
    raise ValueError(f"услуга не распознана: {show(value)!r}")


def is_both(value: object) -> bool:
    return isinstance(value, str) and " ".join(value.lower().split()) == BOTH


def parse_domain(value: object) -> tuple[str, bool]:
    """Домен площадки и признак «в файле адрес, а не домен».

    Адрес — когда есть путь или параметры: `https://site.com/blog/`.
    Схема и `www.` — обычная запись домена, о ней не сообщаем.
    """
    raw = show(value)
    if not raw:
        raise ValueError("нет площадки")
    if "@" in raw or any(char.isspace() for char in raw):
        raise ValueError(f"не домен: {raw!r}")
    try:
        domain = normalize_domain(raw)
    except ValueError:
        raise ValueError(f"не домен: {raw!r}") from None
    if "." not in domain or domain.startswith(".") or ".." in domain:
        raise ValueError(f"не домен: {raw!r}")
    parts = urlsplit(raw if "//" in raw else "//" + raw)
    is_url = parts.path not in ("", "/") or bool(parts.query)
    return domain, is_url


def _decimal(cleaned: str, original: object) -> Decimal:
    """Число из строки без пробелов: точка или запятая — дробная часть или разряды."""
    text = cleaned
    if "," in text and "." in text:
        # Обе: последняя — дробная часть. `1,200.50` и `1.200,50`.
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        text = text.replace(",", "") if _COMMA_GROUPED.fullmatch(text) else text.replace(",", ".")
    elif _DOT_GROUPED.fullmatch(text):
        text = text.replace(".", "")
    if not _NUMBER.fullmatch(text):
        raise ValueError(f"не число: {show(original)!r}")
    try:
        return Decimal(text)
    except InvalidOperation:
        raise ValueError(f"не число: {show(original)!r}") from None
