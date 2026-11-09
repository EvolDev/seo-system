"""Сверка с Collaborator по базе: снимок, действия, отмена, экраны (E11-01, ADR-080).

API не вызывается: снимок пишет `deals.refresh` с подменённым клиентом.
"""

import json
import logging
from typing import Any

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse

from apps.content.domain_settings import set_product_setting
from apps.keywords.models import AnchorList, AnchorType, Keyword
from apps.placements.collaborator import deals, service
from apps.placements.models import (
    CollaboratorDeal,
    Invoice,
    InvoiceItem,
    Placement,
    PlacementLink,
    PlacementStatus,
)
from apps.sites.models import Product, Seller, Site, Upload, UploadKind
from apps.sites.uploads import undo
from apps.workspace import locks

pytestmark = pytest.mark.django_db

KEY = "test-collaborator-key-0123456789abcdef"
CARD = "https://collaborator.pro/creator/article/view?id={}"
ARTICLE = "https://www.contabeis.com.br/noticias/80646/como-transformar-pdf-em-word/"
SCREEN = reverse("admin:collaborator")
FILL = reverse("admin:collaborator_fill")
TAKE = reverse("admin:collaborator_take")
KEEP = reverse("admin:collaborator_keep")


class FakeClient:
    def __init__(self, items: list[dict[str, Any]]) -> None:
        self.items = items

    def deals(self, on_page: Any = None) -> list[dict[str, Any]]:
        if on_page is not None:
            on_page(1, 1)
        return self.items


def item(deal_id: int = 2834957, card: int = 549884, **fields: Any) -> dict[str, Any]:
    """Сделка той же формы, что у API, — по умолчанию contabeis.com.br на 08.11."""
    value: dict[str, Any] = {
        "id": deal_id,
        "project": {"id": 144728, "name": "Convertio"},
        "creator": {"id": card, "name": "contabeis.com.br", "url": CARD.format(card)},
        "status": "Pending approval",
        "anchors": [
            {
                "anchor": "transformar pdf em doc com o Convertio",
                "url": "https://convertio.co/pt/pdf-doc/",
            },
            {"anchor": "pdf em docx", "url": "https://convertio.co/pt/pdf-docx/"},
        ],
        "created_at": "2026-10-26 22:46:25",
        "dateOfPlacement": None,
        "urlOfPublishedArticle": ARTICLE,
        "price": "571.64 EUR",
        "pricePublication": "543.53 EUR",
        "priceSpelling": "28.11 EUR",
        "priceAnnouncement": None,
        "priceInsurance": None,
        "publicationType": "Article by the owner",
        "task": [],
    }
    value.update(fields)
    return value


def load(*items: dict[str, Any]) -> deals.Refreshed:
    return deals.refresh(FakeClient(list(items)))  # type: ignore[arg-type]


@pytest.fixture
def convertio() -> Product:
    return Product.objects.create(name="Convertio", domain="convertio.co")


@pytest.fixture
def collaborator() -> Seller:
    return Seller.collaborator()


@pytest.fixture
def site(convertio: Product) -> Site:
    return Site.objects.create(domain="contabeis.com.br", collaborator_url=CARD.format(549884))


@pytest.fixture
def placement(site: Site, convertio: Product, collaborator: Seller) -> Placement:
    placement = Placement.objects.create(
        site=site, product=convertio, seller=collaborator, status=PlacementStatus.ORDERED
    )
    for index, (anchor, url) in enumerate(
        [
            ("transformar pdf em doc com o Convertio", "https://convertio.co/pt/pdf-doc/"),
            ("pdf em docx", "https://convertio.co/pt/pdf-docx/"),
        ],
        start=1,
    ):
        PlacementLink.objects.create(
            placement=placement, anchor=anchor, target_url=url, link_index=index
        )
    return placement


def _json(response: Any) -> dict[str, Any]:
    assert response.status_code == 200, response.content[:500]
    data: dict[str, Any] = json.loads(response.content)
    return data


# --- Снимок ---


