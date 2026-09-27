"""Блок 3: ключи продукта и снапшоты позиций (E1-02)."""

import datetime as dt

import pytest
from django.db import IntegrityError, connection, transaction

from apps.keywords.models import Keyword, KeywordPosition
from apps.sites.models import MetricSource, Product

pytestmark = pytest.mark.django_db


@pytest.fixture
def convertio() -> Product:
    return Product.objects.create(name="Convertio", domain="convertio.co")


@pytest.fixture
def keyword(convertio: Product) -> Keyword:
    return Keyword.objects.create(
        product=convertio, keyword="mp4 to mp3", target_url="https://convertio.co/mp4-mp3/"
    )


def _db_today() -> dt.date:
    with connection.cursor() as cursor:
        cursor.execute("SELECT CURRENT_DATE")
        today: dt.date = cursor.fetchone()[0]
    return today


class TestKeyword:
    def test_same_keyword_in_other_product(self, keyword: Keyword) -> None:
        clideo = Product.objects.create(name="Clideo", domain="clideo.com")
        Keyword.objects.create(product=clideo, keyword=keyword.keyword, target_url="https://x")
        assert Keyword.objects.filter(keyword=keyword.keyword).count() == 2

    def test_duplicate_in_same_product_rejected(self, keyword: Keyword) -> None:
        with pytest.raises(IntegrityError), transaction.atomic():
            Keyword.objects.create(
                product=keyword.product, keyword=keyword.keyword, target_url="https://x"
            )

    def test_tool_is_free_text(self, keyword: Keyword) -> None:
        # Разделы у каждого продукта свои (TOOL_CATEGORIES), не перечисление.
        keyword.tool = "Раздел, которого нет у Convertio"
        keyword.full_clean()
        keyword.save()


class TestPosition:
    def test_defaults(self, keyword: Keyword) -> None:
        position = KeywordPosition.objects.create(keyword=keyword, position=7)
        position.refresh_from_db()
        assert position.country == "US"
        assert position.source == MetricSource.AHREFS_API
        assert position.checked_at == _db_today()

    def test_out_of_top_100_is_empty(self, keyword: Keyword) -> None:
        position = KeywordPosition.objects.create(keyword=keyword, position=None)
        position.refresh_from_db()
        assert position.position is None

    def test_same_date_twice_is_not_duplicated(self, keyword: Keyword) -> None:
        date = dt.date(2026, 9, 22)
        KeywordPosition.objects.create(keyword=keyword, position=7, checked_at=date)
        with pytest.raises(IntegrityError), transaction.atomic():
            KeywordPosition.objects.create(keyword=keyword, position=5, checked_at=date)
        assert list(KeywordPosition.objects.values_list("position", flat=True)) == [7]

    def test_other_country_or_date_is_a_new_snapshot(self, keyword: Keyword) -> None:
        date = dt.date(2026, 9, 22)
        KeywordPosition.objects.create(keyword=keyword, position=7, checked_at=date)
        KeywordPosition.objects.create(keyword=keyword, position=3, country="DE", checked_at=date)
        KeywordPosition.objects.create(
            keyword=keyword, position=5, checked_at=date + dt.timedelta(days=7)
        )
        assert keyword.positions.count() == 3
