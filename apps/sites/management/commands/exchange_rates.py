"""Курсы ЕЦБ вручную: первое заполнение и проверка (E1-07, ADR-043).

    python manage.py exchange_rates

Тот же путь, что у задачи `exchange_rates_update`, только сразу, без очереди.
"""

from typing import Any

from django.core.management.base import BaseCommand, CommandError

from apps.integrations import ecb
from apps.sites.models import ExchangeRate
from apps.sites.rates import save_rates
from config.run_id import bind_run_id, new_run_id

SHOWN = ("USD", "GBP")


class Command(BaseCommand):
    help = "Загрузить курсы ЕЦБ к евро и показать последние по доллару и фунту."

    def handle(self, *args: Any, **options: Any) -> None:
        with bind_run_id(new_run_id()):
            try:
                saved = save_rates(ecb.fetch_rates())
            except ecb.EcbError as error:
                raise CommandError(str(error)) from error
        self.stdout.write(f"Новых курсов: {saved}")
        for currency in SHOWN:
            rate = ExchangeRate.objects.filter(currency=currency).order_by("-rate_date").first()
            if rate is not None:
                self.stdout.write(f"1 € = {rate.rate} {currency} на {rate.rate_date:%d.%m.%Y}")