class TestSnapshot:
    def test_refresh_replaces_and_keeps_decisions(self) -> None:
        first = load(item(1), item(2))
        CollaboratorDeal.objects.filter(deal_id=1).update(kept={"order": {"pair": ["a", "b"]}})

        again = load(item(1, status="Posted"), item(3))

        assert (first.deals, first.added, first.removed) == (2, 2, 0)
        assert (again.deals, again.added, again.removed) == (2, 1, 1)
        rows = {row.deal_id: row for row in CollaboratorDeal.objects.all()}
        assert set(rows) == {1, 3}
        assert rows[1].status == "Posted"
        assert rows[1].kept == {"order": {"pair": ["a", "b"]}}
        assert rows[1].fetched_at == again.at

    def test_article_text_is_not_stored(self) -> None:
        load(item(1, task={"title": "T", "contentHtml": "<p>long</p>"}))

        assert CollaboratorDeal.objects.get().data["task"] == {"title": "T"}

    def test_product_by_project_setting_then_by_name(self, convertio: Product) -> None:
        clideo = Product.objects.create(name="Clideo", domain="clideo.com")
        set_product_setting(clideo.pk, "COLLABORATOR_PROJECTS", [555])
        load(
            item(1),
            item(2, project={"id": 555, "name": "Другое имя"}),
            item(3, project={"id": 777, "name": "Чужой"}),
        )
        found, _ = deals.snapshot()

        owners = service.owners(found)
        mine = service.report(convertio)

        assert owners == {1: convertio.pk, 2: clideo.pk, 3: None}
        assert [d.id for d in mine.deals] == [1]
        assert mine.foreign == {777: ("Чужой", 1)}


# --- Действия ---


class TestFill:
    def test_fills_only_empty(
        self, admin_client: Client, placement: Placement, convertio: Product
    ) -> None:
        placement.article_url = (
            "https://contabeis.com.br/noticias/80646/como-transformar-pdf-em-word"
        )
        placement.save()
        load(item())

        answer = _json(admin_client.post(FILL, {"deal": [2834957]}))

        placement.refresh_from_db()
        assert placement.collaborator_order_id == "2834957"
        # «Заплачено» у заявки не заполняется — его ставит закрытие в панели.
        assert placement.price_paid_cents is None
        # Адрес был — тот же с точностью до записи: не трогаем.
        assert placement.article_url.endswith("pdf-em-word")
        # Статус и дата — у человека.
        assert placement.status == PlacementStatus.ORDERED
        assert placement.published_at is None
        assert "Записано в размещения: 1 (номер заявки — 1)." in answer["message"]
        upload = Upload.objects.get(kind=UploadKind.COLLABORATOR)
        assert answer["undo_url"] == reverse("admin:sites_upload_delete", args=[upload.pk])
        assert upload.product == convertio and upload.journaled

    def test_paid_for_placed(self, admin_client: Client, placement: Placement) -> None:
        placement.status = PlacementStatus.PLACED
        placement.save()
        load(item(status="Posted"))

        admin_client.post(FILL, {"deal": [2834957]})

        placement.refresh_from_db()
        assert (placement.price_paid_cents, placement.currency) == (57164, "EUR")

    def test_repeat_changes_nothing(self, admin_client: Client, placement: Placement) -> None:
        load(item())
        admin_client.post(FILL, {"deal": [2834957]})
        before = Placement.objects.filter(pk=placement.pk).values().get()

        answer = _json(admin_client.post(FILL, {"deal": [2834957]}))

        assert Placement.objects.filter(pk=placement.pk).values().get() == before
        assert Upload.objects.filter(kind=UploadKind.COLLABORATOR).count() == 1
        assert "undo_url" not in answer

    def test_paid_from_invoice_untouched(
        self, admin_client: Client, placement: Placement, collaborator: Seller
    ) -> None:
        invoice = Invoice.objects.create(seller=collaborator, amount_cents=50000)
        InvoiceItem.objects.create(invoice=invoice, placement=placement, amount_cents=50000)
        placement.status = PlacementStatus.PLACED
        placement.save()
        load(item(status="Posted"))

        admin_client.post(FILL, {"deal": [2834957]})

        placement.refresh_from_db()
        assert placement.price_paid_cents is None
        assert placement.collaborator_order_id == "2834957"

    def test_busy_placement_skipped(
        self, admin_client: Client, placement: Placement, django_user_model: Any
    ) -> None:
        anna = django_user_model.objects.create_user("anna", first_name="Анна")
        locks.take(anna, locks.placements_key(placement.site_id, placement.product_id))
        load(item())

        answer = _json(admin_client.post(FILL, {"deal": [2834957]}))

        placement.refresh_from_db()
        assert placement.collaborator_order_id is None
        assert "contabeis.com.br (Анна)" in answer["message"]
        assert not Upload.objects.filter(kind=UploadKind.COLLABORATOR).exists()

    def test_undo_returns_as_it_was(self, admin_client: Client, placement: Placement) -> None:
        load(item())
        before = Placement.objects.filter(pk=placement.pk).values().get()
        admin_client.post(FILL, {"deal": [2834957]})
        upload = Upload.objects.get(kind=UploadKind.COLLABORATOR)

        undo.undo(upload)

        after = Placement.objects.filter(pk=placement.pk).values().get()
        assert after == before
        assert not Upload.objects.filter(pk=upload.pk).exists()

    def test_undo_through_admin(self, admin_client: Client, placement: Placement) -> None:
        load(item())
        admin_client.post(FILL, {"deal": [2834957]})
        upload = Upload.objects.get(kind=UploadKind.COLLABORATOR)
        url = reverse("admin:sites_upload_delete", args=[upload.pk])

        page = admin_client.get(url)
        admin_client.post(url, {"post": "yes"})

        assert page.status_code == 200
        assert "Вернётся как было — размещения: 1" in page.content.decode()
        placement.refresh_from_db()
        assert placement.collaborator_order_id is None

    def test_other_product_is_not_filled(
        self, admin_client: Client, placement: Placement, convertio: Product
    ) -> None:
        load(item(project={"id": 777, "name": "Clideo"}))

        _json(admin_client.post(FILL, {"deal": [2834957]}))

        placement.refresh_from_db()
        assert placement.collaborator_order_id is None


