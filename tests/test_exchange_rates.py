"""Курсы ЕЦБ: клиент, запись, задача, расписание (E1-07, ADR-043).

В сеть тесты не ходят: у клиента подменён транспорт httpx.
"""

import datetime as dt
from collections.abc import Callable
from decimal import Decimal

import httpx
import pytest
from celery.schedules import crontab
from django.conf import settings
from django.core.management import call_command

from apps.integrations import ecb
from apps.sites.models import ExchangeRate
from apps.sites.rates import save_rates
from apps.sites.tasks import exchange_rates_update

CSV = (
    "KEY,FREQ,CURRENCY,CURRENCY_DENOM,EXR_TYPE,EXR_SUFFIX,TIME_PERIOD,OBS_VALUE,OBS_STATUS\n"
    "EXR.D.USD.EUR.SP00.A,D,USD,EUR,SP00,A,2026-09-29,1.1290,A\n"
    "EXR.D.USD.EUR.SP00.A,D,USD,EUR,SP00,A,2026-09-30,1.1355,A\n"
    "EXR.D.GBP.EUR.SP00.A,D,GBP,EUR,SP00,A,2026-09-30,0.85463,A\n"
    "EXR.D.ISK.EUR.SP00.A,D,ISK,EUR,SP00,A,2026-09-30,,A\n"
)


def _transport(text: str = CSV, status: int = 200) -> httpx.MockTransport:
    return httpx.MockTransport(lambda request: httpx.Response(status, text=text))


def _patch_fetch(monkeypatch: pytest.MonkeyPatch, text: str = CSV) -> None:
    real: Callable[..., list[ecb.Rate]] = ecb.fetch_rates
    monkeypatch.setattr(ecb, "fetch_rates", lambda: real(transport=_transport(text)))


class TestClient:
    def test_parses_rates_and_skips_empty(self) -> None:
        rates = ecb.fetch_rates(transport=_transport())
        assert ecb.Rate("USD", dt.date(2026, 9, 30), Decimal("1.1355")) in rates
        assert {rate.currency for rate in rates} == {"USD", "GBP"}

    def test_asks_last_observations_in_csv(self) -> None:
        sent: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            sent.append(request)
            return httpx.Response(200, text=CSV)

        ecb.fetch_rates(transport=httpx.MockTransport(handler))
        assert sent[0].url.params["format"] == "csvdata"
        assert sent[0].url.params["lastNObservations"] == str(ecb.LAST_OBSERVATIONS)

    def test_error_status_raises(self) -> None:
        with pytest.raises(ecb.EcbError, match="503"):
            ecb.fetch_rates(transport=_transport("down", status=503))

    def test_empty_answer_raises(self) -> None:
        with pytest.raises(ecb.EcbError, match="ни одного"):
            ecb.fetch_rates(transport=_transport("KEY,CURRENCY,TIME_PERIOD,OBS_VALUE\n"))


@pytest.mark.django_db
class TestSaving:
    def test_repeat_adds_nothing(self) -> None:
        rates = ecb.parse_csv(CSV)
        assert save_rates(rates) == 3
        assert save_rates(rates) == 0
        assert ExchangeRate.objects.count() == 3

    def test_task_writes_new_rates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_fetch(monkeypatch)
        assert exchange_rates_update.delay().get() == 3
        latest = ExchangeRate.objects.filter(currency="USD").order_by("-rate_date").first()
        assert latest is not None and latest.rate == Decimal("1.1355")

    def test_command(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _patch_fetch(monkeypatch)
        call_command("exchange_rates")
        out = capsys.readouterr().out
        assert "Новых курсов: 3" in out
        assert "1 € = 1.135500 USD на 30.09.2026" in out


def test_rates_update_daily_in_the_evening() -> None:
    """Критерий приёмки: курс обновляется раз в день — после публикации ЕЦБ."""
    entry = settings.CELERY_BEAT_SCHEDULE["exchange_rates"]
    assert entry["task"] == "exchange_rates_update"
    assert entry["schedule"] == crontab(hour=18, minute=0)
