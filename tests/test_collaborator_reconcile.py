"""Сделки Collaborator: разбор, сопоставление с размещением, сверка (E11-01, ADR-080).

Чистые функции — без базы: сделки и размещения собираются в тесте.
"""

from typing import Any

import pytest

from apps.placements.collaborator.deals import Deal, Link, Money, money, parse
from apps.placements.collaborator.matching import Facts, How, Miss, SiteIndex, card_id, match
from apps.placements.collaborator.reconcile import (
    Field,
    Groups,
    Verdict,
    closing_fills,
    compare_links,
    reconcile,
    summary,
)
from apps.sites.models import WorkStatus

CARD = "https://collaborator.pro/creator/article/view?id={}"
URL = "https://convertio.co/{}"


def deal(deal_id: int = 2834957, **fields: Any) -> Deal:
    values: dict[str, Any] = {"status": "Pending approval", "domain": "example.com", "card_id": 10}
    values.update(fields)
    return Deal(id=deal_id, **values)


def facts(pk: int = 1, **fields: Any) -> Facts:
    values: dict[str, Any] = {"site_id": 100, "status": WorkStatus.ORDERED, "collaborator": True}
    values.update(fields)
    return Facts(id=pk, **values)


SITES = SiteIndex({10: [(100, "example.com")], 20: [(200, "other.com")]}, {"example.com": 100})


def matched_one(item: Deal, *placements: Facts) -> Any:
    [result] = match([item], SITES, {100: list(placements)})
    return result


# --- Разбор сделки ---


def test_parse_api_item() -> None:
    item = {
        "id": 2834957,
        "project": {"id": 144728, "name": "Convertio"},
        "creator": {"id": 549884, "name": "contabeis.com.br", "url": CARD.format(549884)},
        "status": "Pending approval",
        "anchors": [{"anchor": "pdf em docx", "url": URL.format("pt/pdf-docx/")}],
        "created_at": "2026-10-26 22:46:25",
        "dateOfPlacement": None,
        "urlOfPublishedArticle": " https://www.contabeis.com.br/noticias/80646/ ",
        "price": "571.64 EUR",
        "pricePublication": "543.53 EUR",
        "priceSpelling": "28.11 EUR",
        "priceAnnouncement": None,
        "priceInsurance": None,
        "publicationType": "Article by the owner",
        "task": [],
    }

    parsed = parse(item, {"order": {"pair": ["1", "2"]}})

    assert parsed.id == 2834957
    assert parsed.project_id == 144728
    assert parsed.card_id == 549884
    assert parsed.domain == "contabeis.com.br"
    assert parsed.article_url == "https://www.contabeis.com.br/noticias/80646/"
    assert parsed.total == Money(57164, "EUR")
    assert parsed.parts == [("размещение", Money(54353, "EUR")), ("написание", Money(2811, "EUR"))]
    assert parsed.anchors == (Link("pdf em docx", URL.format("pt/pdf-docx/")),)
    assert parsed.placed_at == ""
    assert parsed.kept == {"order": {"pair": ["1", "2"]}}
    assert parsed.article_ready and not parsed.cancelled


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("571.64 EUR", Money(57164, "EUR")),
        ("93.39 eur", Money(9339, "EUR")),
        ("20 USD", Money(2000, "USD")),
        ("", None),
        (None, None),
        ("abc EUR", None),
        ("12.00", None),
    ],
)
def test_money(text: Any, expected: Money | None) -> None:
    assert money(text) == expected


def test_parse_without_id() -> None:
    with pytest.raises(ValueError):
        parse({"status": "Posted"})


def test_card_id() -> None:
    assert card_id(CARD.format(630296)) == 630296
    assert card_id("https://collaborator.pro/creator/article/view?x=1&id=7") == 7
    assert card_id("") is None


# --- Площадка ---


def test_site_by_card_then_domain() -> None:
    index = SiteIndex(
        {10: [(100, "example.com")], 30: [(300, "diena.lt"), (301, "vilniaus.diena.lt")]},
        {"example.com": 100, "fallback.com": 400, "diena.lt": 300},
    )

    # Карточка главнее домена: домен на Collaborator записан иначе.
    assert index.find(deal(card_id=10, domain="www.Example.com")) == 100
    assert index.find(deal(card_id=10, domain="renamed.com")) == 100
    # Одна карточка у двух площадок — та, у которой тот же домен.
    assert index.find(deal(card_id=30, domain="vilniaus.diena.lt")) == 301
    # Карточки в базе нет — по домену.
    assert index.find(deal(card_id=99, domain="fallback.com")) == 400
    assert index.find(deal(card_id=None, card_url=CARD.format(10), domain="")) == 100
    assert index.find(deal(card_id=99, domain="unknown.com")) is None


