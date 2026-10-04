"""Отчёт импорта: что создано и обновлено, что не разобралось (маппинг §3).

Ни одна строка не теряется молча: всё, что импорт пропустил, не понял
или не тронул из-за расхождения с базой, попадает в раздел отчёта.
Разделы — перечисление `Section`: порядок в отчёте — порядок в классе.
"""

from collections import Counter
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Section(StrEnum):
    SKIPPED_ROWS = "Строки, которые не импортированы"
    DUPLICATES = "Дубли строк: конфликты значений"
    BAD_VALUES = "Значения, которые не разобрались (записано пусто)"
    UNKNOWN_LANGUAGE = "Незнакомые языки"
    NEW_CATEGORIES = "Категории, которых не было в базе"
    CATALOG_CHANGED = "Карточка Collaborator и объёмы ключей: что перезаписано"
    DECISION_CONFLICTS = "Решения по площадкам: расхождения с базой, не тронуто"
    PLACEMENT_CONFLICTS = "Размещения и ссылки: расхождения с базой, не тронуто"
    KEYWORD_CONFLICTS = "Ключи: расхождения с базой, не тронуто"
    UNKNOWN_STATUS = "Неизвестные статусы размещения — размещение не создано"
    REFUSALS = "Отказы площадок в комментариях — поправить статус в админке"
    ANCHOR_NO_KEYWORD = "Анкоры без ключа"
    ANCHOR_OTHER_URL = "Анкоры с адресом не как у ключа"
    CLIDEO_BAD = "Битые ячейки «Пример статьи на Clideo» — размещение не создано"
    NO_AHREFS = "Строки без данных Ahrefs: US Traff 0 записан как пусто"
    US_EQUALS_GEO = "US Traff = Top Geo Traff при не-US гео"
    NO_TOTAL_PRICE = "«Размещено», а «Итог цена» пустая или 0 — сколько заплатили, неизвестно"
    LINK_COUNTS = "Links Placed / Links Waiting: расчёт по базе не сходится с таблицей"
    COPY_MISMATCH = "Сверка с вкладкой «Размещения»"
    INVOICES = "Вкладка «Счета»: что не записано или расходится с базой"


class Outcome(StrEnum):
    CREATED = "создано"
    UPDATED = "обновлено"
    UNCHANGED = "без изменений"
    SKIPPED = "пропущено"


@dataclass
class Report:
    """Копится по ходу импорта, в конце — Markdown-файл и сводка в терминал."""

    counts: dict[str, Counter[Outcome]] = field(default_factory=dict)
    issues: dict[Section, list[str]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def count(self, table: str, outcome: Outcome, amount: int = 1) -> None:
        if amount:
            self.counts.setdefault(table, Counter())[outcome] += amount

    def issue(self, section: Section, line: str) -> None:
        self.issues.setdefault(section, []).append(line)

    def note(self, line: str) -> None:
        self.notes.append(line)

    def issue_counts(self) -> dict[str, int]:
        return {section.value: len(self.issues[section]) for section in self._sections()}

    def summary(self) -> str:
        """Коротко для терминала: счётчики по таблицам и число строк в разделах."""
        lines = ["Таблицы:"]
        for table, counter in self.counts.items():
            lines.append(f"  {table}: {_format_counter(counter)}")
        if self.notes:
            lines.append("Заметки:")
            lines.extend(f"  {note}" for note in self.notes)
        sections = self._sections()
        if sections:
            lines.append("В отчёте:")
            lines.extend(f"  {section.value}: {len(self.issues[section])}" for section in sections)
        return "\n".join(lines)

    def to_markdown(self, title: str, header: dict[str, str]) -> str:
        parts = [f"# {title}", ""]
        parts.extend(f"- {key}: {value}" for key, value in header.items())
        parts += ["", "## Таблицы", "", "| Таблица | Итог |", "|---|---|"]
        parts.extend(
            f"| {table} | {_format_counter(counter)} |" for table, counter in self.counts.items()
        )
        if self.notes:
            parts += ["", "## Заметки", ""]
            parts.extend(f"- {note}" for note in self.notes)
        for section in self._sections():
            lines = self.issues[section]
            parts += ["", f"## {section.value} — {len(lines)}", ""]
            parts.extend(f"- {line}" for line in lines)
        return "\n".join(parts) + "\n"

    def as_payload(self) -> dict[str, Any]:
        """Сводка для `task_runs.payload`: счётчики без самих строк."""
        return {
            "counts": {
                table: {outcome.value: amount for outcome, amount in counter.items()}
                for table, counter in self.counts.items()
            },
            "issues": self.issue_counts(),
            "notes": self.notes,
        }

    def _sections(self) -> list[Section]:
        return [section for section in Section if self.issues.get(section)]


def _format_counter(counter: Counter[Outcome]) -> str:
    parts = [f"{outcome.value} {counter[outcome]}" for outcome in Outcome if counter[outcome]]
    return ", ".join(parts)
