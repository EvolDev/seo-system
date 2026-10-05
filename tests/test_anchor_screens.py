"""Экраны анкоров (E3-05, ADR-059): страница «Анкоры», окно «Новый анкор»,
правка долей на месте, блок «Анкоры продукта» в размещении."""

import datetime as dt
from decimal import Decimal

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse

from apps.keywords.models import AnchorType, CountryShare, Keyword, KeywordPosition, PageTypeShare
from apps.placements.models import Placement, PlacementLink, PlacementStatus
from apps.sites.models import Product, Site
from apps.workspace.products import choose_product

pytestmark = pytest.mark.django_db

D = Decimal
PAGE = reverse("admin:keywords_keywordcoverage_changelist")
ADD = reverse("admin:keywords_anchor_add")
SHARE = reverse("admin:keywords_anchor_share")
PLACEMENT_ADD = reverse("admin:placements_placement_add")


@pytest.fixture
def convertio() -> Product:
    product = Product.objects.create(name="Convertio", domain="convertio.co")
    PageTypeShare.objects.create(product=product, page_type="Video", target_pct=D(60), position=1)
    PageTypeShare.objects.create(
        product=product, page_type="Главная", target_pct=D(40), naked_pct=D(25), position=2
    )
    CountryShare.objects.create(product=product, country="us", target_pct=D(50))
    CountryShare.objects.create(product=product, country=None, target_pct=D(50))
    return product


@pytest.fixture
def anchors(convertio: Product) -> dict[str, Keyword]:
    made = {
        "convert": Keyword.objects.create(
            product=convertio, keyword="convert", target_url="https://convertio.co/",
            page_type="Главная", anchor_type=AnchorType.EXACT, volume=42000,
        ),
        "mp4": Keyword.objects.create(
            product=convertio, keyword="mp4 converter",
            target_url="https://convertio.co/mp4-converter/", page_type="Video",
            anchor_type=AnchorType.EXACT, volume=9000,
        ),
        "top": Keyword.objects.create(
            product=convertio, keyword="video converter",
            target_url="https://convertio.co/video-converter/", page_type="Video",
            anchor_type=AnchorType.EXACT,
        ),
        "brand": Keyword.objects.create(
            product=convertio, keyword="Convertio", target_url="https://convertio.co/",
            page_type="Главная", anchor_type=AnchorType.BRANDED, share=D("56.86"),
        ),
    }  # fmt: skip
    for name, position, day in (("top", 2, 22), ("mp4", 12, 22), ("mp4", 15, 16)):
        KeywordPosition.objects.create(
            keyword=made[name], position=position, checked_at=dt.date(2026, 9, day)
        )
    placement = Placement.objects.create(
        site=Site.objects.create(domain="a.com"),
        product=convertio,
        status=PlacementStatus.PUBLISHED,
    )
    PlacementLink.objects.create(
        placement=placement, keyword=made["convert"], anchor="convert",
        target_url="https://convertio.co/",
    )  # fmt: skip
    return made


# ---------- Страница «Анкоры» ----------


def test_page_shows_sheet_and_shares(
    admin_client: Client, convertio: Product, anchors: dict[str, Keyword]
) -> None:
    page = admin_client.get(PAGE, {"product": convertio.pk}).content.decode()
    assert "Анкоры Convertio" in page
    # Колонки позиций — по датам снимков, новые слева.
    assert page.index("Pos 22.09") < page.index("Pos 16.09")
    assert "mp4 converter" in page and "/mp4-converter/" in page
    # Доли: цель правится в таблице, факт посчитан.
    assert 'data-kind="type" data-id="Video" data-field="target"' in page
    assert "Сумма целей" in page and "100%" in page
    assert "безанкорка" in page.lower() and "56.86" in page
    assert "Остальные" in page
    assert "Загрузить файл анкоров" in page
    assert f"?kind=anchors&amp;product={convertio.pk}" in page


def test_page_filters(
    admin_client: Client, convertio: Product, anchors: dict[str, Keyword]
) -> None:
    def rows(**params: str) -> set[str]:
        response = admin_client.get(PAGE, {"product": str(convertio.pk), **params})
        return {row.keyword for row in response.context["cl"].result_list}

    assert rows(kind="naked") == {"Convertio"}
    assert rows(type="Video") == {"mp4 converter", "video converter"}
    assert rows(links="none") == {"mp4 converter", "video converter", "Convertio"}
    assert rows(links="placed") == {"convert"}


def test_page_uses_working_product(
    admin_client: Client, admin_user: User, convertio: Product, anchors: dict[str, Keyword]
) -> None:
    clideo = Product.objects.create(name="Clideo", domain="clideo.com")
    choose_product(admin_user, clideo)
    response = admin_client.get(PAGE)
    assert "Анкоры Clideo" in response.content.decode()
    assert list(response.context["cl"].result_list) == []


def test_in_reference_menu(admin_client: Client) -> None:
    groups = {g["name"]: g for g in admin_client.get(reverse("admin:index")).context["app_list"]}
    names = [m["object_name"] for m in groups["Справочники"]["models"]]
    assert "KeywordCoverage" in names
    assert "Keyword" not in [m["object_name"] for m in groups["Работа"]["models"]]


# ---------- Окно «Новый анкор» ----------


def test_quick_add_creates_anchor(admin_client: Client, convertio: Product) -> None:
    answer = admin_client.post(
        ADD,
        {
            "product": convertio.pk,
            "keyword": "  mp4   to mp3 ",
            "target_url": "https://convertio.co/mp4-mp3/",
            "page_type": "Video",
        },
    ).json()
    assert answer["ok"] and not answer["existing"]
    keyword = Keyword.objects.get(pk=answer["id"])
    assert (keyword.keyword, keyword.target_url, keyword.page_type, keyword.anchor_type) == (
        "mp4 to mp3",
        "https://convertio.co/mp4-mp3/",
        "Video",
        AnchorType.EXACT,
    )
    assert answer["url"] == "https://convertio.co/mp4-mp3/"