# --- Сопоставление ---


def test_by_order_number_first() -> None:
    by_url = facts(1, article_url="https://example.com/a")
    by_order = facts(2, order_id=" 2834957 ")

    result = matched_one(deal(article_url="https://example.com/a"), by_url, by_order)

    assert result.placement == by_order
    assert result.how == How.ORDER


@pytest.mark.parametrize(
    ("system", "collaborator"),
    [
        ("https://example.com/post/", "http://www.example.com/post"),
        ("https://example.com/post?srsltid=AfmBOoq", "https://example.com/post"),
        # Статья на поддомене площадки: example.com, статья на blog.example.com.
        ("https://blog.example.com/post", "https://blog.example.com/post/"),
    ],
)
def test_by_article_url(system: str, collaborator: str) -> None:
    placement = facts(1, article_url=system, collaborator=False)

    result = matched_one(deal(article_url=collaborator), facts(2), placement)

    assert result.placement == placement
    assert result.how == How.ARTICLE


def test_single_collaborator_placement() -> None:
    only = facts(1)

    result = matched_one(deal(), only)

    assert (result.placement, result.how) == (only, How.SINGLE)


def test_single_needs_collaborator_seller() -> None:
    result = matched_one(deal(), facts(1, collaborator=False))

    assert result.placement is None
    assert (result.miss, result.candidates) == (Miss.OTHER_SELLER, (1,))


def test_two_placements_on_site_repeat() -> None:
    """Повторные размещения (ADR-073): без номера и адреса — какое, неясно."""
    first, second = facts(1, status=WorkStatus.PLACED), facts(2)
    with_number = deal(2, card_id=10)

    results = match([with_number, deal(3)], SITES, {100: [first, facts(2, order_id="2")]})

    assert results[0].placement is not None and results[0].placement.id == 2
    assert results[1].placement == first and results[1].how == How.SINGLE
    many = matched_one(deal(4), first, second)
    assert (many.miss, many.candidates) == (Miss.MANY, (1, 2))


def test_one_placement_two_deals() -> None:
    """talkandroid.com: две сделки, одно размещение — у той, чей номер в нём."""
    placement = facts(1, order_id="2820366", status=WorkStatus.PLACED)

    results = match([deal(2816031), deal(2820366)], SITES, {100: [placement]})

    assert results[0].placement is None
    assert (results[0].miss, results[0].candidates) == (Miss.TAKEN, (1,))
    assert results[1].placement == placement and results[1].how == How.ORDER


def test_one_free_placement_two_deals_is_shared() -> None:
    results = match([deal(1), deal(2)], SITES, {100: [facts(7)]})

    assert [(r.miss, r.candidates) for r in results] == [(Miss.SHARED, (7,))] * 2


def test_live_deal_goes_before_cancelled() -> None:
    cancelled = deal(1, status="Rejected by the owner")
    live = deal(2, status="In progress")

    results = match([cancelled, live], SITES, {100: [facts(7)]})

    assert results[0].placement is None
    assert results[1].placement is not None and results[1].how == How.SINGLE


def test_no_site_and_no_placement() -> None:
    results = match([deal(1, card_id=99, domain="nowhere.com"), deal(2)], SITES, {})

    assert [r.miss for r in results] == [Miss.NO_SITE, Miss.NO_PLACEMENT]


def test_placement_goes_to_one_deal_only() -> None:
    """Один адрес у двух сделок — размещение достаётся первой, второй — нет."""
    placement = facts(1, article_url="https://example.com/a")
    same = {"article_url": "https://example.com/a"}

    results = match([deal(1, **same), deal(2, **same)], SITES, {100: [placement]})

    assert results[0].placement == placement
    assert results[1].placement is None and results[1].miss == Miss.TAKEN


# --- Анкоры ---


def links(*pairs: tuple[str, str]) -> tuple[Link, ...]:
    return tuple(Link(text, URL.format(path)) for text, path in pairs)


def verdicts(system: tuple[Link, ...], collaborator: tuple[Link, ...]) -> list[Verdict]:
    return [check.verdict for check in compare_links(system, collaborator)]


def test_links_same_with_case_spaces_and_slash() -> None:
    system = (Link("PDF  em DOCX", "https://convertio.co/pt/pdf-docx"),)
    collaborator = (Link("pdf em docx", "https://convertio.co/pt/pdf-docx/"),)

    assert verdicts(system, collaborator) == [Verdict.OK]


