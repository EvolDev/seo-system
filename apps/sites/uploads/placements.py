"""Файл размещений → размещения продукта и всё, что из них следует (E1-09, ADR-051).

План — без записи, одной функцией и для сводки до записи, и для самой записи
(как у прайса, ADR-044): запись строит план заново в своей транзакции, по
свежему состоянию базы.

Что пишется:
- площадки — одна на домен: новые заводятся под все продукты, удалённые
  возвращаются; заблокированные (чёрный список у всех продуктов) загрузка
  пропускает, как прайс;
- размещение — по общему правилу «какое дополнять» (`placements.matching`):
  новое создаётся, найденное дополняется — пустое заполняется, статус только
  вперёд. Значение в базе другое — в сводку; галочка «Заменить расходящиеся
  значения» берёт значение из файла (статус назад — никогда). Пустой статус в
  файле — «Опубликовано»: файл — список сделанного;
- ссылки — по номеру в статье; ключ — по точному совпадению анкора, как у
  импорта таблицы;
- продавцы — по имени без учёта регистра, новых заводим; сотрудники —
  пользователи без права входа (ADR-041), заводятся при загрузке;
- отметки индексации — проверки человеком в журнале на дату из заголовка или
  на дату файла; «в индексе» у размещения — по самой свежей проверке;
- комментарий — заметка площадки с продуктом; тот же текст второй раз не пишется;
- заплаченная цена — ещё и предложение продавца на дату размещения. Дальше —
  как у прайса (`plan.classify`, ADR-060): у площадки без рабочей цены она
  становится рабочей, у того же продавца и услуги — тоже, если нет заявки в
  работе; цена другого продавца ждёт решения во вкладках разбора загрузки
  («Дешевле рабочей», «Дороже рабочей»…) рядом с ценой Collaborator. Та же
  цена того же продавца второй раз не пишется, нулевая — не пишется вовсе. У
  Collaborator цен из размещений нет — его цены идут из каталога;
- цена выше порога `UPLOAD_PRICE_CAP` не пишется — в сводку;
- все площадки файла — в рабочий список «‹продукт› · размещения · ‹дата›».

Строка, все ссылки которой ведут на другой наш продукт, не записывается: она
попадает в сводку отдельным списком, а если таких строк большинство, сводка
говорит «похоже, это файл ‹продукт›» (ADR-060). 05.10.2026 файл размещений
Clideo записался под Convertio, потому что в шапке стоял рабочий продукт: 455
лишних размещений, 453 сдвинутых статуса и 13 перезаписанных размещений.

Две строки файла про одну площадку видят друг друга: новое размещение первой
строки (ещё без id) попадает в кандидаты следующей.
"""

import datetime as dt
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from django.contrib.auth.models import User
from django.utils import timezone

from apps.content.domain_settings import UploadPriceCap, indexation_schedule, offer_recheck
from apps.keywords.models import Keyword
from apps.observability.models import Check, CheckStatus, Performer
from apps.placements import indexation, invoices
from apps.placements.matching import match_placement, match_without_url, moves_forward
from apps.placements.models import Placement, PlacementLink, PlacementStatus
from apps.sites.models import (
    MetricSource,
    PlacementType,
    Product,
    ReviewGroup,
    Seller,
    Site,
    SiteList,
    SiteListItem,
    SiteNote,
    SitePrice,
    Upload,
    UploadItem,
    ensure_product_sites,
)
from apps.sites.rates import to_eur_cents
from apps.sites.uploads.placement_records import (
    IndexMark,
    LinkData,
    ParsedPlacements,
    PlacementRecord,
    is_on_domain,
)
from apps.sites.uploads.plan import (
    LIST_LIMIT,
    Decision,
    SiteState,
    blocked_ids,
    classify,
    load_state,
    start_of_day,
)

CHUNK = 1000
_USERNAME = re.compile(r"[^\w.@+-]+")

# Подписи полей размещения в сводке.
LABELS = {
    "status": "статус",
    "published_at": "дата публикации",
    "ordered_at": "дата заявки",
    "placement_type": "тип размещения",
    "seller": "продавец",
    "employee": "сотрудник",
    "price_paid": "заплачено",
}
NEW_OFFER, CHANGED_OFFER, SAME_OFFER = "new", "update", "same"

type OfferKey = tuple[int, str, str, dt.datetime]  # площадка, продавец, услуга, дата


def list_name(product: Product, day: dt.date) -> str:
    return f"{product.name} · размещения · {day:%d.%m.%Y}"


def employee_name(user: User) -> str:
    """Как сотрудник подписан на экранах: имя, а без имени — логин."""
    return user.first_name or user.username


def find_employee(name: str) -> User | None:
    """Сотрудник по имени или логину без учёта регистра."""
    return _employees().get(name.strip().lower())


def create_employee(name: str) -> User:
    """Сотрудник — пользователь без права входа (ADR-041): пароля нет, в админку не пускает.

    Начнёт работать в системе сам — ему зададут пароль, и его размещения уже за ним.
    """
    user = User(username=_free_username(name), first_name=name.strip(), is_staff=False)
    user.set_unusable_password()
    user.save()
    return user


