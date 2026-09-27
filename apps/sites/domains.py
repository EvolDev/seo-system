"""Нормализация домена площадки — одна на всю систему (E1-01)."""

from urllib.parse import urlsplit


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
