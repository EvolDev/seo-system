"""Анкоры продукта (E3-05, ADR-059): доли «цель / факт» и рекомендации."""

from decimal import Decimal
from typing import Any

import pytest

from apps.keywords.anchors import Anchor, Share, overview, pct_text, recommend
from apps.keywords.models import AnchorType, Keyword, PageTypeShare
from apps.placements.models import Placement, PlacementLink, PlacementStatus
from apps.sites.models import Product, Site

D = Decimal


def _anchor(pk: int, page_type: str | None, links: int = 0, **fields: Any) -> Anchor:
    data: dict[str, Any] = {
        "id": pk,
        "keyword": f"k{pk}",
        "target_url": "https://convertio.co/",
        "page_type": page_type,
        "anchor_type": "exact",
        "share": None,
        "placed": links,
        "waiting": 0,
        "position": 10,
        "volume": 1000,
        "global_volume": None,
        "tool": None,
    }
    data.update(fields)
    return Anchor(**data)


SHARES = [Share("Video", D(25)), Share("Audio", D(25)), Share("Главная", D(50))]


def test_neediest_type_first() -> None:
    anchors = [
        _anchor(1, "Video", 5),
        _anchor(2, "Audio", 0),
        _anchor(3, "Главная", 5),
        _anchor(4, "Audio", 0),
    ]
    picks = recommend(anchors, SHARES, limit=1, skip_top=3)
    assert [p.anchor.id for p in picks] == [2]
    assert picks[0].reason == "Audio: цель 25%, сейчас 0%"


def test_list_moves_shares_to_target() -> None:
    # Audio без ссылок, остальные — по цели: Audio первым, потом поровну.
    anchors = [
        _anchor(i, t, n)
        for i, (t, n) in enumerate(
            [
                ("Video", 1),
                ("Audio", 0),
                ("Главная", 2),
                ("Audio", 0),
                ("Video", 0),
                ("Главная", 0),
            ],
            start=1,
        )
    ]
    picks = recommend(anchors, SHARES, limit=4, skip_top=3)
    assert [p.anchor.page_type for p in picks] == ["Audio", "Главная", "Video", "Audio"]
    assert len({p.anchor.id for p in picks}) == 4


def test_fewest_links_then_volume_inside_type() -> None:
    anchors = [
        _anchor(1, "Audio", 2, volume=9000),
        _anchor(2, "Audio", 0, volume=100),
        _anchor(3, "Audio", 0, volume=5000),
    ]
    picks = recommend(anchors, [Share("Audio", D(100))], limit=3, skip_top=3)
    assert [p.anchor.id for p in picks] == [3, 2, 1]


def test_top_positions_and_excluded_skipped() -> None:
    anchors = [
        _anchor(1, "Audio", 0, position=2),
        _anchor(2, "Audio", 0, position=None),
        _anchor(3, "Audio", 0),
        _anchor(4, "Audio", 0, position=4),
    ]
    picks = recommend(anchors, [Share("Audio", D(100))], limit=10, skip_top=3, exclude=[3])
    assert {p.anchor.id for p in picks} == {2, 4}


def test_naked_by_split_inside_type() -> None:
    share = Share("Главная", D(100), exact=D(50), naked=D(50))
    anchors = [
        _anchor(1, "Главная", 4),
        _anchor(2, "Главная", 0, anchor_type="branded", share=D(80), position=None),
        _anchor(3, "Главная", 0, anchor_type="url", share=D(20), position=1),
    ]
    picks = recommend(anchors, [share], limit=3, skip_top=3)
    # Безанкора нет, а нужна половина — бренд (доля больше), потом адрес; позиция ему не важна.
    assert [p.anchor.id for p in picks][:2] == [2, 3]


def test_without_shares_fewest_links() -> None:
    anchors = [_anchor(1, None, 3), _anchor(2, None, 0, volume=10), _anchor(3, None, 0, volume=99)]
    picks = recommend(anchors, [], limit=2, skip_top=3)
    assert [p.anchor.id for p in picks] == [3, 2]
    assert picks[0].reason == "ссылок нет"


def test_type_without_candidates_skipped() -> None:
    anchors = [_anchor(1, "Video", 0), _anchor(2, "Audio", 0, position=1)]
    picks = recommend(anchors, SHARES, limit=5, skip_top=3)
    assert [p.anchor.id for p in picks] == [1]


@pytest.mark.parametrize(
    ("value", "text"), [(D("15.00"), "15%"), (D("15.80"), "15,8%"), (None, "—"), (D("0"), "0%")]
)
def test_pct_text(value: Decimal | None, text: str) -> None:
    assert pct_text(value) == text


# ---------- Сводка по базе ----------


@pytest.mark.django_db
def test_overview_counts_and_shares() -> None:
    product = Product.objects.create(name="Convertio", domain="convertio.co")
    video = Keyword.objects.create(
        product=product,
        keyword="mp4 converter",
        target_url="https://convertio.co/mp4/",
        page_type="Video",
        anchor_type=AnchorType.EXACT,
    )
    audio = Keyword.objects.create(
        product=product,
        keyword="mp3 converter",
        target_url="https://convertio.co/mp3/",
        page_type="Audio",
        anchor_type=AnchorType.EXACT,
    )
    PageTypeShare.objects.create(product=product, page_type="Video", target_pct=D(25), position=1)
    PageTypeShare.objects.create(product=product, page_type="Audio", target_pct=D(75), position=2)
    statuses = [PlacementStatus.PLACED, PlacementStatus.ORDERED, PlacementStatus.PLACED]
    for number, status in enumerate(statuses):
        placement = Placement.objects.create(
            site=Site.objects.create(domain=f"s{number}.com"), product=product, status=status
        )
        PlacementLink.objects.create(
            placement=placement, keyword=video, anchor=video.keyword, target_url=video.target_url
        )
    PlacementLink.objects.create(
        placement=placement, anchor="click here", target_url="https://convertio.co/"
    )
    data = overview(product.pk)
    assert (data.placed, data.waiting, data.loose_placed) == (3, 1, 1)
    by_type = {t.page_type: t for t in data.types}
    assert [t.page_type for t in data.types] == ["Video", "Audio"]
    assert by_type["Video"].total_pct == D("100.0")
    assert by_type["Audio"].gap == D("75")
    picks = recommend(data.anchors, data.shares, limit=1, skip_top=3)
    assert picks[0].anchor.id == audio.pk