@dataclass(frozen=True)
class Conflict:
    """Заполненное в базе значение, которое в файле другое."""

    line: int
    domain: str
    what: str
    base: str
    file: str


@dataclass
class PlanRow:
    """Строка файла и что с ней будет."""

    record: PlacementRecord
    site_id: int | None  # None — площадки нет в базе, её заведёт запись
    status: PlacementStatus
    seller: str | None  # имя продавца: из строки или «от кого»
    employee: str | None
    paid_cents: int | None  # цены — после порога
    offer_cents: int | None
    writing_cents: int | None
    announce_cents: int | None
    target: Placement | None = None  # что дополнять или новое (ещё без id); None — неясно
    is_new: bool = False
    other_article: bool = False  # у продукта на площадке уже есть статья с другим адресом
    fill: dict[str, Any] = field(default_factory=dict)  # пустое в базе → значение
    replace: dict[str, Any] = field(default_factory=dict)  # другое в базе → значение файла
    conflicts: list[Conflict] = field(default_factory=list)
    status_moves: bool = False
    links_new: list[LinkData] = field(default_factory=list)
    links_changed: list[tuple[PlacementLink, LinkData]] = field(default_factory=list)
    marks: list[tuple[dt.datetime, IndexMark]] = field(default_factory=list)  # новые отметки
    note_new: bool = False
    offer: str | None = None  # NEW_OFFER, CHANGED_OFFER, SAME_OFFER; None — предложения нет
    becomes_working: bool = False
    decision: Decision | None = None  # вкладка разбора и нужно ли решение — как у прайса
    ref_price_id: int | None = None  # рабочая цена площадки до загрузки

    @property
    def ambiguous(self) -> bool:
        return self.target is None

    @property
    def changes(self) -> bool:
        return bool(self.fill or self.status_moves)


@dataclass
class PlacementPlan:
    product: Product
    currency: str
    file_date: dt.date
    parsed: ParsedPlacements
    cap: UploadPriceCap
    sites: dict[str, int] = field(default_factory=dict)  # домен → площадка базы
    restore: dict[str, int] = field(default_factory=dict)  # удалённые: запись снимет пометку
    blocked: list[str] = field(default_factory=list)
    sellers: dict[str, Seller] = field(default_factory=dict)  # имя без регистра → продавец
    employees: dict[str, User] = field(default_factory=dict)  # имя без регистра → сотрудник
    rows: list[PlanRow] = field(default_factory=list)
    capped: list[dict[str, Any]] = field(default_factory=list)
    no_status: int = 0
    # Строки, чьи ссылки ведут на другой наш продукт: {line, domain, product, url}.
    foreign: list[dict[str, Any]] = field(default_factory=list)

    @property
    def foreign_product(self) -> str | None:
        """Продукт, на который ведёт большинство строк файла, — или None.

        Половина и больше строк со ссылками на один чужой продукт — это файл
        не того продукта: сводка говорит об этом прямо, до записи.
        """
        if not self.foreign:
            return None
        names = Counter(row["product"] for row in self.foreign)
        name, count = names.most_common(1)[0]
        return name if count * 2 >= len(self.rows) + len(self.foreign) else None

    @property
    def new_domains(self) -> list[str]:
        domains = {row.record.domain for row in self.rows if row.site_id is None}
        return sorted(domains - set(self.restore))

    def summary(self, *, rows: int, blank_rows: int) -> dict[str, Any]:
        """Сводка до записи — то, что видит человек перед «Записать»."""
        work = [row for row in self.rows if not row.ambiguous]
        domains = {row.record.domain for row in self.rows}
        known = {row.record.domain for row in self.rows if row.site_id is not None}
        conflicts = [
            {"line": c.line, "domain": c.domain, "what": c.what, "base": c.base, "file": c.file}
            for row in work
            for c in row.conflicts
        ]
        # Непонятное в пропущенных строках не показываем: эти строки не пишутся,
        # и «ссылка ведёт не на домен продукта — запишется» о них противоречит
        # списку «Ссылки ведут на другой продукт — пропущены».
        skipped = {row["line"] for row in self.foreign}
        issues = {
            kind: [issue for issue in found if issue.line not in skipped]
            for kind, found in self.parsed.issues.items()
        }
        return {
            "rows": rows,
            "blank_rows": blank_rows,
            "product": self.product.name,
            "currency": self.currency,
            "sites": len(domains),
            "known": len(known),
            "new": len(domains) - len(known),
            "restored": len(domains & set(self.restore)),
            "placements_new": sum(row.is_new for row in work),
            "placements_supplemented": sum(not row.is_new and row.changes for row in work),
            "placements_unchanged": sum(not row.is_new and not row.changes for row in work),
            "no_status": self.no_status,
            "sellers_new": _new_names((row.seller for row in work), self.sellers),
            "employees_new": _new_names((row.employee for row in work), self.employees),
            "links_new": sum(len(row.links_new) for row in work),
            "checks_new": sum(len(row.marks) for row in work),
            "notes_new": sum(row.note_new for row in work),
            "offers_new": sum(row.offer == NEW_OFFER for row in work),
            "offers_working": sum(row.becomes_working for row in work),
            "offers_review": sum(
                bool(row.decision and row.decision.needs_decision) for row in work
            ),
            "cap_eur": self.cap.eur,
            **_capped("conflicts", conflicts),
            **_capped("ambiguous", [_line(row) for row in self.rows if row.ambiguous]),
            **_capped("other_article", [_line(row) for row in work if row.other_article]),
            **_capped("price_cap", self.capped),
            **_capped(
                "errors", [{"line": e.line, "message": e.message} for e in self.parsed.errors]
            ),
            **_capped(
                "duplicates",
                [{"domain": d, "lines": list(lines)} for d, lines in self.parsed.duplicates],
            ),
            **_capped("blocked", [{"domain": domain} for domain in self.blocked]),
            **_capped("foreign", self.foreign),
            "foreign_product": self.foreign_product,
            **{
                kind: [
                    {"line": i.line, "domain": i.domain, "message": i.message}
                    for i in found[:LIST_LIMIT]
                ]
                for kind, found in issues.items()
            },
            **{f"{kind}_total": len(found) for kind, found in issues.items()},
        }