def test_quick_add_existing_is_chosen(
    admin_client: Client, convertio: Product, anchors: dict[str, Keyword]
) -> None:
    answer = admin_client.post(
        ADD, {"product": convertio.pk, "keyword": "MP4 Converter", "target_url": ""}
    ).json()
    assert answer["ok"] and answer["existing"]
    assert answer["id"] == anchors["mp4"].pk


def test_quick_add_brand_detected(admin_client: Client, convertio: Product) -> None:
    answer = admin_client.post(
        ADD,
        {
            "product": convertio.pk,
            "keyword": "Visit Convertio",
            "target_url": "https://convertio.co/",
        },
    ).json()
    assert Keyword.objects.get(pk=answer["id"]).anchor_type == AnchorType.BRANDED


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"keyword": "", "target_url": "https://convertio.co/"}, "Впишите анкор."),
        ({"keyword": "mp3", "target_url": "convertio.co/mp3"}, "Впишите адрес страницы целиком"),
    ],
)
def test_quick_add_errors(
    admin_client: Client, convertio: Product, fields: dict[str, str], message: str
) -> None:
    answer = admin_client.post(ADD, {"product": convertio.pk, **fields}).json()
    assert not answer["ok"]
    assert answer["message"].startswith(message)


def test_quick_add_needs_rights(client: Client, convertio: Product) -> None:
    staff = User.objects.create_user("viewer", password="x", is_staff=True)
    client.force_login(staff)
    answer = client.post(ADD, {"product": convertio.pk, "keyword": "x"}).json()
    assert not answer["ok"]
    assert not Keyword.objects.exists()


# ---------- Доли на месте ----------


def test_share_type_target(admin_client: Client, convertio: Product) -> None:
    data = {"kind": "type", "id": "Video", "field": "target", "value": "55,5"}
    answer = admin_client.post(SHARE, {**data, "product": convertio.pk}).json()
    assert answer == {"ok": True, "value": "55.5", "text": "55,5%"}
    assert PageTypeShare.objects.get(page_type="Video").target_pct == D("55.50")


def test_share_new_type_and_clear(admin_client: Client, convertio: Product) -> None:
    admin_client.post(
        SHARE,
        {"kind": "type", "id": "Audio", "field": "exact", "value": "85", "product": convertio.pk},
    )
    share = PageTypeShare.objects.get(page_type="Audio")
    assert (share.exact_pct, share.position) == (D("85.00"), 3)
    admin_client.post(
        SHARE,
        {"kind": "type", "id": "Audio", "field": "exact", "value": "", "product": convertio.pk},
    )
    share.refresh_from_db()
    assert share.exact_pct is None


def test_share_naked_and_country(
    admin_client: Client, convertio: Product, anchors: dict[str, Keyword]
) -> None:
    brand = anchors["brand"].pk
    admin_client.post(SHARE, {"kind": "naked", "id": brand, "field": "share", "value": "60"})
    anchors["brand"].refresh_from_db()
    assert anchors["brand"].share == D("60.00")
    us = CountryShare.objects.get(country="us")
    admin_client.post(SHARE, {"kind": "country", "id": us.pk, "field": "target", "value": "45"})
    us.refresh_from_db()
    assert us.target_pct == D("45.00")


@pytest.mark.parametrize("value", ["101", "-1", "abc"])
def test_share_rejects_bad_value(admin_client: Client, convertio: Product, value: str) -> None:
    answer = admin_client.post(
        SHARE,
        {"kind": "type", "id": "Video", "field": "target", "value": value, "product": convertio.pk},
    ).json()
    assert not answer["ok"]
    assert PageTypeShare.objects.get(page_type="Video").target_pct == D(60)


# ---------- Блок «Анкоры продукта» в размещении ----------


def test_block_in_new_placement(
    admin_client: Client, admin_user: User, convertio: Product, anchors: dict[str, Keyword]
) -> None:
    choose_product(admin_user, convertio)
    response = admin_client.get(PLACEMENT_ADD)
    block = response.context["anchor_block"]
    picks = [pick["keyword"] for pick in block["picks"]]
    # Video недобрано (0% при цели 60%) — первым; ключ в топ-3 не предлагается.
    assert picks[0] == "mp4 converter"
    assert "video converter" not in picks
    page = response.content.decode()
    assert "Рекомендуем" in page and "data-anchor-pick" in page
    assert "data-anchor-dialog" in page
    assert "Ключи на позициях 1–3 не предлагаются" in page


def test_block_excludes_anchors_of_this_placement(
    admin_client: Client, convertio: Product, anchors: dict[str, Keyword]
) -> None:
    placement = Placement.objects.get()
    response = admin_client.get(reverse("admin:placements_placement_change", args=[placement.pk]))
    picks = [pick["id"] for pick in response.context["anchor_block"]["picks"]]
    assert anchors["convert"].pk not in picks
    assert anchors["mp4"].pk in picks


def test_summary_fragment(
    admin_client: Client, convertio: Product, anchors: dict[str, Keyword]
) -> None:
    url = reverse("admin:keywords_anchor_summary", args=[convertio.pk])
    page = admin_client.get(url, {"exclude": anchors["mp4"].pk}).content.decode()
    assert "Анкоры Convertio" in page
    assert f'data-anchor-pick="{anchors["mp4"].pk}"' not in page
