"""Рабочий продукт в шапке, рядом с «SEO-система» (E9-12, ADR-057).

`{% working_product_switch %}` в `admin/base_site.html`: выбор продукта —
форма POST на `admin:working_product`; выбор в списке отправляет её сам
(`seo/product-switch.js`), и экран под новый продукт приходит без перезагрузки
(seo/soft-nav.js). На странице входа и во всплывающем окне —
пусто.
"""

from typing import Any

from django import template
from django.template.loader import render_to_string
from django.utils.safestring import SafeString, mark_safe

from apps.workspace.products import products_of, working_product_id

register = template.Library()


@register.simple_tag(takes_context=True)
def working_product_switch(context: template.Context) -> SafeString:
    request = context.get("request")
    if request is None or not request.user.is_authenticated or context.get("is_popup"):
        return mark_safe("")
    current = working_product_id(request)
    # Неактивный продукт — только если он уже выбран: иначе выбор пропал бы из списка.
    products = [
        {"pk": pk, "name": name}
        for pk, name, active in products_of(request)
        if active or pk == current
    ]
    if not products:
        return mark_safe("")
    data: dict[str, Any] = {"products": products, "current": current}
    return mark_safe(render_to_string("workspace/working_product.html", data, request=request))