# --- План ---


class _Planner:
    """Строит план строка за строкой; состояние базы читает один раз."""

    def __init__(
        self,
        parsed: ParsedPlacements,
        upload: Upload,
        rates: dict[str, Decimal],
        cap: UploadPriceCap,
    ) -> None:
        self.upload = upload
        self.rates = rates
        self.plan = PlacementPlan(
            product=upload.get_product(),
            currency=upload.currency or "EUR",
            file_date=upload.prices_date,
            parsed=parsed,
            cap=cap,
        )
        domains = sorted({record.domain for record in parsed.records})
        sites, self.plan.restore, _ = _load_sites(domains)
        blocked = blocked_ids(sites.values())
        self.plan.blocked = sorted(d for d, site_id in sites.items() if site_id in blocked)
        for domain in self.plan.blocked:
            del sites[domain]
        self.plan.sites = sites
        site_ids = list(sites.values())
        self.pools: dict[str, list[Placement]] = _placements(sites, self.plan.product)
        self.plan.sellers = _sellers(parsed.records, upload)
        self.plan.employees = _employees()
        self.human_checks = _human_checks(p.pk for pool in self.pools.values() for p in pool)
        self.notes = _notes(sites)
        self.offers = _offers_by_key(site_ids)
        self.latest = _latest_offers(site_ids)
        self.recheck_pct = offer_recheck().min_change_pct
        # Рабочая цена, отказы и заявки площадок — как их видит разбор прайса.
        self.states: dict[str, SiteState] = load_state(
            domains, seller_id=0, before=start_of_day(upload.prices_date), rates=rates
        ).sites
        # id() размещения → что эта загрузка уже собирается к нему добавить.
        self.planned_links: dict[int, set[int]] = {}
        self.planned_marks: set[tuple[int, dt.datetime]] = set()
        collaborator = Seller.objects.filter(is_collaborator=True).values_list("name", flat=True)
        self.collaborator = {name.lower() for name in collaborator}
        # Домены остальных продуктов: по ним узнаём строки из чужого файла.
        self.others = {
            product.domain: product.name
            for product in Product.objects.exclude(pk=self.plan.product.pk)
            if product.domain
        }

    def build(self) -> PlacementPlan:
        for record in self.plan.parsed.records:
            if record.domain in self.plan.blocked:
                continue
            foreign = self._foreign(record)
            if foreign is not None:
                self.plan.foreign.append(foreign)
                continue
            self.plan.rows.append(self._row(record))
        return self.plan

    def _foreign(self, record: PlacementRecord) -> dict[str, Any] | None:
        """Строка другого продукта: её ссылки ведут на его домен, а не на наш.

        Хоть одна ссылка на свой продукт — строка наша. Строка без ссылок —
        тоже наша: по чему судить, непонятно, а статус и цена из файла нужны.
        """
        mine = self.plan.product.domain
        if any(is_on_domain(link.target_url, mine) for link in record.links):
            return None
        for link in record.links:
            for domain, name in self.others.items():
                if is_on_domain(link.target_url, domain):
                    return {
                        "line": record.line,
                        "domain": record.domain,
                        "product": name,
                        "url": link.target_url,
                    }
        return None

    def _row(self, record: PlacementRecord) -> PlanRow:
        plan = self.plan
        upload = self.upload
        if record.status is None:
            plan.no_status += 1
        row = PlanRow(
            record=record,
            site_id=plan.sites.get(record.domain),
            status=record.status or PlacementStatus.PLACED,
            seller=record.seller or (upload.seller.name if upload.seller else None),
            employee=record.employee
            or (employee_name(upload.employee) if upload.employee else None),
            paid_cents=self._price(record, "Заплачено", record.paid_cents),
            offer_cents=None,
            writing_cents=self._price(record, "Написание", record.writing_cents),
            announce_cents=self._price(record, "Анонс", record.announce_cents),
        )
        if record.price_cents is not None:
            row.offer_cents = self._price(record, "Цена размещения", record.price_cents)
        else:
            row.offer_cents = row.paid_cents
        pool = self.pools.setdefault(record.domain, [])
        match = (
            match_placement(pool, record.article_url)
            if record.article_url
            else match_without_url(pool)
        )
        if match.ambiguous:
            return row
        if match.placement is None:
            row.is_new = True
            row.other_article = bool(record.article_url) and any(p.article_url for p in pool)
            # Площадку новому размещению проставит запись: её может ещё не быть в базе.
            row.target = Placement(product=plan.product)
            pool.append(row.target)
            self._new(row)
        else:
            row.target = match.placement
            self._existing(row)
        self._links(row)
        self._marks(row)
        if record.note and (record.domain, record.note) not in self.notes:
            row.note_new = True
            self.notes.add((record.domain, record.note))
        seller = row.seller
        if seller and seller.lower() not in self.collaborator and row.offer_cents is not None:
            self._offer(row, seller)
        return row

    def _price(self, record: PlacementRecord, what: str, cents: int | None) -> int | None:
        """Цена выше порога похожа на ошибку: не пишется, строка — в сводку."""
        if cents is None:
            return None
        eur = to_eur_cents(cents, self.plan.currency, self.rates)
        if eur is not None and eur > self.plan.cap.eur_cents:
            self.plan.capped.append(
                {
                    "line": record.line,
                    "domain": record.domain,
                    "what": what,
                    "value": _money(cents, self.plan.currency),
                }
            )
            return None
        return cents

    def _new(self, row: PlanRow) -> None:
        record = row.record
        target = row.target
        assert target is not None
        row.fill = {
            "status": row.status,
            "article_url": record.article_url,
            "placement_type": record.placement_type,
            "extra": record.extra or None,
        }
        for day_field, day in self._dates(record, row.status):
            row.fill[day_field] = start_of_day(day)
        if row.paid_cents is not None:
            row.fill["price_paid"] = row.paid_cents
        if row.seller:
            row.fill["seller"] = row.seller
        if row.employee:
            row.fill["employee"] = row.employee
        # Следующая строка того же файла должна видеть адрес нового размещения.
        target.article_url = record.article_url

    def _dates(self, record: PlacementRecord, status: PlacementStatus) -> list[tuple[str, dt.date]]:
        """Какие даты записать: поле → день из файла.

        Колонка «Дата отправки» размечена — каждая дата идёт в своё поле, и
        пустая колонка оставляет поле пустым (решение пользователя 07.10.2026).
        Колонки нет — файл старого вида с единственной датой, и её по-прежнему
        раскладывает статус: у опубликованного это день публикации, у
        остального — день заявки.
        """
        if not self.plan.parsed.has_ordered_column:
            if record.published_on is None:
                return []
            return [(_date_field(status), record.published_on)]
        found = []
        if record.published_on is not None:
            found.append(("published_at", record.published_on))
        if record.ordered_on is not None:
            found.append(("ordered_at", record.ordered_on))
        return found

    def _existing(self, row: PlanRow) -> None:
        record = row.record
        target = row.target
        assert target is not None
        if moves_forward(target.status, row.status):
            row.status_moves = True
        elif target.status != row.status and record.status is not None:
            current = PlacementStatus(target.status).label
            self._conflict(row, "status", current, row.status.label, replaceable=False)
        if record.article_url and not target.article_url:
            row.fill["article_url"] = record.article_url
            target.article_url = record.article_url
        status = row.status if row.status_moves else PlacementStatus(target.status)
        for day_field, day in self._dates(record, status):
            current_day = getattr(target, day_field)
            wanted = start_of_day(day)
            if current_day is None:
                row.fill[day_field] = wanted
            elif timezone.localtime(current_day).date() != day:
                self._conflict(row, day_field, _day(current_day), _day(wanted), value=wanted)
        if record.placement_type is not None:
            if not target.placement_type:
                row.fill["placement_type"] = record.placement_type
            elif target.placement_type != record.placement_type:
                current = PlacementType(target.placement_type).label
                wanted_type = record.placement_type
                self._conflict(row, "placement_type", current, wanted_type.label, value=wanted_type)
        if row.paid_cents is not None:
            paid = (target.price_paid_cents, target.currency)
            if target.price_paid_cents is None:
                row.fill["price_paid"] = row.paid_cents
            elif paid != (row.paid_cents, self.plan.currency):
                # «Заплачено» из счёта пишет только счёт (ADR-055): видно, не заменяется.
                from_invoice = target.pk is not None and bool(invoices.invoiced([target.pk]))
                self._conflict(
                    row,
                    "price_paid",
                    _money(target.price_paid_cents, target.currency)
                    + (" — из счёта" if from_invoice else ""),
                    _money(row.paid_cents, self.plan.currency),
                    value=row.paid_cents,
                    replaceable=not from_invoice,
                )
        known_seller = target.seller.name if target.seller_id and target.seller else None
        known_employee = (
            employee_name(target.employee) if target.employee_id and target.employee else None
        )
        for name, wanted_name, known in (
            ("seller", row.seller, known_seller),
            ("employee", row.employee, known_employee),
        ):
            if not wanted_name:
                continue
            if known is None:
                row.fill[name] = wanted_name
            elif known.lower() != wanted_name.lower():
                self._conflict(row, name, known, wanted_name, value=wanted_name)
        if record.extra:
            extra = dict(target.extra or {})
            missing = {k: v for k, v in record.extra.items() if k not in extra}
            if missing:
                row.fill["extra"] = {**extra, **missing}

    def _links(self, row: PlanRow) -> None:
        target = row.target
        assert target is not None
        planned = self.planned_links.setdefault(id(target), set())
        existing = {link.link_index: link for link in target.links.all()} if target.pk else {}
        for link in row.record.links:
            current = existing.get(link.index)
            if current is None:
                if link.index not in planned:
                    row.links_new.append(link)
                    planned.add(link.index)
                continue
            same = (current.anchor, current.target_url.rstrip("/")) == (
                link.anchor,
                link.target_url.rstrip("/"),
            )
            if not same:
                row.links_changed.append((current, link))
                self._conflict(
                    row,
                    f"ссылка {link.index}",
                    f"«{current.anchor}» → {current.target_url}",
                    f"«{link.anchor}» → {link.target_url}",
                    replaceable=False,
                )

    def _marks(self, row: PlanRow) -> None:
        target = row.target
        assert target is not None
        for mark in row.record.marks:
            checked_at = start_of_day(mark.day or self.plan.file_date)
            if target.pk and (target.pk, checked_at) in self.human_checks:
                continue
            if (id(target), checked_at) in self.planned_marks:
                continue
            self.planned_marks.add((id(target), checked_at))
            row.marks.append((checked_at, mark))

    def _offer(self, row: PlanRow, seller: str) -> None:
        record = row.record
        cents = row.offer_cents
        if not cents:
            return  # «бесплатно» или пусто — не цена продавца
        service = record.placement_type or PlacementType.GUEST_POST
        checked_at = start_of_day(record.published_on or self.plan.file_date)
        known = self.plan.sellers.get(seller.lower())
        state = self.states.get(record.domain) if row.site_id is not None else None
        row.decision = classify(
            service=service,
            cents=cents,
            currency=self.plan.currency,
            eur_cents=to_eur_cents(cents, self.plan.currency, self.rates),
            seller_id=known.pk if known is not None else -1,
            site=state,
            record_has_gp=False,
            recheck_pct=self.recheck_pct,
            at=checked_at,
        )
        row.becomes_working = row.decision.becomes_working
        row.ref_price_id = state.working.id if state is not None and state.working else None
        if row.site_id is None:
            row.offer = NEW_OFFER
            return
        existing = self.offers.get((row.site_id, seller.lower(), service, checked_at))
        latest = self.latest.get((row.site_id, seller.lower(), service))
        if existing is None and latest is not None and _same_price(latest, cents, self.plan):
            # Та же цена того же продавца уже есть с другой датой — второй не пишем.
            row.offer = SAME_OFFER
        elif existing is None:
            row.offer = NEW_OFFER
        elif _same_price(existing, cents, self.plan):
            row.offer = SAME_OFFER
        else:
            row.offer = CHANGED_OFFER

    def _conflict(
        self,
        row: PlanRow,
        name: str,
        base: str,
        file: str,
        *,
        value: Any = None,
        replaceable: bool = True,
    ) -> None:
        label = LABELS.get(name, name)
        row.conflicts.append(Conflict(row.record.line, row.record.domain, label, base, file))
        if replaceable:
            row.replace[name] = value


