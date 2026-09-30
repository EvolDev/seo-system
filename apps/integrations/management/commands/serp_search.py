"""Поиск по выдаче Google вручную: проверить ключ, ответ провайдера и расход (E2-02).

    python manage.py serp_search "site:convertio.co"
    python manage.py serp_search "site:example.com/blog/article" --country gb --depth 20

Идёт тем же путём, что задачи: кеш, дневной лимит, строка в `api_usage`.
Повтор того же запроса в течение `SERP_CACHE_HOURS` — из кеша, бесплатно.
"""

from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError, CommandParser

from apps.integrations.serp import SerpBudgetExceeded, SerpError, search, spent_today
from apps.observability.models import ApiUsage
from config.run_id import bind_run_id, new_run_id


class Command(BaseCommand):
    help = "Запрос к выдаче Google через SERP API: результаты, стоимость, расход за сегодня."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("query", help="текст запроса, например site:example.com")
        parser.add_argument("--depth", type=int, default=10, help="сколько результатов (1–100)")
        parser.add_argument("--country", default="us", help="страна, код из двух букв")

    def handle(self, *args: Any, query: str, depth: int, country: str, **options: Any) -> None:
        run_id = new_run_id()
        with bind_run_id(run_id):
            try:
                page = search(query, depth=depth, country=country)
            except (SerpError, SerpBudgetExceeded, ValueError) as error:
                raise CommandError(str(error)) from error

        paid = ApiUsage.objects.filter(run_id=run_id).first()
        self.stdout.write(f"run_id: {run_id} · провайдер: {settings.SERP_PROVIDER}")
        self.stdout.write(f"«{page.query}» · страна {page.country} · глубина {page.depth}")
        for result in page.results:
            self.stdout.write(f"{result.position:>3}. {result.url}")
            if result.title:
                self.stdout.write(f"     {result.title}")
        if not page.results:
            self.stdout.write("Результатов нет.")
        estimate = "нет в ответе" if page.total_estimate is None else f"{page.total_estimate}"
        self.stdout.write(f"Оценка Google «примерно N»: {estimate}")
        if paid is None:
            self.stdout.write("Из кеша, бесплатно.")
        else:
            self.stdout.write(f"Стоимость: {paid.cost_cents} ¢ · кредитов: {paid.units}")
        self.stdout.write(
            f"Потрачено сегодня: {spent_today()} ¢ из {settings.SERP_DAILY_BUDGET_CENTS} ¢"
        )
