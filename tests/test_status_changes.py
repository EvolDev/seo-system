"""История смены статусов площадки и размещения (E1-13, ADR-049).

Строку пишет триггер в базе при каждой смене статуса; кто и откуда —
отметка кода (`config.changes`). Здесь — что каждый путь смены оставляет
строку с нужной отметкой, а правка без смены статуса строки не оставляет.
"""

import datetime as dt
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from django.contrib.auth.models import User
from django.db import connection, transaction
from django.test import Client
from django.urls import reverse

from apps.placements.models import Placement, PlacementStatus, PlacementStatusChange
from apps.sites import status_history, statuses
from apps.sites.importing.run import ImportOptions, run_import
from apps.sites.models import (
    Product,
    ProductSite,
    ReviewGroup,
    Seller,
    Site,
    SiteStatus,
    SiteStatusChange,
    StatusSource,
    Upload,
    UploadItem,
    UploadKind,
    ensure_product_sites,
)
from apps.sites.uploads import review
from config.changes import MIGRATION_STAMP_SQL, bind_change, stamped
from config.run_id import bind_run_id, new_run_id

pytestmark = pytest.mark.django_db

PARTIAL = {"X-Seo-Partial": "1"}


@pytest.fixture
def convertio() -> Product:
    return Product.objects.create(name="Convertio", domain="convertio.co")


@pytest.fixture
def site(convertio: Product) -> Site:
    return Site.objects.create(domain="example.com")


@pytest.fixture
def row(site: Site, convertio: Product) -> ProductSite:
    return ProductSite.objects.get(site=site, product=convertio)


@pytest.fixture
def user() -> User:
    return User.objects.create_user("alice", first_name="Алиса")


def _site_changes(row: ProductSite) -> list[tuple[Any, ...]]:
    return list(
        SiteStatusChange.objects.filter(product_site=row)
        .order_by("pk")
        .values_list("from_status", "to_status", "source", "actor_id", "placement_id")
    )


def _placement_changes(placement: Placement) -> list[tuple[Any, ...]]:
    return list(
        PlacementStatusChange.objects.filter(placement=placement)
        .order_by("pk")
        .values_list("from_status", "to_status", "source", "actor_id")
    )


class TestSiteStatus:
    def test_rows_made_by_system_are_not_changes(self, row: ProductSite) -> None:
        # Каждая пара продукт × площадка создаётся «Новой» — это не смена.
        ensure_product_sites()
        assert not SiteStatusChange.objects.exists()

    def test_change_is_written_once(self, row: ProductSite, user: User) -> None:
        with bind_change(StatusSource.FORM, user.pk):
            row.status = SiteStatus.VIEWED
            row.save()
            # Тот же статус ещё раз и правка причины — не смена.
            row.save()
            row.reject_reason = "дорого"
            row.save()
        assert _site_changes(row) == [
            (SiteStatus.NEW, SiteStatus.VIEWED, StatusSource.FORM, user.pk, None)
        ]

    def test_change_has_time_and_run_id(self, row: ProductSite) -> None:
        run_id = new_run_id()
        with bind_run_id(run_id):
            row.status = SiteStatus.APPROVED
            row.save()
        change = SiteStatusChange.objects.get()
        assert change.run_id == run_id
        assert change.changed_at is not None

    def test_created_not_new_is_a_change(self, convertio: Product) -> None:
        site = Site.objects.create(domain="other.com")
        ProductSite.objects.filter(site=site).delete()
        created = ProductSite.objects.create(site=site, product=convertio, status="viewed")
        assert _site_changes(created) == [(None, SiteStatus.VIEWED, None, None, None)]

    def test_update_around_save_is_written_unmarked(self, row: ProductSite) -> None:
        """Смена в обход save() — тоже строка: её ловит триггер, откуда — не отмечено."""
        ProductSite.objects.filter(pk=row.pk).update(status=SiteStatus.DISCARDED)
        assert _site_changes(row) == [(SiteStatus.NEW, SiteStatus.DISCARDED, None, None, None)]

    def test_migration_marks_itself(self, row: ProductSite) -> None:
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(MIGRATION_STAMP_SQL)
            cursor.execute("UPDATE product_sites SET status = 'viewed' WHERE id = %s", [row.pk])
        assert _site_changes(row) == [
            (SiteStatus.NEW, SiteStatus.VIEWED, StatusSource.MIGRATION, None, None)
        ]

    def test_product_with_changes_has_history(self, row: ProductSite, convertio: Product) -> None:
        row.status = SiteStatus.VIEWED
        row.save()
        row.status = SiteStatus.NEW
        row.save()
        # Строка снова «Новая», но статус меняли — продукт уже не удалить.
        assert convertio.has_history()