def build_plan(
    parsed: ParsedPlacements,
    *,
    upload: Upload,
    rates: dict[str, Decimal],
    cap: UploadPriceCap,
) -> PlacementPlan:
    """Что запишется по свежему состоянию базы. Базу не меняет."""
    return _Planner(parsed, upload, rates, cap).build()


# --- Запись ---


class Writer:
    """Запись плана — внутри транзакции и блокировки загрузки (`service.write`)."""

    def __init__(self, plan: PlacementPlan, upload: Upload, *, replace: bool) -> None:
        self.plan = plan
        self.upload = upload
        self.replace = replace
        self.now = timezone.now()
        self.counts: Counter[str] = Counter()
        self.site_ids = dict(plan.sites)
        self.created: set[str] = set()
        self.schedule = indexation_schedule(plan.product.pk)
        self.items: list[UploadItem] = []  # строки разбора цен — как у прайса

    def run(self) -> dict[str, Any]:
        self._sites()
        sellers = self._sellers()
        employees = self._employees()
        keywords = {k.keyword: k for k in Keyword.objects.filter(product=self.plan.product)}
        offers = _offers_by_key(list(self.site_ids.values()))
        for row in self.plan.rows:
            if row.ambiguous:
                self.counts["ambiguous"] += 1
                continue
            site_id = self.site_ids[row.record.domain]
            placement = self._placement(row, site_id, sellers, employees)
            self._links(row, placement, keywords)
            self._checks(row, placement)
            self._note(row, site_id)
            self._offer(row, site_id, sellers, offers)
        UploadItem.objects.bulk_create(self.items, batch_size=CHUNK)
        self.counts["needs_decision"] = sum(item.needs_decision for item in self.items)
        site_list = self._site_list()
        self.upload.site_list = site_list
        return {"counts": dict(self.counts), "list": site_list.name}

    # --- Площадки, продавцы, сотрудники ---

    def _sites(self) -> None:
        restored = []
        for domain, pk in self.plan.restore.items():
            restored.append(Site(pk=pk, is_deleted=False, updated_at=self.now))
            self.site_ids[domain] = pk
        Site.all_objects.bulk_update(restored, ["is_deleted", "updated_at"], batch_size=CHUNK)
        source = f"Размещения {self.plan.product.name}: {self.upload.file_name}"
        new = [Site(domain=domain, source=source) for domain in self.plan.new_domains]
        # bulk_create не вызывает save(): домен нормализован при разборе,
        # строки «продукт × площадка» создаёт ensure_product_sites одним вызовом.
        created = Site.all_objects.bulk_create(new, batch_size=CHUNK)
        ensure_product_sites(
            site_ids=[site.pk for site in created] + list(self.plan.restore.values())
        )
        for site in created:
            self.site_ids[site.domain] = site.pk
            self.created.add(site.domain)
        self.counts["sites_created"] = len(created)
        self.counts["sites_restored"] = len(restored)

    def _sellers(self) -> dict[str, Seller]:
        sellers = dict(self.plan.sellers)
        for row in self.plan.rows:
            name = row.seller
            if name and name.lower() not in sellers:
                sellers[name.lower()] = Seller.objects.create(
                    name=name, currency=self.plan.currency
                )
                self.counts["sellers_created"] += 1
        return sellers

    def _employees(self) -> dict[str, User]:
        employees = dict(self.plan.employees)
        for row in self.plan.rows:
            name = row.employee
            if name and name.lower() not in employees:
                employees[name.lower()] = create_employee(name)
                self.counts["employees_created"] += 1
        return employees

    # --- Размещение ---

    def _placement(
        self,
        row: PlanRow,
        site_id: int,
        sellers: dict[str, Seller],
        employees: dict[str, User],
    ) -> Placement:
        placement = row.target
        assert placement is not None
        placement.site_id = site_id
        values = dict(row.fill)
        if self.replace and not row.is_new:
            values.update(row.replace)
        changed = bool(values) or row.status_moves
        if row.status_moves:
            placement.status = row.status
        for name, value in values.items():
            if name == "seller":
                placement.seller = sellers[str(value).lower()]
            elif name == "employee":
                placement.employee = employees[str(value).lower()]
            elif name == "price_paid":
                placement.price_paid_cents = value
                placement.currency = self.plan.currency
            else:
                setattr(placement, name, value)
        indexed = _latest_mark(row, placement)
        if indexed is not None:
            placement.is_indexed, placement.indexed_checked_at = indexed
            changed = True
        if placement.pk is None:
            placement.save()
            self.counts["placements_created"] += 1
        elif changed:
            placement.save()
            self.counts["placements_updated"] += 1
        else:
            self.counts["placements_unchanged"] += 1
        return placement

    def _links(self, row: PlanRow, placement: Placement, keywords: dict[str, Keyword]) -> None:
        for link in row.links_new:
            PlacementLink.objects.create(
                placement=placement,
                keyword=keywords.get(link.anchor),
                anchor=link.anchor,
                target_url=link.target_url,
                link_index=link.index,
            )
            self.counts["links_created"] += 1
        if not self.replace:
            return
        for current, link in row.links_changed:
            current.anchor = link.anchor
            current.target_url = link.target_url
            current.keyword = keywords.get(link.anchor)
            current.save(update_fields=["anchor", "target_url", "keyword"])
            self.counts["links_updated"] += 1

    def _checks(self, row: PlanRow, placement: Placement) -> None:
        for checked_at, mark in row.marks:
            Check.objects.create(
                entity_type=indexation.ENTITY_TYPE,
                entity_id=placement.pk,
                check_type=indexation.CHECK_TYPE,
                status=CheckStatus.OK if mark.indexed else CheckStatus.FAILED,
                result={
                    "source": "upload",
                    "upload_id": self.upload.pk,
                    "file": self.upload.file_name,
                    "column": mark.header,
                    "line": row.record.line,
                },
                performed_by=Performer.HUMAN,
                checked_at=checked_at,
                next_check_at=indexation.next_check_at(mark.indexed, self.schedule, checked_at),
            )
            self.counts["checks_created"] += 1

    def _note(self, row: PlanRow, site_id: int) -> None:
        note = row.record.note
        if not note:
            return
        # Тот же текст у площадки второй раз не пишется — как у прайса (E1-08).
        if SiteNote.objects.filter(site_id=site_id, body=note).exists():
            self.counts["notes_unchanged"] += 1
            return
        SiteNote.objects.create(
            site_id=site_id, product=self.plan.product, body=note, source=self.upload.file_name
        )
        self.counts["notes_created"] += 1

    def _offer(
        self,
        row: PlanRow,
        site_id: int,
        sellers: dict[str, Seller],
        offers: dict[OfferKey, SitePrice],
    ) -> None:
        if row.offer is None or row.seller is None or row.decision is None:
            return
        record = row.record
        seller = sellers[row.seller.lower()]
        service = record.placement_type or PlacementType.GUEST_POST
        checked_at = start_of_day(record.published_on or self.plan.file_date)
        values: dict[str, Any] = {
            "placement_cents": row.offer_cents,
            "writing_cents": row.writing_cents,
            "announce_cents": row.announce_cents,
            "currency": self.plan.currency,
            "extra": {"Цена из размещения": f"{self.plan.product.name}, {self.upload.file_name}"},
        }
        decision = row.decision
        # Ждёт решения — без отметки «разобрано»: вопрос в разборе и в «Площадках».
        reviewed_at = None if decision.needs_decision else self.now
        key = (site_id, seller.name.lower(), service, checked_at)
        offer = offers.get(key)
        if row.offer == SAME_OFFER and offer is None:
            offer = _latest_offers([site_id]).get((site_id, seller.name.lower(), service))
        if offer is None:
            offer = SitePrice.objects.create(
                site_id=site_id,
                seller=seller,
                placement_type=service,
                source=MetricSource.CSV_IMPORT,
                checked_at=checked_at,
                reviewed_at=reviewed_at,
                **values,
            )
            offers[key] = offer
            self.counts["offers_created"] += 1
        elif row.offer == CHANGED_OFFER:
            for name, value in values.items():
                setattr(offer, name, value)
            offer.reviewed_at = reviewed_at
            offer.save(update_fields=[*values, "reviewed_at"])
            self.counts["offers_updated"] += 1
        else:
            self.counts["offers_unchanged"] += 1
        if row.becomes_working:
            Site.all_objects.filter(pk=site_id).update(price_id=offer.pk, updated_at=self.now)
            self.counts["working_set"] += 1
        group = decision.group if row.offer != SAME_OFFER else ReviewGroup.SAME
        self.items.append(
            UploadItem(
                upload=self.upload,
                site_id=site_id,
                price_id=offer.pk,
                ref_price_id=row.ref_price_id,
                review_group=group,
                needs_decision=decision.needs_decision and row.offer != SAME_OFFER,
                auto_applied=row.becomes_working,
                site_created=record.domain in self.created,
                line=record.line,
            )
        )

    def _site_list(self) -> SiteList:
        site_list, _ = SiteList.objects.get_or_create(
            name=list_name(self.plan.product, self.plan.file_date),
            defaults={"source": self.upload.file_name},
        )
        domains = sorted({row.record.domain for row in self.plan.rows})
        present = set(
            SiteListItem.objects.filter(site_list=site_list).values_list("site_id", flat=True)
        )
        new = [
            SiteListItem(
                site_list=site_list, site_id=self.site_ids[d], first_seen=d in self.created
            )
            for d in domains
            if self.site_ids[d] not in present
        ]
        SiteListItem.objects.bulk_create(new, batch_size=CHUNK)
        self.counts["list_items_created"] = len(new)
        self.counts["list_sites"] = len(domains)
        return site_list


