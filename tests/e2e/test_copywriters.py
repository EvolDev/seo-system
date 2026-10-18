"""Копирайтер в размещении в браузере (ADR-072): выбор подставляет ставку в пустую цену,
вписанную руками цену не трогает."""

import pytest
from playwright.sync_api import Page, expect
from pytest_django.live_server_helper import LiveServer

from apps.content.models import Copywriter
from apps.placements.models import Placement, PlacementStatus
from apps.sites.models import Product, Site

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=True, serialized_rollback=True)]


@pytest.fixture
def placement() -> Placement:
    Copywriter.objects.create(name="Anna", rate_cents=4000)
    Copywriter.objects.create(name="Igor", rate_cents=3550, currency="USD")
    return Placement.objects.create(
        site=Site.objects.create(domain="writing.com"),
        product=Product.objects.create(name="Convertio", domain="convertio.co"),
        status=PlacementStatus.WRITING,
    )


def test_rate_follows_choice(
    admin_page: Page, live_server: LiveServer, placement: Placement
) -> None:
    page = admin_page
    page.goto(f"{live_server.url}/admin/placements/placement/{placement.pk}/change/")
    select = page.locator("select[name=copywriter]")
    price = page.locator("input[name=copywriter_cents]")
    currency = page.locator("select[name=copywriter_currency]")
    select.select_option(label="Anna")
    expect(price).to_have_value("40")
    expect(currency).to_have_value("EUR")
    # Подставленная ставка сменяется ставкой другого копирайтера.
    select.select_option(label="Igor")
    expect(price).to_have_value("35,50")
    expect(currency).to_have_value("USD")
    # Вписанную руками цену выбор не трогает.
    price.fill("70")
    select.select_option(label="Anna")
    expect(price).to_have_value("70")
