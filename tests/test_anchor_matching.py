"""Разбавленные анкоры (ADR-069): ключ во фразе, вид ссылки, форма, подсказка, доли."""

from decimal import Decimal

import pytest
from django.test import Client
from django.urls import reverse

from apps.keywords import matching
from apps.keywords.anchors import Anchor, Share, overview, recommend
from apps.keywords.matching import Candidate
from apps.keywords.models import AnchorList, AnchorType, Keyword, PageTypeShare
from apps.placements.forms import PlacementLinkForm
from apps.placements.models import Placement, PlacementLink, PlacementStatus
from apps.sites.models import Product, Site
from apps.sites.uploads.anchors import attach_links

pytestmark = pytest.mark.django_db

D = Decimal
MP3 = "https://convertio.co/mp4-mp3/"
VIDEO = "https://convertio.co/video-converter/"
HOME = "https://convertio.co/"


def _c(pk: int, text: str, url: str = MP3, kind: str | None = "exact") -> Candidate:
    return Candidate(pk, text, url, kind)


POOL = [
    _c(1, "convert", HOME),
    _c(2, "mp4 to mp3", MP3),
    _c(3, "mp4 to mp3 converter", MP3),
    _c(4, "video converter", VIDEO),
    _c(5, "Convertio", HOME, "branded"),
    _c(6, "here", HOME, "generic"),
    _c(7, "convert mp4 to mp3", MP3),
    _c(8, "mp4 to mp3 online", "https://convertio.co/mp4-mp3-online/"),
]


# ---------- Поиск и вид ----------


@pytest.mark.parametrize(
    ("text", "keyword", "kind"),
    [
        ("Convertio's mp4 to mp3 converter", "mp4 to mp3 converter", AnchorType.DILUTED),
        ("Convertio video converter", "video converter", AnchorType.DILUTED),
        ("Convertio's mp4 to mp3 tool", "mp4 to mp3", AnchorType.DILUTED),
        ("mp4 to mp3", "mp4 to mp3", AnchorType.EXACT),
        ("  MP4   to MP3!", "mp4 to mp3", AnchorType.EXACT),
        ("convertio", "Convertio", AnchorType.BRANDED),
    ],
)
def test_user_examples(text: str, keyword: str, kind: AnchorType) -> None:
    found, got = matching.bind(text, POOL)
    assert found.keyword is not None and found.keyword.keyword == keyword
    assert got == kind


def test_short_key_is_not_found_inside_words() -> None:
    # «convert» сидит внутри «Convertio» и «converter», но словом не является.
    found = matching.find("Convertio converter", POOL)
    assert found.keyword is None and found.candidates == ()
    found = matching.find("Convertio's mp4 to mp3 converter", POOL)
    assert "convert" not in {c.keyword for c in found.candidates}


def test_longest_first_and_nested_is_not_ambiguous() -> None:
    found = matching.find("Convertio's mp4 to mp3 converter", POOL)
    assert [c.keyword for c in found.candidates] == ["mp4 to mp3 converter", "mp4 to mp3"]
    assert not found.ambiguous


def test_equal_length_goes_to_older_key() -> None:
    pool = [_c(9, "mp3 to wav"), _c(3, "wav to mp3")]
    found = matching.find("wav to mp3 to wav", pool)
    assert found.keyword is not None and found.keyword.id == 3


def test_overlap_on_different_pages_is_ambiguous() -> None:
    found = matching.find("convert mp4 to mp3 online", POOL)
    assert found.ambiguous and found.keyword is None
    assert {c.keyword for c in found.candidates} >= {"convert mp4 to mp3", "mp4 to mp3 online"}
    assert "разные страницы" in matching.why_unbound(found)


def test_link_url_resolves_overlap() -> None:
    found = matching.find("convert mp4 to mp3 online", POOL, url=MP3 + "/")
    assert found.keyword is not None and found.keyword.keyword == "convert mp4 to mp3"
    online = matching.find(
        "convert mp4 to mp3 online", POOL, url="https://convertio.co/mp4-mp3-online/"
    )
    assert online.keyword is not None and online.keyword.keyword == "mp4 to mp3 online"


def test_naked_only_as_whole_text() -> None:
    assert matching.find("click here for files", POOL).keyword is None
    found = matching.find("Here", POOL)
    assert found.exact and found.keyword is not None and found.keyword.keyword == "here"