class TestPlacement:
    def test_created_and_changed(self, site: Site, convertio: Product, user: User) -> None:
        with bind_change(StatusSource.PANEL, user.pk):
            placement = Placement.objects.create(site=site, product=convertio)
            placement.status = PlacementStatus.ORDERED
            placement.save()
        assert _placement_changes(placement) == [
            (None, PlacementStatus.PLANNED, StatusSource.PANEL, user.pk),
            (PlacementStatus.PLANNED, PlacementStatus.ORDERED, StatusSource.PANEL, user.pk),
        ]

    def test_edit_without_status_change_is_not_written(
        self, site: Site, convertio: Product
    ) -> None:
        placement = Placement.objects.create(site=site, product=convertio)
        placement.comment = "картинки битые"
        placement.save()
        placement.is_indexed = True
        placement.save(update_fields=["is_indexed"])
        placement.save()
        assert len(_placement_changes(placement)) == 1

    def test_site_status_follows_placement(
        self, row: ProductSite, site: Site, convertio: Product, user: User
    ) -> None:
        """Система по размещению (ADR-047): «по размещению», номер, кто сменил размещение."""
        with bind_change(StatusSource.PANEL, user.pk):
            placement = Placement.objects.create(site=site, product=convertio)
            placement.status = PlacementStatus.PUBLISHED
            placement.save()
        assert _site_changes(row) == [
            (SiteStatus.NEW, SiteStatus.PLACED, StatusSource.PLACEMENT, user.pk, placement.pk)
        ]

    def test_advance_mark_does_not_stick(self, row: ProductSite, site: Site) -> None:
        """Отметка «по размещению» — только на свою запись, дальше в той же транзакции её нет."""
        with transaction.atomic():
            statuses.advance(site.pk, row.product_id, SiteStatus.ORDERED, placement_id=None)
            ProductSite.objects.filter(pk=row.pk).update(status=SiteStatus.DISCARDED)
        sources = [change[2] for change in _site_changes(row)]
        assert sources == [StatusSource.PLACEMENT, None]

    def test_nested_mark_returns_outer(self, row: ProductSite) -> None:
        with transaction.atomic(), bind_change(StatusSource.FORM), stamped():
            with stamped(source=StatusSource.PLACEMENT):
                pass
            ProductSite.objects.filter(pk=row.pk).update(status=SiteStatus.VIEWED)
        assert _site_changes(row)[0][2] == StatusSource.FORM


