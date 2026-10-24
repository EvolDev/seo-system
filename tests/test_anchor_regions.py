"""Списки анкоров по регионам (ADR-075): угадывание региона, доли по списку, форма
размещения, окно «Новый анкор», страница «Анкоры» и удаление списка."""

import re
from decimal import Decimal
from typing import Any

import pytest
from django.contrib.auth.models import Permission, User
from django.db import IntegrityError, transaction
from django.test import Client
from django.urls import reverse

from apps.keywords import matching, regions
from apps.keywords.anchors import overview
from apps.keywords.models import (
    AnchorList,
    AnchorType,
    CountryShare,
    Keyword,
    KeywordPosition,
)
from apps.placements.forms import PlacementLinkForm
from apps.placements.models import Placement, PlacementLink, PlacementStatus
from apps.sites.models import Product, Site
from apps.workspace.products import choose_product
from config.admin import PARTIAL_HEADER

pytestmark = pytest.mark.django_db

HOME = "https://convertio.co/"
HOME_PT = "https://convertio.co/pt/"
MP3 = "https://convertio.co/mp4-mp3/"
MP3_PT = "https://convertio.co/pt/mp4-mp3/"
PAGE = reverse("admin:keywords_keywordcoverage_changelist")


@pytest.fixture
def convertio() -> Product:
    return Product.objects.create(name="Convertio", domain="convertio.co")


@pytest.fixture
def us(convertio: Product) -> AnchorList:
    return AnchorList.objects.create(product=convertio, country="us")


@pytest.fixture
def br(convertio: Product, us: AnchorList) -> AnchorList:
    return AnchorList.objects.create(product=convertio, country="br")


@pytest.fixture
def anchors(convertio: Product, us: AnchorList, br: AnchorList) -> dict[str, Keyword]:
    """Одни и те же тексты в двух списках — на разные страницы."""
    made = {}
    for name, anchor_list, text, url, kind in (
        ("us_mp3", us, "mp4 to mp3", MP3, AnchorType.EXACT),
        ("br_mp3", br, "mp4 to mp3", MP3_PT, AnchorType.EXACT),
        ("us_brand", us, "Convertio", HOME, AnchorType.BRANDED),
        ("br_brand", br, "Convertio", HOME_PT, AnchorType.BRANDED),
    ):
        made[name] = Keyword.objects.create(
            product=convertio,
            anchor_list=anchor_list,
            keyword=text,
            target_url=url,
            page_type="Video",
            anchor_type=kind,
        )
    return made


def _text(html: str) -> str:
    """Текст страницы без разметки — подписи и числа подтверждения рядом."""
    return " ".join(re.sub(r"<[^>]+>", " ", html).split())


def _placement(
    product: Product, domain: str, *, language: str | None = None, **fields: Any
) -> Placement:
    site = Site.objects.create(domain=domain, language=language)
    return Placement.objects.create(site=site, product=product, **fields)


# ---------- Модель ----------


class TestModel:
    def test_same_text_in_two_lists(self, anchors: dict[str, Keyword], us: AnchorList) -> None:
        assert anchors["us_mp3"].keyword == anchors["br_mp3"].keyword
        with pytest.raises(IntegrityError), transaction.atomic():
            Keyword.objects.create(
                product=us.product, anchor_list=us, keyword="mp4 to mp3", target_url=MP3
            )

    def test_keyword_without_list_goes_to_main(self, convertio: Product) -> None:
        # Продукт без списков: первый анкор заводит основной — США.
        keyword = Keyword.objects.create(product=convertio, keyword="convert", target_url=HOME)
        assert (keyword.anchor_list.country, keyword.anchor_list.region_name) == ("us", "США")
        again = Keyword.objects.create(product=convertio, keyword="convert file", target_url=HOME)
        assert again.anchor_list == keyword.anchor_list

    def test_list_gives_product(self, br: AnchorList) -> None:
        keyword = Keyword.objects.create(anchor_list=br, keyword="converter", target_url=HOME_PT)
        assert keyword.product_id == br.product_id

    def test_main_is_oldest(self, convertio: Product, us: AnchorList, br: AnchorList) -> None:
        assert AnchorList.main_of(convertio.pk) == us
        assert AnchorList.of_product(convertio.pk) == [us, br]


# ---------- Какой список у размещения ----------


