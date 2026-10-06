"""Проверка индексации в админке: кнопка, действия, история (E2-03)."""

from datetime import timedelta
from typing import Any

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.integrations.serp import SerpPage, SerpResult
from apps.observability.models import Check, CheckStatus, TaskRun
from apps.placements import indexation, tasks
from apps.placements.models import Placement, PlacementStatus
from apps.sites.models import Product, Site

pytestmark = pytest.mark.django_db

URL = "https://example.com/blog/post/"
CHANGELIST = reverse("admin:placements_placement_changelist")


class FakeSearch:
    def __init__(self) -> None:
        self.urls: list[str] = [URL]
        self.calls = 0

    def __call__(self, query: str, **kwargs: Any) -> SerpPage:
        self.calls += 1
        results = tuple(SerpResult(i, u, "example.com", "", "") for i, u in enumerate(self.urls, 1))
        return SerpPage(query, 10, "us", results, None, timezone.now())


@pytest.fixture
def serp(monkeypatch: pytest.MonkeyPatch) -> FakeSearch:
    fake = FakeSearch()
    monkeypatch.setattr(indexation, "search", fake)
    monkeypatch.setattr(tasks.check_indexation, "throttle", None)
    return fake


@pytest.fixture
def placement() -> Placement:
    product = Product.objects.create(name="Convertio", domain="convertio.co")
    site = Site.objects.create(domain="example.com")
    return Placement.objects.create(
        site=site, product=product, status=PlacementStatus.PLACED, article_url=URL
    )


def _change(placement: Placement) -> str:
    return reverse("admin:placements_placement_change", args=[placement.pk])


def _button(placement: Placement) -> str:
    return reverse("admin:placements_placement_check_indexation", args=[placement.pk])


def _messages(response: Any) -> list[str]:
    return [str(message) for message in response.context["messages"]]


class TestButton:
    def test_shown_with_article_url(self, admin_client: Client, placement: Placement) -> None:
        page = admin_client.get(_change(placement)).content.decode()
        assert _button(placement) in page
        assert "Проверить индексацию" in page
        assert "Ещё не проверялась." in page

    def test_hidden_without_article_url(self, admin_client: Client, placement: Placement) -> None:
        placement.article_url = None
        placement.save()
        page = admin_client.get(_change(placement)).content.decode()
        assert _button(placement) not in page

    def test_click_checks_and_shows_result(
        self, admin_client: Client, placement: Placement, serp: FakeSearch
    ) -> None:
        response = admin_client.post(_button(placement), follow=True)
        assert "поставлена в очередь" in " ".join(_messages(response))
        (check,) = Check.objects.all()
        assert check.status == CheckStatus.OK
        assert check.result is not None
        assert check.result["manual"] is True
        # Нажатие — начало цепочки: у проверки свой run_id.
        assert check.run_id is not None
        placement.refresh_from_db()
        assert placement.is_indexed is True
        page = response.content.decode()
        assert "в индексе, место 1" in page
        assert "кнопка" in page

    def test_click_works_with_schedule_off_and_skip_checks(
        self, admin_client: Client, placement: Placement, serp: FakeSearch
    ) -> None:
        placement.skip_checks = True
        placement.save()
        admin_client.post(_button(placement))
        assert serp.calls == 1

    def test_get_does_not_check(
        self, admin_client: Client, placement: Placement, serp: FakeSearch
    ) -> None:
        assert admin_client.get(_button(placement)).status_code == 405
        assert serp.calls == 0

    def test_without_article_url_nothing_to_check(
        self, admin_client: Client, placement: Placement, serp: FakeSearch
    ) -> None:
        placement.article_url = ""
        placement.save()
        response = admin_client.post(_button(placement), follow=True)
        assert "Нет адреса статьи" in " ".join(_messages(response))
        assert serp.calls == 0

    def test_unknown_placement(self, admin_client: Client) -> None:
        url = reverse("admin:placements_placement_check_indexation", args=[10**9])
        assert admin_client.post(url).status_code == 404