class TestAdmin:
    @pytest.fixture
    def admin(self, admin_client: Client) -> User:
        return User.objects.get(username="admin")

    def test_decision_panel(self, admin_client: Client, admin: User, row: ProductSite) -> None:
        url = reverse("admin:sites_productsite_decision", args=[row.pk])
        admin_client.post(url, {"status": SiteStatus.DISCARDED, "reject_reason": "nofollow"},
                          headers=PARTIAL)  # fmt: skip
        assert _site_changes(row) == [
            (SiteStatus.NEW, SiteStatus.DISCARDED, StatusSource.PANEL, admin.pk, None)
        ]

    def test_decision_full_form(self, admin_client: Client, admin: User, row: ProductSite) -> None:
        url = reverse("admin:sites_productsite_change", args=[row.pk])
        response = admin_client.post(url, {"status": SiteStatus.VIEWED, "reject_reason": ""})
        assert response.status_code == 302
        assert _site_changes(row) == [
            (SiteStatus.NEW, SiteStatus.VIEWED, StatusSource.FORM, admin.pk, None)
        ]

    def test_site_form_status_rows(
        self, admin_client: Client, admin: User, row: ProductSite, site: Site
    ) -> None:
        url = reverse("admin:sites_site_change", args=[site.pk])
        data = {
            "domain": site.domain,
            "marks_as_ad": "unknown",
            "product_sites-TOTAL_FORMS": "1",
            "product_sites-INITIAL_FORMS": "1",
            "product_sites-0-id": str(row.pk),
            "product_sites-0-site": str(site.pk),
            "product_sites-0-status": SiteStatus.APPROVED,
            "product_sites-0-reject_reason": "",
        }
        response = admin_client.post(url, data)
        assert response.status_code == 302, response.context["errors"]
        assert _site_changes(row) == [
            (SiteStatus.NEW, SiteStatus.APPROVED, StatusSource.FORM, admin.pk, None)
        ]

    def test_placement_panel(
        self, admin_client: Client, admin: User, row: ProductSite, site: Site, convertio: Product
    ) -> None:
        placement = Placement.objects.create(site=site, product=convertio)
        url = reverse("admin:placements_placement_change", args=[placement.pk])
        data = {
            "status": "ordered",
            "ordered_at": "2026-10-02",
            "currency": "EUR",
            "announce_on_homepage": "unknown",
            "links-TOTAL_FORMS": "0",
            "links-INITIAL_FORMS": "0",
        }
        assert admin_client.post(url, data, headers=PARTIAL).json()["saved"]
        assert _placement_changes(placement)[-1] == (
            PlacementStatus.PLANNED,
            PlacementStatus.ORDERED,
            StatusSource.PANEL,
            admin.pk,
        )
        assert _site_changes(row) == [
            (SiteStatus.NEW, SiteStatus.ORDERED, StatusSource.PLACEMENT, admin.pk, placement.pk)
        ]


def test_import_marks_changes(
    make_workbook: Callable[..., Path], tmp_path: Path, convertio: Product
) -> None:
    Product.objects.create(name="Clideo", domain="clideo.com")
    book = make_workbook(
        base=[
            ("a.com", {"Статус": "Заявка отправлена", "Анкор1": "convert",
                       "Ссылка1": "https://convertio.co/"}),
            ("b.com", {"Комментарий к площадке": "Дорого, отбрасываем"}),
        ],
        keywords=[("convert", {})],
    )  # fmt: skip
    options = ImportOptions(book, as_of=dt.date(2026, 9, 27), list_name="Сентябрь", dry_run=False)
    run_id = new_run_id()
    with bind_run_id(run_id):
        run_import(options, tmp_path / "reports")
    placement = Placement.objects.get(site__domain="a.com")
    assert _placement_changes(placement) == [
        (None, PlacementStatus.ORDERED, StatusSource.IMPORT, None)
    ]
    changes = SiteStatusChange.objects.filter(product_site__product=convertio)
    found = {(c.product_site.site.domain, c.to_status, c.source, c.placement_id) for c in changes}
    assert found == {
        ("a.com", SiteStatus.ORDERED, StatusSource.PLACEMENT, placement.pk),
        ("b.com", SiteStatus.DISCARDED, StatusSource.IMPORT, None),
    }
    assert {c.run_id for c in changes} == {run_id}


