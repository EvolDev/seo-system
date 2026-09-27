"""Админка блока 3: позиции ключей как снапшоты (E1-02)."""

import datetime as dt

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.keywords.models import Keyword, KeywordPosition
from apps.sites.models import Product

pytestmark = pytest.mark.django_db

ADD_URL = reverse("admin:keywords_keywordposition_add")


@pytest.fixture
def keyword() -> Keyword:
    product = Product.objects.create(name="Convertio", domain="convertio.co")
    return Keyword.objects.create(product=product, keyword="convert", target_url="https://x")


def _form(keyword: Keyword, position: int, date: dt.date) -> dict[str, str]:
    return {
        "keyword": str(keyword.pk),
        "position": str(position),
        "country": "US",
        "source": "manual",
        "checked_at": date.isoformat(),
    }


def _change_url(position: KeywordPosition) -> str:
    return reverse("admin:keywords_keywordposition_change", args=[position.pk])


def test_add_form_suggests_today(admin_client: Client) -> None:
    response = admin_client.get(ADD_URL)
    assert response.context["adminform"].form.initial["checked_at"] == timezone.localdate()


def test_same_date_is_a_form_error(admin_client: Client, keyword: Keyword) -> None:
    date = dt.date(2026, 9, 22)
    KeywordPosition.objects.create(keyword=keyword, position=7, checked_at=date)
    response = admin_client.post(ADD_URL, _form(keyword, 5, date))
    assert response.status_code == 200
    assert response.context["adminform"].form.non_field_errors()
    assert list(KeywordPosition.objects.values_list("position", flat=True)) == [7]


def test_save_as_new_with_new_date(admin_client: Client, keyword: Keyword) -> None:
    old = KeywordPosition.objects.create(
        keyword=keyword, position=7, checked_at=dt.date(2026, 9, 15)
    )
    data = {**_form(keyword, 5, dt.date(2026, 9, 22)), "_saveasnew": "1"}
    assert admin_client.post(_change_url(old), data).status_code == 302
    history = keyword.positions.order_by("checked_at").values_list("position", flat=True)
    assert list(history) == [7, 5]
    # Прежняя позиция теперь не последняя — только просмотр.
    assert admin_client.post(_change_url(old), _form(keyword, 1, old.checked_at)).status_code == 403


def test_latest_is_per_country(admin_client: Client, keyword: Keyword) -> None:
    us = KeywordPosition.objects.create(
        keyword=keyword, position=7, checked_at=dt.date(2026, 9, 15)
    )
    KeywordPosition.objects.create(
        keyword=keyword, position=3, country="DE", checked_at=dt.date(2026, 9, 22)
    )
    # Более свежая позиция в Германии не делает позицию в США устаревшей.
    response = admin_client.get(_change_url(us))
    assert response.context["has_change_permission"] is True