class TestChoose:
    def test_by_site_language(self, us: AnchorList, br: AnchorList) -> None:
        choice = regions.choose(Site(domain="acritica.net", language="pt"), [us, br])
        assert (choice.anchor_list, choice.reason) == (br, "по языку площадки")

    def test_by_domain_zone(self, us: AnchorList, br: AnchorList) -> None:
        choice = regions.choose(Site(domain="dci.com.br"), [us, br])
        assert (choice.anchor_list, choice.reason) == (br, "по домену .br")

    @pytest.mark.parametrize(
        ("language", "domain", "reason"),
        [
            ("de", "spiegel.de", "для языка «Немецкий» списка нет — взят «США»"),
            (None, "example.com", "язык площадки не известен — взят «США»"),
        ],
    )
    def test_fallback_to_main(
        self, us: AnchorList, br: AnchorList, language: str | None, domain: str, reason: str
    ) -> None:
        choice = regions.choose(Site(domain=domain, language=language), [us, br])
        assert (choice.anchor_list, choice.reason) == (us, reason)

    def test_stored_then_linked(self, us: AnchorList, br: AnchorList) -> None:
        site = Site(domain="example.com", language="en")
        stored = regions.choose(site, [us, br], stored=br.pk)
        assert (stored.anchor_list, stored.reason) == (br, "сохранён в размещении")
        linked = regions.choose(site, [us, br], linked=[br.pk])
        assert (linked.anchor_list, linked.reason) == (br, "по анкорам ссылок")
        # Ссылки из двух списков — не подсказка: дальше по языку.
        assert regions.choose(site, [us, br], linked=[us.pk, br.pk]).anchor_list == us

    def test_no_lists(self) -> None:
        assert regions.choose(Site(domain="a.com"), []).anchor_list is None

    def test_placement_countries(self, convertio: Product, us: AnchorList, br: AnchorList) -> None:
        _placement(convertio, "a.com", language="en", status=PlacementStatus.PLACED)
        _placement(convertio, "b.com.br", status=PlacementStatus.ORDERED)
        _placement(convertio, "c.com", anchor_list=br, status=PlacementStatus.IN_WORK)
        _placement(convertio, "d.com", status=PlacementStatus.REJECTED)
        assert regions.placement_countries(convertio.pk) == {"us": 1, "br": 2}


# ---------- Доли и поиск по списку ----------


class TestOverview:
    def test_links_count_in_own_list(
        self, convertio: Product, anchors: dict[str, Keyword], us: AnchorList, br: AnchorList
    ) -> None:
        placement = _placement(convertio, "a.com.br", anchor_list=br)
        PlacementLink.objects.create(
            placement=placement, keyword=anchors["br_mp3"], anchor="mp4 to mp3", target_url=MP3_PT
        )
        PlacementLink.objects.create(placement=placement, anchor="clique aqui", target_url=HOME_PT)
        legacy = _placement(convertio, "b.com")
        PlacementLink.objects.create(placement=legacy, anchor="click here", target_url=HOME)
        brazil = overview(convertio.pk, br)
        assert {a.keyword for a in brazil.anchors} == {"mp4 to mp3", "Convertio"}
        assert sum(a.waiting for a in brazil.anchors) == 1
        assert brazil.loose_waiting == 1
        usa = overview(convertio.pk, us)
        assert sum(a.waiting for a in usa.anchors) == 0
        # Размещения без списка — у основного.
        assert usa.loose_waiting == 1
        assert overview(convertio.pk).anchors == usa.anchors

    def test_position_by_list_country(
        self, anchors: dict[str, Keyword], br: AnchorList, convertio: Product
    ) -> None:
        keyword = anchors["br_mp3"]
        KeywordPosition.objects.create(keyword=keyword, country="US", position=40)
        KeywordPosition.objects.create(keyword=keyword, country="BR", position=7)
        found = {a.id: a.position for a in overview(convertio.pk, br).anchors}
        assert found[keyword.pk] == 7

    def test_matching_pool_is_one_list(
        self, convertio: Product, anchors: dict[str, Keyword], br: AnchorList
    ) -> None:
        found = matching.find("Convertio", matching.candidates(convertio.pk, br.pk))
        assert found.keyword is not None and found.keyword.id == anchors["br_brand"].pk
        main = matching.find("Convertio", matching.candidates(convertio.pk))
        assert main.keyword is not None and main.keyword.id == anchors["us_brand"].pk