class TestTakeAndKeep:
    def test_take_order_number(self, admin_client: Client, placement: Placement) -> None:
        placement.collaborator_order_id = "945158"
        placement.save()
        load(item())

        answer = _json(admin_client.post(TAKE, {"deal": 2834957, "field": "order"}))

        placement.refresh_from_db()
        assert placement.collaborator_order_id == "2834957"
        assert answer["level"] == "success"

    def test_status_is_not_taken(self, admin_client: Client, placement: Placement) -> None:
        load(item(status="Posted"))

        answer = _json(admin_client.post(TAKE, {"deal": 2834957, "field": "status"}))

        placement.refresh_from_db()
        assert placement.status == PlacementStatus.ORDERED
        assert "в панели" in answer["message"]

    def test_take_anchors_rewrites_links(
        self, admin_client: Client, placement: Placement, convertio: Product
    ) -> None:
        brazil = AnchorList.objects.create(product=convertio, country="BR")
        brand = Keyword.objects.create(
            product=convertio,
            anchor_list=brazil,
            keyword="Convertio",
            target_url="https://convertio.co/pt/",
            anchor_type=AnchorType.BRANDED,
        )
        placement.anchor_list = brazil
        placement.save()
        extra = PlacementLink.objects.create(
            placement=placement,
            anchor="wav to mp3",
            target_url="https://convertio.co/x/",
            link_index=3,
        )
        PlacementLink.objects.filter(placement=placement, link_index=2).update(anchor="Converito")
        anchors = [
            {
                "anchor": "transformar pdf em doc com o Convertio",
                "url": "https://convertio.co/pt/pdf-doc/",
            },
            {"anchor": "Convertio", "url": "https://convertio.co/pt/pdf-docx/"},
            {"anchor": "Convertio", "url": "https://convertio.co/pt/"},
        ]
        load(item(anchors=anchors))
        before = list(PlacementLink.objects.filter(placement=placement).values())

        _json(admin_client.post(TAKE, {"deal": 2834957, "field": "anchors"}))

        links = list(placement.links.order_by("link_index"))
        assert [(link.anchor, link.target_url) for link in links] == [
            (a["anchor"], a["url"]) for a in anchors
        ]
        assert not PlacementLink.objects.filter(pk=extra.pk).exists()
        assert links[2].keyword_id == brand.pk and links[2].link_index == 4
        row = service.report(convertio).row_of(2834957)
        assert row is not None and row.differ_of("anchors") is None  # type: ignore[arg-type]

        undo.undo(Upload.objects.get(kind=UploadKind.COLLABORATOR))

        after = list(PlacementLink.objects.filter(placement=placement).values())
        assert sorted(after, key=lambda r: r["id"]) == sorted(before, key=lambda r: r["id"])

    def test_keep_until_values_change(
        self, admin_client: Client, placement: Placement, convertio: Product
    ) -> None:
        placement.collaborator_order_id = "945158"
        placement.save()
        load(item())

        _json(admin_client.post(KEEP, {"deal": 2834957, "field": "order"}))
        kept = service.report(convertio)
        load(item())  # снова обновили из Collaborator — пометка на месте
        still = service.report(convertio)
        placement.collaborator_order_id = "945159"
        placement.save()
        changed = service.report(convertio)

        assert kept.groups.differ == [] and len(kept.groups.kept) == 1
        assert still.groups.differ == []
        assert [d.field for d in changed.groups.differ[0].open_differs] == ["order"]

    def test_back_to_reconcile(
        self, admin_client: Client, placement: Placement, convertio: Product
    ) -> None:
        placement.collaborator_order_id = "945158"
        placement.save()
        load(item())
        admin_client.post(KEEP, {"deal": 2834957, "field": "order"})

        _json(admin_client.post(KEEP, {"deal": 2834957, "field": "order", "back": "1"}))

        assert CollaboratorDeal.objects.get().kept == {}
        assert len(service.report(convertio).groups.differ) == 1

    def test_view_only_user_cannot_change(self, placement: Placement, client: Client) -> None:
        viewer = User.objects.create_user("viewer", is_staff=True)
        client.force_login(viewer)
        load(item())

        response = client.post(FILL, {"deal": [2834957]})

        assert response.status_code in (302, 403)
        placement.refresh_from_db()
        assert placement.collaborator_order_id is None