def test_word_forms_are_not_guessed() -> None:
    found = matching.find("mp4 to mp3 converters", POOL)
    assert found.keyword is not None and found.keyword.keyword == "mp4 to mp3"
    assert matching.find("converting mp4 into mp3", POOL).keyword is None


def test_kind_without_key_in_text_is_unknown() -> None:
    assert matching.kind("click here", "mp4 to mp3", "exact") is None
    assert matching.kind("Convertio's website", "Convertio", "branded") == AnchorType.BRANDED


# ---------- Форма ссылки ----------


@pytest.fixture
def convertio() -> Product:
    return Product.objects.create(name="Convertio", domain="convertio.co")


@pytest.fixture
def keys(convertio: Product) -> dict[str, Keyword]:
    found = {}
    for text, url in (
        ("mp4 to mp3", MP3),
        ("mp4 to mp3 converter", MP3),
        ("convert mp4 to mp3", MP3),
        ("mp4 to mp3 online", "https://convertio.co/mp4-mp3-online/"),
    ):
        found[text] = Keyword.objects.create(
            product=convertio,
            keyword=text,
            target_url=url,
            page_type="Video",
            anchor_type=AnchorType.EXACT,
        )
    return found


@pytest.fixture
def placement(convertio: Product) -> Placement:
    return Placement.objects.create(
        site=Site.objects.create(domain="a.com"), product=convertio, status=PlacementStatus.ORDERED
    )


def _form(
    placement: Placement,
    *,
    keyword: Keyword | None = None,
    text: str = "",
    kind: str = "",
    url: str = "",
    instance: PlacementLink | None = None,
) -> PlacementLinkForm:
    data = {
        "keyword": str(keyword.pk) if keyword else "",
        "text": text,
        "kind": kind,
        "target_url": url,
    }
    link = instance or PlacementLink(placement=placement)
    return PlacementLinkForm(data=data, instance=link, product_id=placement.product_id)


def test_form_keeps_own_text_and_marks_diluted(
    placement: Placement, keys: dict[str, Keyword]
) -> None:
    form = _form(
        placement, keyword=keys["mp4 to mp3 converter"], text="Convertio's mp4 to mp3 converter"
    )
    assert form.is_valid(), form.errors
    link = form.save()
    assert (link.anchor, link.anchor_type, link.target_url) == (
        "Convertio's mp4 to mp3 converter",
        AnchorType.DILUTED,
        MP3,
    )


def test_form_empty_text_is_exact_as_before(placement: Placement, keys: dict[str, Keyword]) -> None:
    form = _form(placement, keyword=keys["mp4 to mp3"])
    assert form.is_valid(), form.errors
    link = form.save()
    assert (link.anchor, link.anchor_type) == ("mp4 to mp3", AnchorType.EXACT)


def test_form_finds_key_in_typed_phrase(placement: Placement, keys: dict[str, Keyword]) -> None:
    form = _form(placement, text="Convertio's mp4 to mp3 converter")
    assert form.is_valid(), form.errors
    link = form.save()
    assert link.keyword == keys["mp4 to mp3 converter"]
    assert link.anchor_type == AnchorType.DILUTED


def test_form_ambiguous_phrase_asks_for_key(placement: Placement, keys: dict[str, Keyword]) -> None:
    form = _form(placement, text="convert mp4 to mp3 online")
    assert not form.is_valid()
    assert "разные страницы" in form.errors["keyword"][0]


def test_form_url_settles_ambiguous_phrase(placement: Placement, keys: dict[str, Keyword]) -> None:
    form = _form(placement, text="convert mp4 to mp3 online", url=MP3)
    assert form.is_valid(), form.errors
    assert form.save().keyword == keys["convert mp4 to mp3"]


def test_form_manual_key_without_it_in_text_needs_kind(
    placement: Placement, keys: dict[str, Keyword]
) -> None:
    form = _form(placement, keyword=keys["mp4 to mp3"], text="click here")
    assert not form.is_valid()
    assert "во фразе нет" in form.errors["kind"][0]
    form = _form(placement, keyword=keys["mp4 to mp3"], text="click here", kind="generic")
    assert form.is_valid(), form.errors
    link = form.save()
    assert (link.keyword, link.anchor_type) == (keys["mp4 to mp3"], AnchorType.GENERIC)


