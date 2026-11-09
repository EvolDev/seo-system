"""`/healthz` — жив ли сайт: проверка контейнера, выкладки и внешнего монитора (E10-01).

Без входа и без данных: «ok», если приложение отвечает и база доступна,
иначе 503. Выкладка (E10-02) после перезапуска ждёт здесь «ok».
"""

import logging

from django.db import DatabaseError, connection
from django.http import HttpRequest, HttpResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

logger = logging.getLogger(__name__)


@never_cache
@require_GET
def healthz(request: HttpRequest) -> HttpResponse:
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except DatabaseError:
        logger.exception("Проверка /healthz: база недоступна")
        return HttpResponse("database unavailable", status=503, content_type="text/plain")
    return HttpResponse("ok", content_type="text/plain")