def test_upload_block_and_undo(row: ProductSite, site: Site, user: User, offer: Any) -> None:
    """«Заблокировать» в разборе загрузки и отмена — «загрузка», от того, кто нажал."""
    price = offer(site, 10000)
    upload = Upload.objects.create(
        kind=UploadKind.PRICE_LIST,
        seller=Seller.collaborator(),
        prices_date=dt.date(2026, 10, 1),
        file_name="p.csv",
        file_path="p.csv",
        file_sha256="0" * 64,
    )
    item = UploadItem.objects.create(
        upload=upload, site=site, price=price, review_group=ReviewGroup.NEW
    )
    undo, problems = review.decide(upload, [item.pk], "block", author=user)
    assert not problems
    review.undo(undo, author=user)
    assert _site_changes(row) == [
        (SiteStatus.NEW, SiteStatus.BLACKLISTED, StatusSource.UPLOAD, user.pk, None),
        (SiteStatus.BLACKLISTED, SiteStatus.NEW, StatusSource.UPLOAD, user.pk, None),
    ]


class TestShown:
    """Хронология видна в карточке площадки и в панели размещения."""

    def test_card_shows_history_by_product(
        self, admin_client: Client, row: ProductSite, site: Site, convertio: Product
    ) -> None:
        admin = User.objects.get(username="admin")
        with bind_change(StatusSource.PANEL, admin.pk):
            row.status = SiteStatus.VIEWED
            row.save()
            placement = Placement.objects.create(
                site=site, product=convertio, status=PlacementStatus.ORDERED
            )
        card = reverse("admin:sites_site_card", args=[site.pk])
        page = admin_client.get(card, headers=PARTIAL).content.decode()
        assert "<summary>история (2)</summary>" in page
        # Новые сверху: сначала «по размещению» — со ссылкой на размещение в панели.
        placement_url = reverse("admin:placements_placement_change", args=[placement.pk])
        newest = page.index("Просмотрено → Заявка отправлена")
        oldest = page.index("Новая → Просмотрено")
        assert newest < oldest
        assert f'<a href="{placement_url}" data-panel>по размещению</a> · admin' in page
        assert "admin, панель" in page
        # Полная страница карточки — та же история.
        assert "история (2)" in admin_client.get(card).content.decode()

    def test_card_without_changes_has_no_history(
        self, admin_client: Client, row: ProductSite, site: Site
    ) -> None:
        page = admin_client.get(reverse("admin:sites_site_card", args=[site.pk])).content.decode()
        assert "история (" not in page

    def test_placement_panel_shows_history(
        self, admin_client: Client, site: Site, convertio: Product
    ) -> None:
        with bind_change(StatusSource.FORM):
            placement = Placement.objects.create(site=site, product=convertio)
        Placement.objects.filter(pk=placement.pk).update(status=PlacementStatus.WRITING)
        url = reverse("admin:placements_placement_change", args=[placement.pk])
        page = admin_client.get(url, headers=PARTIAL).content.decode()
        history = page[page.index('class="seo-status-list"') :]
        assert history.index('Запланировано → Пишется · <span class="seo-sub">не отмечено') < (
            history.index('Создано: Запланировано · <span class="seo-sub">форма')
        )
        # Сразу под кнопками статуса — раньше группы «Заявка».
        assert page.index('name="status"') < page.index("seo-status-list") < page.index(">Заявка<")

    def test_new_placement_form_has_no_history(self, admin_client: Client) -> None:
        page = admin_client.get(reverse("admin:placements_placement_add")).content.decode()
        assert "история статуса" not in page


@pytest.mark.parametrize(
    ("source", "actor_name", "shown"),
    [
        (StatusSource.IMPORT, None, "импорт таблицы"),
        (StatusSource.MIGRATION, None, "миграция"),
        (StatusSource.UPLOAD, "Алиса", "Алиса, загрузка"),
        (StatusSource.FORM, "Алиса", "Алиса, форма"),
        (None, "Алиса", "Алиса"),
        (None, None, "не отмечено"),
        (StatusSource.PLACEMENT, None, "по размещению"),
    ],
)
def test_who(source: StatusSource | None, actor_name: str | None, shown: str) -> None:
    actor = User(username="alice", first_name=actor_name) if actor_name else None
    assert str(status_history._who(source, actor)) == shown