def test_form_free_phrase_without_key_is_saved(
    placement: Placement, keys: dict[str, Keyword]
) -> None:
    form = _form(placement, text="this handy tool", url=MP3)
    assert form.is_valid(), form.errors
    link = form.save()
    assert (link.keyword, link.anchor, link.anchor_type) == (None, "this handy tool", None)


def test_form_rejects_key_of_other_product(placement: Placement) -> None:
    other = Product.objects.create(name="Clideo", domain="clideo.com")
    foreign = Keyword.objects.create(product=other, keyword="video editor", target_url=VIDEO)
    form = _form(placement, keyword=foreign)
    assert not form.is_valid()
    assert "другого продукта" in form.errors["keyword"][0]


def test_saved_link_keeps_text_when_key_changes(
    placement: Placement, keys: dict[str, Keyword]
) -> None:
    link = PlacementLink.objects.create(
        placement=placement,
        keyword=keys["mp4 to mp3 converter"],
        anchor="Convertio's mp4 to mp3 converter",
        anchor_type=AnchorType.DILUTED,
        target_url="https://convertio.co/own/",
    )
    form = _form(
        placement,
        keyword=keys["mp4 to mp3"],
        text=link.anchor,
        url=link.target_url,
        instance=link,
    )
    assert form.is_valid(), form.errors
    form.save()
    link.refresh_from_db()
    assert (link.keyword, link.anchor, link.target_url) == (
        keys["mp4 to mp3"],
        "Convertio's mp4 to mp3 converter",
        "https://convertio.co/own/",
    )


def test_manual_kind_is_shown_and_kept(placement: Placement, keys: dict[str, Keyword]) -> None:
    link = PlacementLink.objects.create(
        placement=placement,
        keyword=keys["mp4 to mp3"],
        anchor="mp4 to mp3",
        anchor_type=AnchorType.BRANDED,
        target_url=MP3,
    )
    form = PlacementLinkForm(instance=link, product_id=placement.product_id)
    assert form.fields["kind"].initial == AnchorType.BRANDED
    auto = PlacementLink.objects.create(
        placement=placement,
        keyword=keys["mp4 to mp3"],
        anchor="mp4 to mp3",
        anchor_type=AnchorType.EXACT,
        target_url=MP3,
    )
    assert PlacementLinkForm(instance=auto).fields["kind"].initial is None


def test_saved_link_without_key_is_not_bound_silently(
    placement: Placement, keys: dict[str, Keyword]
) -> None:
    link = PlacementLink.objects.create(
        placement=placement, anchor="mp4 to mp3 converter", target_url=MP3
    )
    form = PlacementLinkForm(
        data={"keyword": "", "text": link.anchor, "kind": "", "target_url": MP3},
        initial={"text": link.anchor},
        instance=link,
        product_id=placement.product_id,
    )
    assert form.is_valid(), form.errors
    form.save()
    link.refresh_from_db()
    assert link.keyword is None


# ---------- Подсказка в панели ----------


def test_match_view(admin_client: Client, convertio: Product, keys: dict[str, Keyword]) -> None:
    url = reverse("admin:keywords_anchor_match")
    answer = admin_client.get(
        url, {"product": str(convertio.pk), "text": "Convertio's mp4 to mp3 converter"}
    ).json()
    assert answer["ok"] and not answer["ambiguous"]
    assert answer["chosen"] == keys["mp4 to mp3 converter"].pk
    assert [c["keyword"] for c in answer["candidates"]] == ["mp4 to mp3 converter", "mp4 to mp3"]
    assert (answer["kind"], answer["kind_label"]) == ("diluted", "Разбавленный")
    manual = admin_client.get(
        url,
        {"product": str(convertio.pk), "text": "click here", "keyword": str(keys["mp4 to mp3"].pk)},
    ).json()
    assert manual["kind"] is None and manual["candidates"] == []
    ambiguous = admin_client.get(
        url, {"product": str(convertio.pk), "text": "convert mp4 to mp3 online"}
    ).json()
    assert ambiguous["ambiguous"] and ambiguous["chosen"] is None


def test_panel_has_text_and_kind_fields(admin_client: Client, convertio: Product) -> None:
    page = admin_client.get(reverse("admin:placements_placement_add")).content.decode()
    assert 'name="links-0-text"' in page and "data-anchor-text" in page
    assert 'name="links-0-kind"' in page and "data-match-url" in page


