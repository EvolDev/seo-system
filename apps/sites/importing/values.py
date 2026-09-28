"""Разбор значений ячеек таблицы: числа, деньги, даты, категории.

Чистые функции без базы. Не разобралось — `ValueError` с понятным
текстом: вызывающий код кладёт строку в отчёт, импорт не падает.
Ячейка приходит из openpyxl как `str`, `int`, `float`, `datetime` или
`None`, поэтому на входе — `object` и проверки `isinstance`.
"""

import datetime as dt
import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from urllib.parse import urlsplit

from apps.sites.domains import normalize_domain

# Пробелы, которыми разделяют разряды: обычный, неразрывный, узкий неразрывный.
_SPACES = str.maketrans("", "", "   ")
# Запятая — разделитель разрядов, только если группы по три цифры:
# `84,307,672`. `3,5` — не целое, а дробь с запятой, её не угадываем.
_COMMA_GROUPED = re.compile(r"-?\d{1,3}(,\d{3})+")
_PLAIN_INT = re.compile(r"-?\d+")

# Метка «вне топ-100» в колонках позиций — не позиция (маппинг §2.2).
OUT_OF_TOP_MARK = 101

# Отказ для Convertio в «Комментарии к площадке»: «отбрасываем», «отбрасываю».
_REJECTION_STEM = "отбрасыва"
# Площадка отказала по заявке: «Отказали», «Отказались писать», «отклонили заявку».
_REFUSAL_STEMS = ("отказ", "отклон")

_YES_NO = {"да": True, "нет": False}


def is_blank(value: object) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def text(value: object) -> str | None:
    """Строка без пробелов по краям; пустая ячейка — None."""
    if is_blank(value):
        return None
    return str(value).strip()


def parse_int(value: object) -> int | None:
    """Целое из числа или из строки с разделителями: `84,307,672`, `24 195 783`."""
    if is_blank(value):
        return None
    if isinstance(value, bool):
        raise ValueError(f"не число: {value!r}")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value.is_integer():
            return int(value)
        raise ValueError(f"не целое число: {value!r}")
    if isinstance(value, str):
        cleaned = value.strip().translate(_SPACES)
        if _COMMA_GROUPED.fullmatch(cleaned):
            return int(cleaned.replace(",", ""))
        if _PLAIN_INT.fullmatch(cleaned):
            return int(cleaned)
    raise ValueError(f"не число: {value!r}")


def parse_cents(value: object) -> int | None:
    """Евро → целые центы с округлением: `18.2` → 1820 (ADR-009)."""
    if is_blank(value):
        return None
    amount: Decimal
    if isinstance(value, bool):
        raise ValueError(f"не сумма: {value!r}")
    if isinstance(value, int | float):
        # str() даёт кратчайшую запись float — 18.2, а не 18.19999…:
        # Decimal из неё точен, округление не съедает цент.
        amount = Decimal(str(value))
    elif isinstance(value, str):
        try:
            amount = Decimal(value.strip().translate(_SPACES))
        except InvalidOperation:
            raise ValueError(f"не сумма: {value!r}") from None
    else:
        raise ValueError(f"не сумма: {value!r}")
    if not amount.is_finite() or amount < 0:
        raise ValueError(f"не сумма: {value!r}")
    return int((amount * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def parse_announce_cents(value: object) -> int | None:
    """Цена анонса: число → центы, «бесплатно» → 0, пусто → None."""
    if isinstance(value, str) and value.strip().lower() == "бесплатно":
        return 0
    return parse_cents(value)


def parse_date(value: object) -> dt.date | None:
    """Дата `ДД.ММ.ГГГГ` или дата из ячейки Excel."""
    if is_blank(value):
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str):
        try:
            return dt.datetime.strptime(value.strip(), "%d.%m.%Y").date()
        except ValueError:
            pass
    raise ValueError(f"не дата ДД.ММ.ГГГГ: {value!r}")


def parse_yes_no(value: object) -> bool | None:
    """«Да» / «да» / «Нет» → bool, пусто → None: регистр в таблице разъезжается."""
    if is_blank(value):
        return None
    if isinstance(value, str) and value.strip().lower() in _YES_NO:
        return _YES_NO[value.strip().lower()]
    raise ValueError(f"ожидалось «да» или «нет»: {value!r}")


def split_categories(value: object) -> list[str]:
    """«Бизнес и финансы, СМИ (Новости), Общество, политика, законы» → три категории.

    Запятая бывает и внутри названия: «Кредитование, микрозаймы»,
    «Шопинг (сайты для покупок, купоны)». Новая категория начинается
    после запятой, только если запятая не в скобках и следующий
    фрагмент начинается с заглавной буквы (маппинг §1.1).
    """
    source = text(value)
    if source is None:
        return []
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    for index, char in enumerate(source):
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(depth - 1, 0)
        elif char == "," and depth == 0 and source[index + 1 :].lstrip()[:1].isupper():
            parts.append("".join(current).strip())
            current = []
            continue
        current.append(char)
    parts.append("".join(current).strip())
    return [part for part in parts if part]


def split_languages(value: object) -> list[str]:
    """«Украинский, Русский» → два языка, как в карточке."""
    source = text(value)
    if source is None:
        return []
    return [part.strip() for part in source.split(",") if part.strip()]


def parse_position(value: object) -> int | None:
    """Позиция в выдаче 1–100; `101` — метка «вне топ-100», хранится как None.

    Пустую ячейку вызывающий код отсеивает сам: у неё нет снапшота, а
    у `101` снапшот есть, только без позиции.
    """
    position = parse_int(value)
    if position is None:
        raise ValueError("пустая ячейка позиции")
    if position == OUT_OF_TOP_MARK:
        return None
    if not 1 <= position < OUT_OF_TOP_MARK:
        raise ValueError(f"позиция вне 1–100: {value!r}")
    return position


def is_url_on_domain(url: str, domain: str) -> bool:
    """Адрес `http(s)://` на домене площадки или его поддомене."""
    candidate = url.strip()
    if any(char.isspace() for char in candidate):
        return False
    parts = urlsplit(candidate)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False
    try:
        host = normalize_domain(parts.hostname)
    except ValueError:
        return False
    return host == domain or host.endswith("." + domain)


def is_rejection(comment: str | None) -> bool:
    """Комментарий к площадке — отказ для Convertio (маппинг §1.6)."""
    return comment is not None and _REJECTION_STEM in comment.lower()


def mentions_refusal(comment: str | None) -> bool:
    """В комментарии сказано, что площадка отказала по заявке (маппинг §1.5)."""
    return comment is not None and any(stem in comment.lower() for stem in _REFUSAL_STEMS)