def _latest_mark(row: PlanRow, placement: Placement) -> tuple[bool, dt.datetime] | None:
    """«В индексе» по самой свежей отметке, если она не старше того, что уже знает база."""
    if not row.marks:
        return None
    checked_at, mark = max(row.marks, key=lambda pair: pair[0])
    known = placement.indexed_checked_at
    if known is not None and known > checked_at:
        return None
    return mark.indexed, checked_at


# --- Состояние базы ---


def _load_sites(domains: Sequence[str]) -> tuple[dict[str, int], dict[str, int], dict[int, int]]:
    """Площадки файла в базе: домен → id; удалённые отдельно; рабочие цены."""
    sites: dict[str, int] = {}
    deleted: dict[str, int] = {}
    prices: dict[int, int] = {}
    for chunk in _chunks(domains):
        rows = Site.all_objects.filter(domain__in=chunk).values_list(
            "id", "domain", "is_deleted", "price_id"
        )
        for site_id, domain, is_deleted, price_id in rows:
            if is_deleted:
                deleted[domain] = site_id
                continue
            sites[domain] = site_id
            if price_id is not None:
                prices[site_id] = price_id
    return sites, deleted, prices


def _placements(sites: dict[str, int], product: Product) -> dict[str, list[Placement]]:
    """Размещения продукта на площадках файла, по домену: кандидаты «что дополнять»."""
    domains = {site_id: domain for domain, site_id in sites.items()}
    result: dict[str, list[Placement]] = {}
    for chunk in _chunks(list(domains)):
        rows = (
            Placement.objects.filter(site_id__in=chunk, product=product)
            .select_related("seller", "employee")
            .prefetch_related("links")
            .order_by("pk")
        )
        for placement in rows:
            result.setdefault(domains[placement.site_id], []).append(placement)
    return result