# ---------- Привязка при загрузке анкоров ----------


def test_attach_links_finds_key_inside_phrase(
    placement: Placement, keys: dict[str, Keyword]
) -> None:
    diluted = PlacementLink.objects.create(
        placement=placement, anchor="Convertio's mp4 to mp3 converter", target_url=MP3
    )
    typed = PlacementLink.objects.create(
        placement=placement,
        anchor="mp4 to mp3",
        target_url=MP3,
        anchor_type=AnchorType.GENERIC,
    )
    ambiguous = PlacementLink.objects.create(
        placement=placement,
        anchor="convert mp4 to mp3 online",
        target_url="https://convertio.co/other/",
    )
    assert attach_links(AnchorList.ensure_main(placement.product_id)) == 2
    diluted.refresh_from_db()
    typed.refresh_from_db()
    ambiguous.refresh_from_db()
    assert (diluted.keyword, diluted.anchor_type) == (
        keys["mp4 to mp3 converter"],
        AnchorType.DILUTED,
    )
    # Записанный вид не перетираем.
    assert (typed.keyword, typed.anchor_type) == (keys["mp4 to mp3"], AnchorType.GENERIC)
    assert ambiguous.keyword is None


# ---------- Доли ----------


def test_overview_counts_kinds_and_fallback(
    convertio: Product, placement: Placement, keys: dict[str, Keyword]
) -> None:
    brand = Keyword.objects.create(
        product=convertio,
        keyword="Convertio",
        target_url=HOME,
        page_type="Video",
        anchor_type=AnchorType.BRANDED,
    )
    PageTypeShare.objects.create(product=convertio, page_type="Video", target_pct=D(100))
    exact = keys["mp4 to mp3"]
    for anchor, keyword, kind in (
        ("mp4 to mp3", exact, AnchorType.EXACT),
        ("Convertio's mp4 to mp3", exact, AnchorType.DILUTED),
        ("mp4 to mp3", exact, None),  # старая ссылка: вид у ключа
        ("Convertio", brand, AnchorType.BRANDED),
    ):
        PlacementLink.objects.create(
            placement=placement,
            keyword=keyword,
            anchor=anchor,
            anchor_type=kind,
            target_url=MP3,
        )
    rejected = Placement.objects.create(
        site=placement.site, product=convertio, status=PlacementStatus.REJECTED
    )
    PlacementLink.objects.create(
        placement=rejected, keyword=exact, anchor="x", anchor_type="diluted", target_url=MP3
    )
    video = {t.page_type: t for t in overview(convertio.pk).types}["Video"]
    kinds = video.kinds
    assert (kinds.exact, kinds.diluted, kinds.naked, kinds.by_key) == (2, 1, 1, 1)
    assert kinds.pct("diluted") == D("25.0")


def _anchor(pk: int, links: int, kind: str = "exact", share: Decimal | None = None) -> Anchor:
    return Anchor(
        id=pk,
        keyword=f"k{pk}",
        target_url=HOME,
        page_type="Главная",
        anchor_type=kind,
        share=share,
        placed=links,
        waiting=0,
        position=None if kind != "exact" else 10,
        volume=1000,
        global_volume=None,
        tool=None,
    )


def test_recommend_treats_exact_and_diluted_as_key_links() -> None:
    anchors = [
        _anchor(1, 5),
        _anchor(2, 1, "branded", D(100)),
    ]
    # Прямых 40 и разбавленных 40 — ключевым 80%: 5 ключевых и 1 безанкор,
    # следующая — ключевая. Считай ключевым только прямые 40% — вышел бы безанкор.
    share = Share("Главная", D(100), exact=D(40), diluted=D(40), naked=D(20))
    assert recommend(anchors, [share], limit=1, skip_top=3)[0].anchor.id == 1
    # Ни прямые, ни разбавленные не заданы — как раньше: ключевым всё, кроме безанкорки.
    plain = Share("Главная", D(100), naked=D(20))
    assert recommend(anchors, [plain], limit=1, skip_top=3)[0].anchor.id == 1
    only_exact = Share("Главная", D(100), exact=D(40), naked=D(20))
    assert recommend(anchors, [only_exact], limit=1, skip_top=3)[0].anchor.id == 2