# --- Экраны ---


class TestScreens:
    def test_screen_groups(self, admin_client: Client, placement: Placement) -> None:
        load(item(), item(1, card=99, creator={"id": 99, "name": "therooftopguide.com"}))

        page = admin_client.get(SCREEN)

        text = page.content.decode()
        assert page.status_code == 200
        assert (
            "2 сделки: 1 готова к закрытию, 1 можно заполнить, 0 расходится, 1 нет в системе"
            in text
        )
        assert "therooftopguide.com" in text
        assert "площадки нет в базе" in text

    def test_screen_without_snapshot(self, admin_client: Client, convertio: Product) -> None:
        page = admin_client.get(SCREEN)

        assert "Сделок ещё нет" in page.content.decode()

    def test_panel_block(self, admin_client: Client, placement: Placement) -> None:
        load(item())

        page = admin_client.get(
            reverse("admin:placements_placement_change", args=[placement.pk]),
            headers={"X-Seo-Partial": "1"},
        )

        text = page.content.decode()
        assert "Заявка <b>2834957</b>" in text
        assert "€571.64" in text and "€543.53" in text and "€28.11" in text
        assert text.count("is-ok") == 2
        assert "data-collab-fill-form-panel" in text
        assert "&quot;price_paid_cents&quot;: &quot;571,64&quot;" in text

    def test_filter_ready(self, admin_client: Client, placement: Placement, site: Site) -> None:
        other = Placement.objects.create(
            site=Site.objects.create(domain="other.com"),
            product=placement.product,
            status=PlacementStatus.ORDERED,
        )
        load(item())
        changelist = reverse("admin:placements_placement_changelist")

        page = admin_client.get(changelist, {"collaborator": "ready"})

        ids = {obj.pk for obj in page.context["cl"].result_list}
        assert ids == {placement.pk}
        assert other.pk not in ids

    def test_new_placement_from_deal(
        self, admin_client: Client, site: Site, convertio: Product, collaborator: Seller
    ) -> None:
        load(item(status="Posted"))
        add = reverse("admin:placements_placement_add")

        page = admin_client.get(
            add, {"site": site.pk, "product": convertio.pk, "collaborator_deal": 2834957}
        )

        form = page.context["adminform"].form
        assert form.initial["collaborator_order_id"] == "2834957"
        assert form.initial["status"] == PlacementStatus.ORDERED
        assert form.initial["price_paid_cents"] == 57164
        assert form.initial["article_url"] == ARTICLE
        assert form.initial["ordered_at"].date().isoformat() == "2026-10-26"


class TestRefreshButton:
    STATUS = reverse("admin:collaborator_refresh_status")
    REFRESH = reverse("admin:collaborator_refresh")

    def test_without_key(
        self, admin_client: Client, convertio: Product, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("COLLABORATOR_API_KEY", raising=False)

        answer = _json(admin_client.post(self.REFRESH))

        assert "COLLABORATOR_API_KEY" in answer["error"]

    def test_refresh_and_status(
        self,
        admin_client: Client,
        placement: Placement,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setenv("COLLABORATOR_API_KEY", KEY)
        monkeypatch.setattr(
            "apps.placements.tasks.CollaboratorClient.from_env",
            classmethod(lambda cls: FakeClient([item()])),
        )
        caplog.set_level(logging.DEBUG)

        started = _json(admin_client.post(self.REFRESH))
        state = _json(admin_client.get(self.STATUS, {"key": started["key"]}))

        assert state["state"] == "done"
        assert state["text"].startswith("1 сделка: 1 готова к закрытию")
        assert state["url"] == SCREEN
        assert CollaboratorDeal.objects.count() == 1
        assert KEY not in json.dumps(started) + json.dumps(state) + caplog.text