def _latest_offers(site_ids: Sequence[int]) -> dict[tuple[int, str, str], SitePrice]:
    """Последняя цена площадки у продавца за услугу — ключ «площадка, продавец, услуга»."""
    result: dict[tuple[int, str, str], SitePrice] = {}
    for chunk in _chunks(site_ids):
        rows = (
            SitePrice.objects.filter(site_id__in=chunk)
            .select_related("seller")
            .order_by("-checked_at", "-pk")
        )
        for offer in rows:
            key = (offer.site_id, offer.seller.name.lower(), offer.placement_type)
            result.setdefault(key, offer)
    return result


def _same_price(offer: SitePrice, cents: int, plan: "PlacementPlan") -> bool:
    return (offer.placement_cents, offer.currency) == (cents, plan.currency)


def _sellers(records: Iterable[PlacementRecord], upload: Upload) -> dict[str, Seller]:
    """Продавцы базы с именами из файла — без учёта регистра, как в `sellers_name_key`."""
    names = {record.seller.lower() for record in records if record.seller}
    if upload.seller is not None:
        names.add(upload.seller.name.lower())
    return {s.name.lower(): s for s in Seller.objects.all() if s.name.lower() in names}


def _employees() -> dict[str, User]:
    """Пользователи по имени и по логину без учёта регистра — их немного."""
    result: dict[str, User] = {}
    for user in User.objects.order_by("pk"):
        result.setdefault(user.username.lower(), user)
        if user.first_name:
            result.setdefault(user.first_name.lower(), user)
    return result


