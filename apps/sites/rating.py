"""Оценка площадки звёздочкой, 1–5 (E1-21, ADR-064).

Оценка общая для площадки: оценивается сама площадка как партнёр, а не
пара с продуктом. Одна строка на человека — передумал, строка
перезаписывается; история оценок не ведётся (решение пользователя
07.10.2026).

Среднее здесь не считается: его даёт `v_site_latest` одной группировкой
на все площадки. Тут только запись и своя оценка строки.
"""

from decimal import Decimal
from typing import Any

from django.db.models import Avg, Count, OuterRef, QuerySet, Subquery
from django.db.models.functions import Round
from django.utils import timezone

from apps.sites.models import Site, SiteRating

MIN_VALUE = 1
MAX_VALUE = 5

#: Имя аннотации со своей оценкой — одно на оба списка.
MINE = "my_rating"


def clean_value(raw: str | None) -> int | None:
    """Оценка из формы: 1–5, или None — «снять оценку».

    Снимает только пустое поле. Число вне 1–5 и нечисло — ошибка
    (`ValueError`), а не снятие: иначе опечатка молча стёрла бы оценку.
    """
    text = (raw or "").strip()
    if not text:
        return None
    value = int(text)
    if not MIN_VALUE <= value <= MAX_VALUE:
        raise ValueError(f"оценка вне {MIN_VALUE}–{MAX_VALUE}: {value}")
    return value


def set_rating(site: Site, user: Any, value: int | None) -> None:
    """Поставить оценку или снять её. Повторная оценка заменяет прежнюю."""
    if value is None:
        SiteRating.objects.filter(site=site, user=user).delete()
        return
    SiteRating.objects.update_or_create(
        site=site, user=user, defaults={"value": value, "updated_at": timezone.now()}
    )


def summary(site: Site) -> tuple[Decimal | None, int]:
    """Среднее и сколько оценок — после записи, чтобы ответить странице."""
    rows = SiteRating.objects.filter(site=site)
    count = rows.count()
    if not count:
        return None, 0
    average = rows.aggregate(value=Round(Avg("value"), 1))["value"]
    return average, count


def with_summary(queryset: QuerySet[Any], site_field: str = "site_id") -> QuerySet[Any]:
    """Среднее и счёт оценок для списка, где представления нет («Размещения»).

    Два скоррелированных подзапроса на строку. В «Площадках» так делать
    нельзя — там 45 000 строк каталога и своё представление, — а здесь на
    странице полсотни размещений рабочего продукта.
    """
    rows = SiteRating.objects.filter(site_id=OuterRef(site_field)).values("site_id")
    annotated: QuerySet[Any] = queryset.annotate(
        rating_avg=Subquery(rows.annotate(v=Round(Avg("value"), 1)).values("v")[:1]),
        rating_count=Subquery(rows.annotate(n=Count("*")).values("n")[:1]),
    )
    return annotated


def with_mine(queryset: QuerySet[Any], user: Any, site_field: str = "site_id") -> QuerySet[Any]:
    """Своя оценка строки — подзапросом: в представлении её нет и быть не может."""
    if not user.is_authenticated:
        return queryset
    mine = SiteRating.objects.filter(site_id=OuterRef(site_field), user=user)
    annotated: QuerySet[Any] = queryset.annotate(**{MINE: Subquery(mine.values("value")[:1])})
    return annotated
