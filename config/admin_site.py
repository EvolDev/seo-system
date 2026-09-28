"""Сайт админки: меню по работе человека, а не по приложениям Django (E9-08).

Штатно Django строит меню из приложений: «Площадки» — все таблицы блока 1
вперемешку, рабочий экран рядом со снапшотами. Здесь пункты собраны в группы
по тому, что человек делает: работает со списком, заглядывает в справочник,
меняет настройки. Служебные таблицы — отдельной группой, свёрнутой по
умолчанию (config/templates/admin/base_site.html).

Подключение — штатное: `SeoAdminConfig` стоит в INSTALLED_APPS вместо
django.contrib.admin, и `admin.site` во всём проекте — `SeoAdminSite`.
"""

from typing import Any

from django.contrib import admin
from django.contrib.admin.apps import AdminConfig
from django.db.models import Count, Q
from django.http import HttpRequest
from django.template.response import TemplateResponse

# Группы меню: (название, код группы, [(приложение, модель)]). Порядок — как в меню.
MENU: list[tuple[str, str, list[tuple[str, str]]]] = [
    (
        "Работа",
        "work",
        [("sites", "productsitelatest"), ("placements", "placement"), ("keywords", "keyword")],
    ),
    ("Справочники", "reference", [("sites", "site"), ("sites", "sitelist"), ("sites", "product")]),
    ("Настройки", "settings", [("content", "domainsetting"), ("auth", "user"), ("auth", "group")]),
    (
        "Служебное",
        "service",
        [
            ("sites", "productsite"),
            ("sites", "sitemetric"),
            ("sites", "siteprice"),
            ("sites", "grayscan"),
            ("sites", "siteaudit"),
            ("keywords", "keywordposition"),
            ("observability", "taskrun"),
        ],
    ),
]
WORK = "work"
SERVICE = "service"
# Не в меню: строки рабочих списков открываются из списка («Площадки списка»),
# тема оформления меняется расцветками, а не таблицей.
HIDDEN = {("sites", "sitelistitem"), ("admin_interface", "theme")}


class SeoAdminSite(admin.AdminSite):
    site_header = site_title = "SEO-система"
    index_title = "Главная"
    # «Открыть сайт» в шапке: публичного сайта у системы нет.
    site_url = None

    def get_app_list(
        self, request: HttpRequest, app_label: str | None = None
    ) -> list[dict[str, Any]]:
        apps = super().get_app_list(request, app_label)
        if app_label is not None:
            # Страница одного приложения (/admin/sites/) — штатная.
            return apps
        models = {
            (app["app_label"], model["object_name"].lower()): model
            for app in apps
            for model in app["models"]
        }
        groups: list[dict[str, Any]] = []
        placed = set(HIDDEN)
        for name, code, keys in MENU:
            placed.update(keys)
            group_models = [models[key] for key in keys if key in models]
            groups.append({"name": name, "app_label": code, "models": group_models})
        # Зарегистрированное позже и не попавшее в MENU — в «Служебное», чтобы
        # новая таблица не пропала из меню молча.
        rest = [model for key, model in models.items() if key not in placed]
        groups[-1]["models"].extend(sorted(rest, key=lambda model: str(model["name"])))
        result = []
        for group in groups:
            if not group["models"]:
                continue
            # Заголовок группы ведёт на её первый экран: своей страницы у группы нет.
            first = group["models"][0]
            group["app_url"] = first.get("admin_url") or first.get("view_only_url") or ""
            group["has_module_perms"] = True
            result.append(group)
        return result

    def index(
        self, request: HttpRequest, extra_context: dict[str, Any] | None = None
    ) -> TemplateResponse:
        # Главная — вход в работу, а не список всех таблиц: карточки экранов
        # группы «Работа» с парой чисел, ниже — остальные группы меню.
        groups = self.get_app_list(request)
        work = next((group for group in groups if group["app_label"] == WORK), None)
        context = {
            "home_cards": _home_cards(work["models"]) if work else [],
            "home_app_list": [group for group in groups if group["app_label"] != WORK],
            **(extra_context or {}),
        }
        return super().index(request, context)


def _number(value: int) -> str:
    """1978 -> «1 978»: разряды через узкий неразрывный пробел, как в списках."""
    return f"{value:,}".replace(",", "\u202f")


def _home_cards(models: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Карточки главной — по пунктам группы «Работа», в том же порядке."""
    numbers = _home_numbers()
    product, site_list = numbers.get("product"), numbers.get("site_list")
    notes: dict[str, tuple[str, str]] = {}
    if product is not None:
        where = f"{product}" + (f", список «{site_list}»" if site_list else "")
        notes = {
            "productsitelatest": (
                _number(numbers["sites"]),
                f"{where} · не разобрано: {_number(numbers['undecided'])}",
            ),
            "placement": (
                _number(numbers["published"]),
                f"опубликовано · в работе: {_number(numbers['in_progress'])}",
            ),
            "keyword": (_number(numbers["keywords"]), "активных ключей продукта"),
        }
    cards = []
    for model in models:
        value, note = notes.get(model["object_name"].lower(), ("", ""))
        url = model.get("admin_url") or model.get("view_only_url")
        cards.append({"name": model["name"], "url": url, "value": value, "note": note})
    return cards


def _home_numbers() -> dict[str, Any]:
    """Цифры для карточек главной: продукт по умолчанию и самый новый список."""
    # Модели — внутри функции: этот модуль читается при загрузке приложений,
    # до того как модели готовы.
    from apps.keywords.models import Keyword
    from apps.placements.models import Placement, PlacementStatus
    from apps.sites.models import Product, ProductSiteLatest, SiteList, SiteStatus

    product = Product.objects.filter(is_active=True).order_by("pk").first()
    site_list = SiteList.objects.order_by("-created_at", "-pk").first()
    numbers: dict[str, Any] = {"product": product, "site_list": site_list}
    if product is None:
        return numbers
    rows = ProductSiteLatest.objects.filter(product_id=product.pk)
    if site_list is not None:
        rows = rows.filter(site_id__in=site_list.items.values("site_id"))
    numbers.update(
        rows.aggregate(sites=Count("pk"), undecided=Count("pk", filter=Q(status=SiteStatus.NEW)))
    )
    in_progress = [
        PlacementStatus.PLANNED,
        PlacementStatus.ORDERED,
        PlacementStatus.WRITING,
        PlacementStatus.REVIEW,
    ]
    numbers.update(
        Placement.objects.filter(product=product).aggregate(
            published=Count("pk", filter=Q(status=PlacementStatus.PUBLISHED)),
            in_progress=Count("pk", filter=Q(status__in=in_progress)),
        )
    )
    numbers["keywords"] = Keyword.objects.filter(product=product, is_active=True).count()
    return numbers


class SeoAdminConfig(AdminConfig):
    default_site = "config.admin_site.SeoAdminSite"