def _human_checks(placement_ids: Iterable[int]) -> set[tuple[int, dt.datetime]]:
    """Отметки индексации человеком, которые уже есть: размещение и время."""
    result: set[tuple[int, dt.datetime]] = set()
    for chunk in _chunks(list(placement_ids)):
        rows = Check.objects.filter(
            entity_type=indexation.ENTITY_TYPE,
            entity_id__in=chunk,
            check_type=indexation.CHECK_TYPE,
            performed_by=Performer.HUMAN,
        ).values_list("entity_id", "checked_at")
        result.update((int(entity_id), checked_at) for entity_id, checked_at in rows)
    return result


def _notes(sites: dict[str, int]) -> set[tuple[str, str]]:
    """Заметки площадок файла: домен и текст — тот же текст второй раз не пишется."""
    domains = {site_id: domain for domain, site_id in sites.items()}
    result: set[tuple[str, str]] = set()
    for chunk in _chunks(list(domains)):
        rows = SiteNote.objects.filter(site_id__in=chunk).values_list("site_id", "body")
        result.update((domains[site_id], body) for site_id, body in rows)
    return result


def _offers_by_key(site_ids: Sequence[int]) -> dict[OfferKey, SitePrice]:
    """Предложения площадок по ключу «площадка, продавец, услуга, дата» — снимок дня."""
    result: dict[OfferKey, SitePrice] = {}
    for chunk in _chunks(site_ids):
        rows = SitePrice.objects.filter(site_id__in=chunk).select_related("seller").order_by("pk")
        for offer in rows:
            key = (offer.site_id, offer.seller.name.lower(), offer.placement_type, offer.checked_at)
            result.setdefault(key, offer)
    return result