def test_links_text_url_extra_missing() -> None:
    system = links(("Converito", "pt/"), ("mkv to mp4", ""), ("wav to mp3", "wav-mp3/"))
    collaborator = links(("Convertio", "pt/"), ("mkv to mp4", "mkv-mp4/"), ("Read more", "x/"))

    checks = compare_links(system, collaborator)

    assert [c.verdict for c in checks] == [
        Verdict.TEXT,
        Verdict.URL,
        Verdict.MISSING,
        Verdict.EXTRA,
    ]
    assert checks[0].system == system[0] and checks[0].collaborator == collaborator[0]
    assert checks[3].system == system[2] and checks[3].collaborator is None


def test_links_bare_domain_text_is_different() -> None:
    """«convertio.co» и «https://convertio.co» — разные тексты: это расхождение."""
    system = links(("https://convertio.co", ""))
    collaborator = links(("convertio.co", ""))

    assert verdicts(system, collaborator) == [Verdict.TEXT]


def test_three_links_any_order() -> None:
    system = links(("c", "c/"), ("a", "a/"), ("b", "b/"))
    collaborator = links(("a", "a/"), ("b", "b/"), ("c", "c/"))

    assert verdicts(system, collaborator) == [Verdict.OK] * 3


# --- Сверка ---


def ready_deal(deal_id: int = 2834957, **fields: Any) -> Deal:
    values: dict[str, Any] = {
        "article_url": "https://example.com/post",
        "total": Money(57164, "EUR"),
        "anchors": links(("pdf em docx", "pt/pdf-docx/")),
    }
    values.update(fields)
    return deal(deal_id, **values)


def row_of(item: Deal, placement: Facts) -> Any:
    return reconcile(matched_one(item, placement))


def test_ready_and_fills_for_contabeis() -> None:
    row = row_of(ready_deal(), facts(1, links=links(("pdf em docx", "pt/pdf-docx/"))))

    assert row.ready
    # «Заплачено» в списке — только у «Размещено» (пользователь, 09.11.2026).
    assert [(f.field, f.value) for f in row.fills] == [
        (Field.ORDER, "2834957"),
        (Field.ARTICLE, "https://example.com/post"),
    ]
    # Закрывая в панели, «Заполнить из Collaborator» ставит и его.
    assert [(f.field, f.value) for f in closing_fills(row)] == [
        (Field.ORDER, "2834957"),
        (Field.PAID, Money(57164, "EUR")),
        (Field.ARTICLE, "https://example.com/post"),
    ]
    assert row.differs == ()
    assert [c.verdict for c in row.links] == [Verdict.OK]


@pytest.mark.parametrize(
    ("placement_status", "listed"),
    [
        (WorkStatus.IN_WORK, False),
        (WorkStatus.ORDERED, False),
        (WorkStatus.WRITING, False),
        (WorkStatus.PLACED, True),
    ],
)
def test_paid_only_for_placed(placement_status: str, listed: bool) -> None:
    item = ready_deal(status="Posted")

    empty = row_of(item, facts(1, status=placement_status))
    other = row_of(item, facts(1, status=placement_status, paid=Money(100, "EUR")))

    assert (Field.PAID in {f.field for f in empty.fills}) is listed
    assert (other.differ_of(Field.PAID) is not None) is listed


@pytest.mark.parametrize(
    ("status", "article", "placement_status", "ready"),
    [
        ("Pending approval", "https://example.com/p", WorkStatus.ORDERED, True),
        ("Pending approval", "https://example.com/p", WorkStatus.WRITING, True),
        ("Pending approval", "https://example.com/p", WorkStatus.IN_WORK, False),
        ("Pending approval", "https://example.com/p", WorkStatus.PLACED, False),
        ("Pending approval", "", WorkStatus.ORDERED, False),
        ("In improvement", "https://example.com/p", WorkStatus.ORDERED, False),
    ],
)
def test_ready_to_close(status: str, article: str, placement_status: str, ready: bool) -> None:
    row = row_of(deal(status=status, article_url=article), facts(1, status=placement_status))

    assert row.ready is ready


def test_paid_and_announce_only_with_ready_article() -> None:
    priced: dict[str, Any] = {"total": Money(41155, "EUR"), "announce": Money(5893, "EUR")}

    in_progress = row_of(deal(status="In progress", **priced), facts(1))
    posted = row_of(deal(status="Posted", **priced), facts(1, status=WorkStatus.PLACED))

    assert {f.field for f in in_progress.fills} == {Field.ORDER}
    assert {f.field: f.value for f in posted.fills} == {
        Field.ORDER: "2834957",
        Field.PAID: Money(41155, "EUR"),
        Field.ANNOUNCE: True,
    }


