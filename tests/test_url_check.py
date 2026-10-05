"""Проверка адреса на индексацию из «Размещений» (E9-12): только окошко, без записи."""

from typing import Any

import pytest
from django.core.exceptions import ValidationError
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.integrations.serp import SerpPage, SerpResult
from apps.observability.models import Check
from apps.placements import indexation, tasks
from apps.placements.indexation import page_url
from apps.placements.models import Placement, PlacementStatus
from apps.sites.models import Product, Site

pytestmark = pytest.mark.django_db

URL = "https://example.com/blog/post/"
CHECK = reverse("admin:placements_placement_check_url")
STATUS = reverse("admin:placements_placement_check_url_status")


class FakeSearch:
    def __init__(self, found: list[str]) -> None:
        self.found = found
        self.queries: list[str] = []

    def __call__(self, query: str, **kwargs: Any) -> SerpPage:
        self.queries.append(query)
        results = tuple(
            SerpResult(i, u, "example.com", "", "") for i, u in enumerate(self.found, 1)
        )
        return SerpPage(query, 10, "us", results, None, timezone.now())


def _serp(monkeypatch: pytest.MonkeyPatch, found: list[str]) -> FakeSearch:
    fake = FakeSearch(found)
    monkeypatch.setattr(indexation, "search", fake)
    monkeypatch.setattr(tasks.check_url_indexation, "throttle", None)
    return fake


def _check(client: Client, url: str) -> dict[str, Any]:
    started = client.post(CHECK, {"url": url})
    assert started.status_code == 200
    data = started.json()
    state: dict[str, Any] = client.get(STATUS, {"key": data["key"], "task": data["task"]}).json()
    return state


class TestPageUrl:
    def test_adds_scheme_and_trims(self) -> None:
        assert page_url("  example.com/blog/post/ ") == "https://example.com/blog/post/"
        assert page_url("http://example.com/a?b=1") == "http://example.com/a?b=1"

    @pytest.mark.parametrize("raw", ["", "   ", "не адрес", "ftp://example.com/x"])
    def test_rejects(self, raw: str) -> None:
        with pytest.raises(ValidationError):
            page_url(raw)


class TestCheck:
    def test_indexed(self, admin_client: Client, monkeypatch: pytest.MonkeyPatch) -> None:
        serp = _serp(monkeypatch, [URL])
        state = _check(admin_client, URL)
        assert state == {"state": "done", "url": URL, "indexed": True, "position": 1}
        assert serp.queries == ["site:example.com/blog/post/"]

    def test_not_indexed_after_both_queries(
        self, admin_client: Client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        serp = _serp(monkeypatch, ["https://example.com/other/"])
        state = _check(admin_client, "example.com/blog/post/")
        assert state["state"] == "done" and state["indexed"] is False
        assert serp.queries == ["site:example.com/blog/post/", "https://example.com/blog/post/"]

    def test_writes_nothing(self, admin_client: Client, monkeypatch: pytest.MonkeyPatch) -> None:
        """Адрес статьи размещения — всё равно ни журнала, ни отметки в размещении."""
        _serp(monkeypatch, [URL])
        product = Product.objects.create(name="Convertio", domain="convertio.co")
        placement = Placement.objects.create(
            site=Site.objects.create(domain="example.com"),
            product=product,
            status=PlacementStatus.PUBLISHED,
            article_url=URL,
        )
        assert _check(admin_client, URL)["indexed"] is True
        placement.refresh_from_db()
        assert placement.is_indexed is None and placement.indexed_checked_at is None
        assert not Check.objects.exists()

    def test_bad_address(self, admin_client: Client) -> None:
        response = admin_client.post(CHECK, {"url": "не адрес"})
        assert response.status_code == 200  # ответ человеку, не ошибка запроса
        assert "не адрес страницы" in response.json()["error"]
        assert "key" not in response.json()

    def test_unknown_key_is_queued(self, admin_client: Client) -> None:
        assert admin_client.get(STATUS, {"key": "nope", "task": "nope"}).json() == {
            "state": "queued"
        }

    def test_field_on_placements_list(self, admin_client: Client) -> None:
        # Строка действий есть, когда в списке есть размещения.
        Placement.objects.create(
            site=Site.objects.create(domain="example.com"),
            product=Product.objects.create(name="Convertio", domain="convertio.co"),
        )
        page = admin_client.get(reverse("admin:placements_placement_changelist")).content.decode()
        assert 'id="seo-url-check"' in page
        assert 'form="seo-url-check"' in page
        assert CHECK in page and STATUS in page