# --- Мелочи ---


def _date_field(status: PlacementStatus) -> str:
    """Дата из файла у опубликованного — день публикации, у остального — день заявки."""
    return "published_at" if status == PlacementStatus.PLACED else "ordered_at"


def _free_username(name: str) -> str:
    """Логин из имени как в файле («Evgeniy»): его показывает выбор сотрудника в форме."""
    base = _USERNAME.sub("_", name.strip()).strip("_")[:140] or "employee"
    username = base
    number = 2
    while User.objects.filter(username__iexact=username).exists():
        username = f"{base}_{number}"
        number += 1
    return username


def _new_names(names: Iterable[str | None], known: dict[str, Any]) -> list[str]:
    """Имена, которых нет в базе: одно написание на имя без учёта регистра."""
    found: dict[str, str] = {}
    for name in names:
        if name and name.lower() not in known:
            found.setdefault(name.lower(), name)
    return sorted(found.values(), key=str.lower)


def _line(row: PlanRow) -> dict[str, Any]:
    return {
        "line": row.record.line,
        "domain": row.record.domain,
        "url": row.record.article_url or "",
    }


def _day(moment: dt.datetime) -> str:
    return f"{timezone.localtime(moment):%d.%m.%Y}"


def _money(cents: int | None, currency: str | None) -> str:
    if cents is None:
        return "—"
    amount = f"{cents / 100:,.2f}".replace(",", " ").replace(".", ",")
    return f"{amount} {currency or ''}".strip()


def _capped(name: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {name: rows[:LIST_LIMIT], f"{name}_total": len(rows)}


def _chunks[T](values: Sequence[T] | Iterable[T]) -> list[list[T]]:
    items = list(values)
    return [items[i : i + CHUNK] for i in range(0, len(items), CHUNK)]