class TestHistory:
    def test_rows(self, admin_client: Client, placement: Placement, serp: FakeSearch) -> None:
        indexation.check_placement(placement, manual=False)
        serp.urls = []
        Check.objects.create(
            entity_type="placement",
            entity_id=placement.pk,
            check_type="indexation",
            status=CheckStatus.FAILED,
            result={"url": "https://example.com/old/", "alert": True, "failing_days": 31},
            checked_at=timezone.now() - timedelta(days=40),
        )
        Check.objects.create(
            entity_type="placement",
            entity_id=placement.pk,
            check_type="indexation",
            status=CheckStatus.OK,
            performed_by="human",
            checked_at=timezone.now() - timedelta(days=50),
        )
        page = admin_client.get(_change(placement)).content.decode()
        assert "расписание" in page
        old = "https://example.com/old/"
        assert f"не в индексе 31 дн. — оповещение — по прежнему адресу {old}" in page
        assert "человек" in page

    def test_found_by_url_is_marked(self, admin_client: Client, placement: Placement) -> None:
        Check.objects.create(
            entity_type="placement",
            entity_id=placement.pk,
            check_type="indexation",
            status=CheckStatus.OK,
            result={"url": URL, "position": 3, "found_by": "url"},
        )
        page = admin_client.get(_change(placement)).content.decode()
        assert "в индексе, место 3 — найдена поиском по адресу, site: её не показал" in page

    def test_seen_urls_explain_failure(self, admin_client: Client, placement: Placement) -> None:
        Check.objects.create(
            entity_type="placement",
            entity_id=placement.pk,
            check_type="indexation",
            status=CheckStatus.FAILED,
            result={"url": URL, "seen": ["https://example.com/blog/", "https://example.com/"]},
        )
        page = admin_client.get(_change(placement)).content.decode()
        assert "не в индексе; в выдаче: https://example.com/blog/ и ещё 1" in page

    def test_other_checks_are_not_shown(self, admin_client: Client, placement: Placement) -> None:
        Check.objects.create(
            entity_type="placement",
            entity_id=placement.pk,
            check_type="link_alive",
            status=CheckStatus.OK,
        )
        Check.objects.create(
            entity_type="site",
            entity_id=placement.pk,
            check_type="indexation",
            status=CheckStatus.OK,
        )
        page = admin_client.get(_change(placement)).content.decode()
        assert "Ещё не проверялась." in page


class TestListActions:
    def test_check_selected(
        self, admin_client: Client, placement: Placement, serp: FakeSearch
    ) -> None:
        no_url = Placement.objects.create(site=placement.site, product=placement.product)
        data = {"action": "check_indexation_action", "_selected_action": [placement.pk, no_url.pk]}
        response = admin_client.post(CHANGELIST, data, follow=True)
        text = " ".join(_messages(response))
        assert "поставлена в очередь: 1" in text
        assert "Без адреса статьи, не проверяются: 1" in text
        assert serp.calls == 1

    def test_skip_and_resume(self, admin_client: Client, placement: Placement) -> None:
        data = {"action": "skip_checks_action", "_selected_action": [placement.pk]}
        response = admin_client.post(CHANGELIST, data, follow=True)
        assert "Убрано из проверок по расписанию: 1" in " ".join(_messages(response))
        placement.refresh_from_db()
        assert placement.skip_checks is True
        data["action"] = "resume_checks_action"
        admin_client.post(CHANGELIST, data)
        placement.refresh_from_db()
        assert placement.skip_checks is False

    def test_skip_checks_in_form(self, admin_client: Client, placement: Placement) -> None:
        form = admin_client.get(_change(placement)).context["adminform"].form
        assert "skip_checks" in form.fields

    def test_list_columns_and_filter(self, admin_client: Client, placement: Placement) -> None:
        response = admin_client.get(CHANGELIST, {"skip_checks__exact": "0"})
        assert response.context["cl"].result_count == 1
        page = response.content.decode()
        assert "индексация проверена" in page.lower()
        assert "не проверять" in page.lower()


BATCH = reverse("admin:placements_placement_check_indexation_batch")
STATUS = reverse("admin:placements_placement_indexation_status")


def _state(client: Client, placement: Placement, task: str = "", **params: str) -> Any:
    response = client.get(STATUS, {"t": f"{placement.pk}:{task}", **params})
    assert response.status_code == 200
    return response.json()["items"][str(placement.pk)]