# ---------- Форма размещения ----------


def _change_url(placement: Placement) -> str:
    return reverse("admin:placements_placement_change", args=[placement.pk])


def _post_data(**fields: str) -> dict[str, str]:
    data = {
        "status": "in_work",
        "placement_type": "",
        "seller": "",
        "employee": "",
        "collaborator_order_id": "",
        "ordered_at": "",
        "price_paid_cents": "",
        "currency": "EUR",
        "article_url": "",
        "published_at": "",
        "announce_on_homepage": "unknown",
        "clicks_from_homepage": "",
        "comment": "",
        "links-TOTAL_FORMS": "0",
        "links-INITIAL_FORMS": "0",
        "links-MIN_NUM_FORMS": "0",
        "links-MAX_NUM_FORMS": "1000",
    }
    data.update(fields)
    return data


def _with_link(keyword: Keyword, **fields: str) -> dict[str, str]:
    return _post_data(
        **{
            "links-TOTAL_FORMS": "1",
            "links-0-keyword": str(keyword.pk),
            "links-0-text": "",
            "links-0-kind": "",
            "links-0-target_url": "",
        },
        **fields,
    )


class TestPlacementCard:
    def test_block_guesses_by_domain(
        self, admin_client: Client, convertio: Product, anchors: dict[str, Keyword], br: AnchorList
    ) -> None:
        placement = _placement(convertio, "dci.com.br")
        page = admin_client.get(_change_url(placement), headers={PARTIAL_HEADER: "1"})
        html = page.content.decode()
        assert 'name="anchor_list" data-anchor-list' in html
        assert f'<option value="{br.pk}" data-flag="br" selected>Бразилия</option>' in html
        assert "по домену .br" in html
        assert f'data-list="{br.pk}"' in html  # варианты анкоров помечены списком

    def test_choice_is_saved(
        self, admin_client: Client, convertio: Product, us: AnchorList, br: AnchorList
    ) -> None:
        placement = _placement(convertio, "example.com", language="en")
        response = admin_client.post(
            _change_url(placement),
            _post_data(anchor_list=str(br.pk)),
            headers={PARTIAL_HEADER: "1"},
        )
        assert response.json()["saved"] is True
        placement.refresh_from_db()
        assert placement.anchor_list == br
        page = admin_client.get(_change_url(placement)).content.decode()
        assert "сохранён в размещении" in page

    def test_form_without_block_keeps_list(
        self, admin_client: Client, convertio: Product, br: AnchorList
    ) -> None:
        placement = _placement(convertio, "example.com", anchor_list=br)
        admin_client.post(_change_url(placement), _post_data(), headers={PARTIAL_HEADER: "1"})
        placement.refresh_from_db()
        assert placement.anchor_list == br

    def test_anchor_of_other_list_is_rejected(
        self, admin_client: Client, convertio: Product, anchors: dict[str, Keyword], br: AnchorList
    ) -> None:
        placement = _placement(convertio, "dci.com.br")
        response = admin_client.post(
            _change_url(placement),
            _with_link(anchors["us_mp3"], anchor_list=str(br.pk)),
            headers={PARTIAL_HEADER: "1"},
        )
        assert "Анкор из списка «США», а у размещения — «Бразилия»" in response.content.decode()
        assert not placement.links.exists()
        response = admin_client.post(
            _change_url(placement),
            _with_link(anchors["br_mp3"], anchor_list=str(br.pk)),
            headers={PARTIAL_HEADER: "1"},
        )
        assert response.json()["saved"] is True
        link = placement.links.get()
        assert (link.keyword, link.target_url) == (anchors["br_mp3"], MP3_PT)

    def test_old_link_of_other_list_stays(
        self, convertio: Product, anchors: dict[str, Keyword], br: AnchorList
    ) -> None:
        placement = _placement(convertio, "dci.com.br", anchor_list=br)
        link = PlacementLink.objects.create(
            placement=placement, keyword=anchors["us_mp3"], anchor="mp4 to mp3", target_url=MP3
        )
        data = {
            "keyword": str(link.keyword_id),
            "text": "mp4 to mp3",
            "kind": "",
            "target_url": MP3,
        }
        form = PlacementLinkForm(
            data=data, instance=link, product_id=convertio.pk, placement=placement
        )
        assert form.is_valid(), form.errors

    def test_phrase_is_matched_in_placement_list(
        self, convertio: Product, anchors: dict[str, Keyword], br: AnchorList
    ) -> None:
        placement = _placement(convertio, "dci.com.br", anchor_list=br)
        data = {"keyword": "", "text": "Convertio", "kind": "", "target_url": ""}
        form = PlacementLinkForm(
            data=data,
            instance=PlacementLink(placement=placement),
            product_id=convertio.pk,
            placement=placement,
        )
        assert form.is_valid(), form.errors
        assert form.save().keyword == anchors["br_brand"]

    def test_summary_for_chosen_list(
        self, admin_client: Client, convertio: Product, anchors: dict[str, Keyword], br: AnchorList
    ) -> None:
        url = reverse("admin:keywords_anchor_summary", args=[convertio.pk])
        page = admin_client.get(url, {"list": br.pk}).content.decode()
        assert f'<option value="{br.pk}" data-flag="br" selected>Бразилия</option>' in page
        assert f"list={br.pk}" in page  # «Все анкоры» — на этот список
        site = Site.objects.create(domain="segs.com.br")
        guessed = admin_client.get(url, {"site": site.pk}).content.decode()
        assert "по домену .br" in guessed

    def test_match_view_uses_list(
        self, admin_client: Client, convertio: Product, anchors: dict[str, Keyword], br: AnchorList
    ) -> None:
        url = reverse("admin:keywords_anchor_match")
        answer = admin_client.get(
            url, {"product": str(convertio.pk), "list": str(br.pk), "text": "Convertio"}
        ).json()
        assert answer["chosen"] == anchors["br_brand"].pk


