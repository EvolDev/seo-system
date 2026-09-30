"""Экран «Расход API»: только просмотр, суммы в долларах, лимит выдачи за сегодня (E2-02)."""

from decimal import Decimal
from uuid import uuid4

import pytest
from django.test import Client
from django.urls import reverse
from pytest_django import Settings

from apps.observability.admin import money_text
from apps.observability.models import ApiUsage

pytestmark = pytest.mark.django_db

URL = reverse("admin:observability_apiusage_changelist")


@pytest.mark.parametrize(
    ("cents", "currency", "text"),
    [
        (Decimal("0.1"), "USD", "$0,001"),
        (Decimal("0.075"), "USD", "$0,00075"),
        (Decimal("500"), "USD", "$5,00"),
        (Decimal("1234.5"), "USD", "$12,345"),
        (0, "USD", "$0,00"),
        (Decimal("250"), "EUR", "2,50 EUR"),
        (None, "USD", None),
    ],
)
def test_money_text(cents: Decimal | int | None, currency: str, text: str | None) -> None:
    assert money_text(cents, currency) == text


def test_list_in_russian_with_today_spend(admin_client: Client, settings: Settings) -> None:
    settings.SERP_DAILY_BUDGET_CENTS = 500
    run_id = uuid4()
    ApiUsage.objects.create(
        provider="serper", endpoint="search", units=1, cost_cents=Decimal("0.1"), run_id=run_id
    )
    ApiUsage.objects.create(
        provider="serper", endpoint="search", units=2, cost_cents=Decimal("0.2")
    )
    ApiUsage.objects.create(provider="ahrefs", endpoint="metrics", units=50, cost_cents=Decimal(7))

    response = admin_client.get(URL)
    assert response.status_code == 200
    content = response.content.decode()
    expected = ("Расход API", "Serper · выдача Google", "Ahrefs", "$0,001", "$0,07")
    for text in (*expected, str(run_id)[:8]):
        assert text in content
    # Над списком — выдача за сегодня против лимита; Ahrefs в неё не входит.
    assert "Выдача Google сегодня: $0,003 из $5,00 в сутки" in content


def test_read_only(admin_client: Client) -> None:
    usage = ApiUsage.objects.create(provider="serper", cost_cents=Decimal("0.1"))
    assert admin_client.get(reverse("admin:observability_apiusage_add")).status_code == 403
    change = admin_client.post(
        reverse("admin:observability_apiusage_change", args=[usage.pk]), {"provider": "x"}
    )
    assert change.status_code == 403
    delete = admin_client.post(reverse("admin:observability_apiusage_delete", args=[usage.pk]))
    assert delete.status_code == 403
    usage.refresh_from_db()
    assert usage.provider == "serper"


def test_search_by_run_id(admin_client: Client) -> None:
    run_id = uuid4()
    wanted = ApiUsage.objects.create(provider="serper", run_id=run_id)
    ApiUsage.objects.create(provider="serper", run_id=uuid4())
    response = admin_client.get(URL, {"q": str(run_id)})
    assert list(response.context["cl"].result_list) == [wanted]
