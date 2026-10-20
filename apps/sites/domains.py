"""Нормализация домена площадки — одна на всю систему (E1-01)."""

import re
from urllib.parse import urlsplit

# Между доменами вставленного столбика: перевод строки, пробел, табуляция
# (две колонки из таблицы), запятая, точка с запятой.
_PASTE_SEPARATORS = re.compile(r"[\s,;]+")


def normalize_domain(value: str) -> str:
    """Домен в каноническом виде: нижний регистр, без схемы, `www.` и пути.

    `HTTPS://WWW.Example.com/` и `example.com` дают `example.com`. Путь,
    порт и параметры отбрасываются: человек вставляет в поиск адрес
    статьи целиком, а площадка — это домен.
    """
    text = value.strip().lower()
    # urlsplit находит хост только после «//»: у голого домена его нет.
    if "//" not in text:
        text = "//" + text
    host = urlsplit(text).hostname or ""
    host = host.rstrip(".")
    if host.startswith("www."):
        host = host[len("www.") :]
    if not host:
        raise ValueError(f"Не удалось выделить домен из {value!r}")
    return host


def pasted_domains(text: str) -> list[str] | None:
    """Домены из вставленного в поиск столбика — для поиска списком.

    Разделители — `_PASTE_SEPARATORS`; каждый домен — как `normalize_domain`,
    без повторов, в порядке вставки. Меньше двух доменов — None: это обычный
    поиск по слову. Кусок без точки (заголовок столбца «Domain», «lensa» из
    опечатки «lensa,com») остаётся в списке: строк он не найдёт, и экран
    покажет его среди ненайденных. Кусок без букв и цифр («—» пустой ячейки)
    пропускается.
    """
    found: dict[str, None] = {}
    for piece in _PASTE_SEPARATORS.split(text):
        if not any(char.isalnum() for char in piece):
            continue
        try:
            domain = normalize_domain(piece)
        except ValueError:
            domain = piece.lower()
        found[domain] = None
    domains = list(found)
    return domains if sum("." in domain for domain in domains) >= 2 else None
