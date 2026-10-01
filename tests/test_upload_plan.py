"""Правило разбора загрузки: вкладка, нужно ли решение, станет ли цена рабочей (ADR-044).

Чистая функция `classify` — без базы. Как правило работает на записи —
`test_upload_write.py`.
"""

import pytest

from apps.sites.models import PlacementType, ReviewGroup
from apps.sites.uploads.plan import Decision, OfferState, SiteState, classify

COLLABORATOR = 1
LINKHUB = 2
GP = PlacementType.GUEST_POST
LI = PlacementType.LINK_INSERTION


def _offer(
    seller: int = COLLABORATOR,
    cents: int = 20000,
    *,
    service: str = GP,
    currency: str = "EUR",
    eur: int | None = None,
    reviewed: bool = True,
    pk: int = 10,
) -> OfferState:
    return OfferState(pk, seller, service, cents, currency, reviewed, cents if eur is None else eur)


def _site(working: OfferState | None = None, **kwargs: object) -> SiteState:
    state = SiteState(1, "a.com", working=working)
    for name, value in kwargs.items():
        setattr(state, name, value)
    return state


def _classify(
    site: SiteState | None,
    *,
    seller: int = COLLABORATOR,
    cents: int = 20000,
    eur: int | None = None,
    service: str = GP,
    currency: str = "EUR",
    has_gp: bool = True,
) -> Decision:
    return classify(
        service=service,
        cents=cents,
        currency=currency,
        eur_cents=cents if eur is None else eur,
        seller_id=seller,
        site=site,
        record_has_gp=has_gp,
        recheck_pct=3,
    )


class TestFirstPrice:
    def test_new_site_publication_becomes_working(self) -> None:
        assert _classify(None) == Decision(ReviewGroup.NEW, False, True)

    def test_new_site_insertion_waits_for_publication(self) -> None:
        assert _classify(None, service=LI) == Decision(ReviewGroup.NEW, False, False)

    def test_new_site_without_publication_insertion_becomes_working(self) -> None:
        assert _classify(None, service=LI, has_gp=False) == Decision(ReviewGroup.NEW, False, True)

    def test_known_site_without_working_price(self) -> None:
        assert _classify(_site()) == Decision(ReviewGroup.NEW, False, True)

    def test_rejected_site_without_working_price_still_gets_it(self) -> None:
        site = _site(rejected=["Convertio: Nofollow, отбрасываем"])
        assert _classify(site) == Decision(ReviewGroup.REJECTED, False, True)


class TestSameSeller:
    """Тот же продавец, та же услуга — рабочая цена идёт за ним сама."""

    def test_rate_drift_follows_without_decision(self) -> None:
        # €46.38 → €47.35: гривна подешевела — у ~1400 площадок за несколько дней.
        site = _site(_offer(cents=4638))
        assert _classify(site, cents=4735) == Decision(ReviewGroup.CHANGED, False, True)

    def test_big_change_follows_too(self) -> None:
        site = _site(_offer(cents=20000))
        assert _classify(site, cents=24000) == Decision(ReviewGroup.CHANGED, False, True)

    def test_same_price_moves_to_fresh_snapshot(self) -> None:
        assert _classify(_site(_offer())) == Decision(ReviewGroup.SAME, False, True)

    def test_order_in_work_freezes_the_price(self) -> None:
        site = _site(_offer(cents=20000), frozen=True)
        assert _classify(site, cents=24000) == Decision(ReviewGroup.CHANGED, True, False)
        assert _classify(site) == Decision(ReviewGroup.SAME, False, False)

    def test_same_seller_other_currency_is_a_change(self) -> None:
        site = _site(_offer(cents=20000, currency="USD", eur=17600))
        assert _classify(site, cents=20000, eur=20000).group == ReviewGroup.CHANGED


class TestOtherSeller:
    def test_cheaper_waits_for_decision(self) -> None:
        site = _site(_offer())
        decision = _classify(site, seller=LINKHUB, cents=21000, currency="USD", eur=18500)
        assert decision == Decision(ReviewGroup.CHEAPER, True, False)

    def test_pricier_waits_for_decision(self) -> None:
        site = _site(_offer())
        assert _classify(site, seller=LINKHUB, eur=25000) == Decision(
            ReviewGroup.PRICIER, True, False
        )

    def test_equal_in_euro_needs_nothing(self) -> None:
        assert _classify(_site(_offer()), seller=LINKHUB) == Decision(
            ReviewGroup.SAME, False, False
        )

    def test_other_service_waits_for_decision(self) -> None:
        site = _site(_offer())
        assert _classify(site, seller=LINKHUB, service=LI, cents=12000) == Decision(
            ReviewGroup.OTHER_SERVICE, True, False
        )

    def test_rejected_site_goes_to_rejected_tab(self) -> None:
        site = _site(_offer(), rejected=["Convertio: Не тематика, отбрасываем"])
        assert _classify(site, seller=LINKHUB, eur=15000) == Decision(
            ReviewGroup.REJECTED, True, False
        )


class TestRecheck:
    """Уже оставленное предложение снова ждёт решения, только если цена сдвинулась больше 3%."""

    @pytest.mark.parametrize(("new", "needs"), [(10300, False), (9700, False), (10301, True)])
    def test_threshold(self, new: int, needs: bool) -> None:
        prev = _offer(LINKHUB, 10000, pk=11)
        site = _site(_offer(cents=20000), prev={GP: prev})
        decision = _classify(site, seller=LINKHUB, cents=new)
        assert decision.group == ReviewGroup.CHEAPER
        assert decision.needs_decision is needs

    def test_pending_previous_offer_still_needs_decision(self) -> None:
        prev = _offer(LINKHUB, 10000, reviewed=False, pk=11)
        site = _site(_offer(cents=20000), prev={GP: prev})
        assert _classify(site, seller=LINKHUB, cents=10000).needs_decision

    def test_other_currency_is_not_compared(self) -> None:
        prev = _offer(LINKHUB, 10000, currency="USD", pk=11)
        site = _site(_offer(cents=20000), prev={GP: prev})
        assert _classify(site, seller=LINKHUB, cents=10000, currency="EUR").needs_decision