class TestInPlace:
    """Скрипт страницы: пачка проверок одним запросом и её состояние одним запросом."""

    def test_batch_queues_and_status_shows_result(
        self, admin_client: Client, placement: Placement, serp: FakeSearch
    ) -> None:
        no_url = Placement.objects.create(site=placement.site, product=placement.product)
        response = admin_client.post(BATCH, {"ids": [placement.pk, no_url.pk, 10**9]})
        assert response.status_code == 200
        data = response.json()
        assert list(data["tasks"]) == [str(placement.pk)]
        assert data["skipped"] == [no_url.pk, 10**9]
        state = _state(admin_client, placement, data["tasks"][str(placement.pk)])
        assert state["state"] == "success"
        assert state["indexed"] is True
        assert "icon-yes" in state["indexed_html"]
        assert state["checked_at"] != "—"
        assert state["errors"] == []
        assert "history_html" not in state

    def test_one_batch_one_run_id(
        self, admin_client: Client, placement: Placement, serp: FakeSearch
    ) -> None:
        other = Placement.objects.create(
            site=placement.site,
            product=placement.product,
            status=PlacementStatus.PLACED,
            article_url="https://example.com/other/",
        )
        admin_client.post(BATCH, {"ids": [placement.pk, other.pk]})
        run_ids = set(Check.objects.values_list("run_id", flat=True))
        assert len(run_ids) == 1
        assert None not in run_ids

    def test_history_for_card(
        self, admin_client: Client, placement: Placement, serp: FakeSearch
    ) -> None:
        task = admin_client.post(BATCH, {"ids": [placement.pk]}).json()["tasks"][str(placement.pk)]
        state = _state(admin_client, placement, task, history="1")
        assert "кнопка" in state["history_html"]

    def test_bad_ids(self, admin_client: Client) -> None:
        response = admin_client.post(BATCH, {"ids": ["abc"]})
        assert response.status_code == 400
        assert "Неверный список" in response.json()["error"]

    def test_task_not_started_yet(self, admin_client: Client, placement: Placement) -> None:
        assert _state(admin_client, placement, "no-such-task")["state"] == "queued"
        state = _state(admin_client, placement)
        assert state["state"] == "unknown"
        assert "icon-unknown" in state["indexed_html"]

    def test_unknown_placement_is_left_out(self, admin_client: Client) -> None:
        response = admin_client.get(STATUS, {"t": [f"{10**9}:x", "junk"]})
        assert response.json() == {"items": {}}

    def _run(self, **fields: Any) -> TaskRun:
        defaults: dict[str, Any] = {"task_name": "check_indexation", "status": "running"}
        return TaskRun.objects.create(**{**defaults, **fields})

    def test_failed_attempt_then_retry(self, admin_client: Client, placement: Placement) -> None:
        error = {"attempt": 1, "error": "SerpError: Serper ответил 403: Unauthorized."}
        self._run(payload={"task_id": "t1", "attempt": 2, "errors": [error]})
        state = _state(admin_client, placement, "t1")
        assert (state["state"], state["attempt"], state["attempts"]) == ("running", 2, 3)
        assert state["errors"] == [error]

    def test_failed(self, admin_client: Client, placement: Placement) -> None:
        self._run(status="failed", error="SerpError: сбой", payload={"task_id": "t2"})
        state = _state(admin_client, placement, "t2")
        assert (state["state"], state["error"]) == ("failed", "SerpError: сбой")

    def test_paused(self, admin_client: Client, placement: Placement) -> None:
        until = timezone.localtime().replace(hour=23, minute=59, second=0, microsecond=0)
        waiting = {"until": until.isoformat(), "reason": "дневной лимит на выдачу исчерпан"}
        self._run(payload={"task_id": "t3", "waiting": waiting})
        state = _state(admin_client, placement, "t3")
        assert state["state"] == "waiting"
        assert state["waiting"]["until"].endswith("23:59")
        assert state["waiting"]["reason"] == "дневной лимит на выдачу исчерпан"

    def test_get_and_post_only(self, admin_client: Client) -> None:
        assert admin_client.post(STATUS).status_code == 405
        assert admin_client.get(BATCH).status_code == 405

    def test_status_of_many_in_few_queries(
        self,
        admin_client: Client,
        placement: Placement,
        django_assert_max_num_queries: Any,
    ) -> None:
        others = [
            Placement.objects.create(site=placement.site, product=placement.product)
            for _ in range(20)
        ]
        params = {"t": [f"{p.pk}:task{p.pk}" for p in [placement, *others]]}
        with django_assert_max_num_queries(6):
            response = admin_client.get(STATUS, params)
        assert len(response.json()["items"]) == 21

    def test_list_row_button_only_with_url(
        self, admin_client: Client, placement: Placement
    ) -> None:
        no_url = Placement.objects.create(site=placement.site, product=placement.product)
        page = admin_client.get(CHANGELIST).content.decode()
        assert f'data-placement="{placement.pk}" data-name="example.com"' in page
        assert f'data-check-url="{BATCH}"' in page
        assert f'data-status-url="{STATUS}"' in page
        assert f'data-placement="{no_url.pk}"' not in page
        assert f'data-indexed="{no_url.pk}"' in page
        assert "seo/indexation.js" in page
        assert "seo/indexation.css" in page

    def test_card_fields_are_filled_by_checks(
        self, admin_client: Client, placement: Placement
    ) -> None:
        response = admin_client.get(_change(placement))
        form = response.context["adminform"].form
        assert "is_indexed" not in form.fields
        assert "indexed_checked_at" not in form.fields
        page = response.content.decode()
        assert f'data-check-url="{BATCH}"' in page
        assert f'data-placement="{placement.pk}"' in page
        assert 'data-name="example.com"' in page
        assert "seo/indexation.js" in page
