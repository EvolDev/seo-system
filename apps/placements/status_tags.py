"""Тег полосы быстрых фильтров над списком: статус кнопками и «Период ▾» (ADR-073)."""

from typing import Any

from django import template
from django.utils import timezone

from apps.placements.status_chips import StatusPeriodFilter, month_value

register = template.Library()


@register.inclusion_tag("admin/seo_status_bar.html")
def status_bar(cl: Any) -> dict[str, Any]:
    """Полоса над списком, если у списка есть фильтр «статус и период»."""
    spec = next(
        (spec for spec in getattr(cl, "filter_specs", ()) if isinstance(spec, StatusPeriodFilter)),
        None,
    )
    if spec is None:
        return {"spec": None}
    period = spec.period
    first, last = period.own if period.own is not None else (None, None)
    return {
        "spec": spec,
        "statuses": spec.status_choices(cl),
        "periods": spec.period_choices(cl),
        "period": period,
        "kept": spec.kept_params(cl),
        "from_value": month_value(first),
        "to_value": month_value(last),
        "max_month": month_value(timezone.localdate()),
    }