def test_paid_from_invoice_untouched() -> None:
    item = ready_deal()

    placed = {"status": WorkStatus.PLACED, "from_invoice": True}
    empty = row_of(item, facts(1, **placed))
    other = row_of(item, facts(1, paid=Money(50000, "EUR"), **placed))

    assert Field.PAID not in {f.field for f in empty.fills}
    assert Field.PAID not in {f.field for f in closing_fills(empty)}
    [paid] = [d for d in other.differs if d.field == Field.PAID]
    assert paid.takeable is False


def test_differs() -> None:
    item = deal(
        status="Posted",
        article_url="https://designcrowd.com/article/1711",
        total=Money(24536, "EUR"),
        announce=Money(4677, "EUR"),
        anchors=links(("mp4 to mp3", "mp4-mp3/")),
    )
    placement = facts(
        1,
        status=WorkStatus.ORDERED,
        order_id="945158",
        article_url="https://blog.designcrowd.com/article/1711",
        paid=Money(0, "EUR"),
        announce=False,
        links=links(("mp4 to mp3 converter", "mp4-mp3/")),
    )

    row = row_of(item, placement)

    assert row.fills == ()
    assert [(d.field, d.system, d.collaborator) for d in row.differs] == [
        (Field.ORDER, "945158", "2834957"),
        (Field.ARTICLE, placement.article_url, item.article_url),
        (Field.ANNOUNCE, False, True),
        (
            Field.ANCHORS,
            [["mp4 to mp3 converter", URL.format("mp4-mp3/")]],
            [["mp4 to mp3", URL.format("mp4-mp3/")]],
        ),
        (Field.STATUS, WorkStatus.ORDERED, "Posted"),
    ]
    status = row.differ_of(Field.STATUS)
    assert status is not None
    assert status.takeable is False and status.target_status == WorkStatus.PLACED
    assert all(d.takeable for d in row.differs if d.field != Field.STATUS)


def test_same_values_written_differently_do_not_differ() -> None:
    item = ready_deal(status="Posted", article_url="https://www.example.com/post/")
    placement = facts(
        1,
        status=WorkStatus.PLACED,
        order_id="2834957",
        article_url="https://example.com/post?utm_source=x",
        paid=Money(57164, "EUR"),
        links=links(("PDF em docx ", "pt/pdf-docx")),
    )

    row = row_of(item, placement)

    assert (row.fills, row.differs, row.ready) == ((), (), False)


@pytest.mark.parametrize(
    ("placement_status", "differs"),
    [
        (WorkStatus.ORDERED, True),
        (WorkStatus.PLACED, True),
        (WorkStatus.REJECTED, False),
        (WorkStatus.DISCARDED, False),
    ],
)
def test_cancelled_deal_only_status(placement_status: str, differs: bool) -> None:
    item = ready_deal(status="Withdrawn")

    row = row_of(item, facts(1, status=placement_status))

    assert row.fills == ()
    if differs:
        [status] = row.differs
        assert status.field == Field.STATUS and status.target_status == WorkStatus.REJECTED
    else:
        assert row.differs == ()


def test_kept_while_pair_is_the_same() -> None:
    kept = {"order": {"pair": ["945158", "2834957"], "by": 1, "at": "2026-11-08"}}
    placement = facts(1, order_id="945158")

    same = row_of(deal(kept=kept), placement)
    changed = row_of(deal(kept=kept), facts(1, order_id="945159"))

    assert same.open_differs == [] and [d.field for d in same.kept_differs] == [Field.ORDER]
    assert [d.field for d in changed.open_differs] == [Field.ORDER]


def test_groups_and_summary() -> None:
    placements = {
        100: [facts(1, links=links(("pdf em docx", "pt/pdf-docx/")))],
        200: [facts(2, site_id=200, order_id="9", status=WorkStatus.PLACED)],
    }
    deals = [
        ready_deal(1),
        deal(2, card_id=20, domain="other.com", status="Posted"),
        deal(3, card_id=99, domain="nowhere.com"),
        deal(4, card_id=99, domain="nowhere.com", status="Rejected"),
    ]

    groups = Groups.of(match(deals, SITES, placements))

    assert [row.deal.id for row in groups.ready] == [1]
    assert [row.deal.id for row in groups.fill] == [1]
    assert [row.deal.id for row in groups.differ] == [2]
    # Отменённая сделка без размещения — не «нет в системе»: заводить нечего.
    assert [m.deal.id for m in groups.missing] == [3]
    assert groups.fill_counts() == {Field.ORDER: 1, Field.ARTICLE: 1}
    assert groups.placements("ready") == {1}
    assert summary(4, groups) == (
        "4 сделки: 1 готова к закрытию, 1 можно заполнить, 1 расходится, 1 нет в системе"
    )
