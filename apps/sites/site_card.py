"""Карточка площадки на экране (E9-13, ADR-058): лента заметок, плитки, сводки разделов.

Только показ: данные собирает `_card_context` в `apps/sites/admin.py`, здесь —
подписи для человека. Время — по часовому поясу проекта.
"""

import datetime as dt
from collections.abc import Iterable
from dataclasses import dataclass

from django.utils import timezone

from apps.sites.models import SiteNote, SiteStatus
from apps.sites.offers import money

SYSTEM = "Система"

_MONTHS = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)

# Цвет плашки статуса: путь в работу — синий, договорились и размещали —
# зелёный, отказы — красный, аудит — жёлтый.
STATUS_TONES = {
    SiteStatus.NEW: "info",
    SiteStatus.VIEWED: "info",
    SiteStatus.IN_WORK: "ok",
    SiteStatus.ORDERED: "ok",
    SiteStatus.PLACED: "ok",
    SiteStatus.DISCARDED: "no",
    SiteStatus.REJECTED: "no",
    SiteStatus.BLACKLISTED: "no",
    SiteStatus.IN_WORK: "mark",
}


def when_text(moment: dt.datetime, now: dt.datetime | None = None) -> str:
    """«сегодня, 12:45», «вчера, 18:02», «3 октября, 18:02», «3 октября 2025, 18:02»."""
    local = timezone.localtime(moment)
    today = timezone.localtime(now).date() if now else timezone.localdate()
    clock = f"{local:%H:%M}"
    day = local.date()
    if day == today:
        return f"сегодня, {clock}"
    if day == today - dt.timedelta(days=1):
        return f"вчера, {clock}"
    date = f"{day.day} {_MONTHS[day.month - 1]}"
    if day.year != today.year:
        date += f" {day.year}"
    return f"{date}, {clock}"


def full_time(moment: dt.datetime) -> str:
    return f"{timezone.localtime(moment):%d.%m.%Y %H:%M}"


@dataclass(frozen=True)
class NoteRow:
    """Заметка в ленте: кто, к какому продукту, когда.

    Системная — без автора: пришла из файла (загрузка, таблица линкбилдинга).
    Заметку, которую система записала по нажатию человека (смена рабочей
    цены), подписывает он.
    """

    who: str
    letter: str
    system: bool
    source: str
    product: str
    when: str
    when_full: str
    iso: str
    body: str


def note_rows(notes: Iterable[SiteNote], now: dt.datetime | None = None) -> list[NoteRow]:
    rows = []
    for note in notes:
        author = note.author
        who = author.get_username() if author is not None else SYSTEM
        rows.append(
            NoteRow(
                who=who,
                letter=who[:1].upper() if author is not None else "",
                system=author is None,
                source=note.source or "",
                product=note.product.name if note.product is not None else "",
                when=when_text(note.created_at, now),
                when_full=full_time(note.created_at),
                iso=timezone.localtime(note.created_at).isoformat(),
                body=note.body,
            )
        )
    return rows


def notes_summary(rows: list[NoteRow]) -> str:
    """Сводка свёрнутых «Заметок»: «последняя — rodzedrim, сегодня, 12:45»."""
    if not rows:
        return "заметок нет"
    return f"последняя — {rows[0].who}, {rows[0].when}"


def price_summary(working: str, seller: str, offers: int, pending: int) -> str:
    """Сводка свёрнутой «Цены»: «€490 · LinkHub Media · предложений: 2, новых: 1»."""
    parts = [f"{working} · {seller}" if working else "рабочей цены нет"]
    if offers:
        count = f"предложений: {offers}"
        if pending:
            count += f", новых: {pending}"
        parts.append(count)
    return " · ".join(parts)


def euros(cents: int | None) -> str:
    """Евро после пересчёта — до целых: «€490»."""
    if cents is None:
        return ""
    return money(round(cents / 100) * 100, "EUR")


def fields_text(count: int) -> str:
    """«1 поле», «2 поля», «5 полей»."""
    tail = count % 100
    if 11 <= tail <= 14:
        word = "полей"
    elif count % 10 == 1:
        word = "поле"
    elif 2 <= count % 10 <= 4:
        word = "поля"
    else:
        word = "полей"
    return f"{count} {word}"