# ---------- Окно «Новый анкор» ----------


class TestQuickAdd:
    URL = reverse("admin:keywords_anchor_add")

    def test_into_chosen_list(
        self, admin_client: Client, convertio: Product, anchors: dict[str, Keyword], br: AnchorList
    ) -> None:
        answer = admin_client.post(
            self.URL,
            {
                "product": convertio.pk,
                "list": br.pk,
                "keyword": "conversor de vídeo",
                "target_url": "https://convertio.co/pt/video-converter/",
            },
        ).json()
        assert answer["ok"] and answer["list"] == br.pk
        assert "в список «Бразилия»" in answer["message"]
        assert Keyword.objects.get(pk=answer["id"]).anchor_list == br

    def test_existing_found_only_in_same_list(
        self, admin_client: Client, convertio: Product, anchors: dict[str, Keyword], br: AnchorList
    ) -> None:
        answer = admin_client.post(
            self.URL, {"product": convertio.pk, "list": br.pk, "keyword": "MP4 to MP3"}
        ).json()
        assert answer["existing"] and answer["id"] == anchors["br_mp3"].pk

    def test_list_of_other_product_falls_back_to_main(
        self, admin_client: Client, convertio: Product, us: AnchorList
    ) -> None:
        other = AnchorList.objects.create(
            product=Product.objects.create(name="Clideo", domain="clideo.com"), country="br"
        )
        answer = admin_client.post(
            self.URL,
            {"product": convertio.pk, "list": other.pk, "keyword": "convert", "target_url": HOME},
        ).json()
        assert answer["ok"] and answer["list"] == us.pk


# ---------- Страница «Анкоры» ----------


