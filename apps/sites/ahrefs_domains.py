"""«Домены для Ahrefs»: домены рабочего списка файлами по 500 (E1-10, ADR-045).

Batch Analysis берёт до 500 целей за раз («31 of 500 targets»), поэтому
список режется на части: один домен на строку, файл открыть и вставить в
Ahrefs. Порядок — по DR от большего: если до конца не дойти, первыми
измерятся лучшие площадки. DR — из `v_site_latest`, удалённые площадки туда
не попадают.
"""

import io
import zipfile
from dataclasses import dataclass

from django.db import connection
from django.utils.text import slugify

from apps.sites.models import SiteList

# Лимит Ahrefs Batch Analysis на один запуск — свойство внешнего сервиса, не порог домена.
PART_SIZE = 500

_DOMAINS_SQL = """
SELECT l.domain, l.dr
FROM site_list_items i
JOIN v_site_latest l ON l.id = i.site_id
WHERE i.list_id = %s
ORDER BY l.dr DESC NULLS LAST, l.domain
"""


@dataclass(frozen=True)
class Part:
    number: int  # с 1
    total: int  # частей всего
    start: int  # номер первого домена в списке, с 1
    domains: tuple[str, ...]
    top_dr: int | None  # DR первого домена части

    @property
    def end(self) -> int:
        return self.start + len(self.domains) - 1

    def file_name(self, site_list: SiteList) -> str:
        return f"ahrefs-{_slug(site_list)}-{self.number}-of-{self.total}.txt"

    def text(self) -> str:
        return "\n".join(self.domains) + "\n"


def parts(site_list: SiteList) -> list[Part]:
    """Части списка по `PART_SIZE` доменов. Пустой список — частей нет."""
    # Сырой SQL — отчёт поверх представления (ADR-003).
    with connection.cursor() as cursor:
        cursor.execute(_DOMAINS_SQL, [site_list.pk])
        rows: list[tuple[str, int | None]] = cursor.fetchall()
    chunks = [rows[i : i + PART_SIZE] for i in range(0, len(rows), PART_SIZE)]
    return [
        Part(
            number=index,
            total=len(chunks),
            start=(index - 1) * PART_SIZE + 1,
            domains=tuple(domain for domain, _ in chunk),
            top_dr=chunk[0][1],
        )
        for index, chunk in enumerate(chunks, start=1)
    ]


def archive(site_list: SiteList, all_parts: list[Part]) -> tuple[str, bytes]:
    """Все части одним zip: имя архива и его байты."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive_file:
        for part in all_parts:
            archive_file.writestr(part.file_name(site_list), part.text())
    return f"ahrefs-{_slug(site_list)}.zip", buffer.getvalue()


def _slug(site_list: SiteList) -> str:
    # «Collaborator · 02.10.2026» → collaborator-02102026; кириллица остаётся.
    return slugify(site_list.name, allow_unicode=True) or f"list-{site_list.pk}"