class TestAnchorsPage:
    @pytest.fixture(autouse=True)
    def working(self, admin_user: User, convertio: Product) -> None:
        choose_product(admin_user, convertio)

    def test_main_list_by_default(
        self, admin_client: Client, anchors: dict[str, Keyword], us: AnchorList, br: AnchorList
    ) -> None:
        response = admin_client.get(PAGE)
        assert response.context["title"] == "Анкоры Convertio · США"
        rows = list(response.context["cl"].result_list)
        assert {row.anchor_list_id for row in rows} == {us.pk}
        html = response.content.decode()
        assert 'data-flag="br"' in html and 'data-flag="us"' in html
        assert "&amp;anchor_country=us" in html  # загрузка — в открытый список

    def test_choice_is_remembered(
        self, admin_client: Client, anchors: dict[str, Keyword], br: AnchorList
    ) -> None:
        admin_client.get(PAGE, {"list": br.pk})
        response = admin_client.get(PAGE)
        assert response.context["title"] == "Анкоры Convertio · Бразилия"
        assert {row.anchor_list_id for row in response.context["cl"].result_list} == {br.pk}

    def test_positions_by_list_country(
        self, admin_client: Client, anchors: dict[str, Keyword], br: AnchorList
    ) -> None:
        keyword = anchors["br_mp3"]
        KeywordPosition.objects.create(keyword=keyword, country="BR", position=7)
        KeywordPosition.objects.create(keyword=keyword, country="US", position=40)
        response = admin_client.get(PAGE, {"list": br.pk})
        html = response.content.decode()
        assert '<span class="seo-anchor-pos">7</span>' in html
        assert '<span class="seo-anchor-pos">40</span>' not in html

    def test_delete_button_and_confirmation(
        self, admin_client: Client, convertio: Product, anchors: dict[str, Keyword], br: AnchorList
    ) -> None:
        placement = _placement(convertio, "dci.com.br", anchor_list=br)
        PlacementLink.objects.create(
            placement=placement, keyword=anchors["br_mp3"], anchor="mp4 to mp3", target_url=MP3_PT
        )
        KeywordPosition.objects.create(keyword=anchors["br_mp3"], country="BR", position=7)
        delete_url = reverse("admin:keywords_anchorlist_delete", args=[br.pk])
        html = admin_client.get(PAGE, {"list": br.pk}).content.decode()
        assert delete_url in html and "Удалить список «Бразилия»" in html
        confirm = _text(admin_client.get(delete_url).content.decode())
        for line in (
            "Ключи: 2",
            "Позиции ключей: 1",
            "Размещения останутся без «список анкоров»: 1",
            "Ссылки останутся без «ключ»: 1",
        ):
            assert line in confirm
        admin_client.post(delete_url, {"post": "yes"})
        assert not AnchorList.objects.filter(pk=br.pk).exists()
        assert set(Keyword.objects.values_list("keyword", flat=True)) == {"mp4 to mp3", "Convertio"}
        assert not KeywordPosition.objects.exists()
        placement.refresh_from_db()
        link = placement.links.get()
        assert (placement.anchor_list, link.keyword, link.anchor) == (None, None, "mp4 to mp3")

    def test_country_facts(
        self, admin_client: Client, convertio: Product, us: AnchorList, br: AnchorList
    ) -> None:
        CountryShare.objects.create(product=convertio, country="us", target_pct=Decimal("50"))
        CountryShare.objects.create(product=convertio, country="br", target_pct=Decimal("30"))
        CountryShare.objects.create(product=convertio, country=None, target_pct=Decimal("20"))
        _placement(convertio, "a.com", language="en", status=PlacementStatus.PLACED)
        _placement(convertio, "b.com.br", status=PlacementStatus.ORDERED)
        _placement(convertio, "c.com.br", status=PlacementStatus.ORDERED)
        _placement(convertio, "d.de", status=PlacementStatus.ORDERED, anchor_list=None)
        rows = admin_client.get(PAGE).context["anchors_page"]["countries"]
        found = {row["name"]: (row["count"], row["fact"]) for row in rows}
        # Немецкая площадка без своего списка угадана как США (основной).
        assert found == {"США": (2, "50%"), "Бразилия": (2, "50%"), "Остальные": (0, "0%")}

    def test_country_facts_without_targets(
        self, admin_client: Client, convertio: Product, us: AnchorList, br: AnchorList
    ) -> None:
        _placement(convertio, "b.com.br", status=PlacementStatus.ORDERED)
        rows = admin_client.get(PAGE).context["anchors_page"]["countries"]
        assert [(row["name"], row["count"], row["share"]) for row in rows] == [("Бразилия", 1, "")]

    def test_viewer_has_no_delete_button(
        self, client: Client, convertio: Product, anchors: dict[str, Keyword], br: AnchorList
    ) -> None:
        user = User.objects.create_user("viewer", password="x", is_staff=True)
        user.user_permissions.add(Permission.objects.get(codename="view_keywordcoverage"))
        client.force_login(user)
        html = client.get(PAGE, {"product": convertio.pk, "list": br.pk}).content.decode()
        assert "Удалить список" not in html
