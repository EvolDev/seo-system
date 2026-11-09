"""gunicorn в проде (E10-01, ADR-081): веб-сервер приложения за Caddy.

Читает `gunicorn -c config/gunicorn.py`. Процессы — `WEB_WORKERS`: каждый
держит одно соединение с базой и отвечает на один запрос за раз.
"""

import os

bind = "0.0.0.0:8000"
workers = int(os.environ.get("WEB_WORKERS", "3"))
# Самые долгие запросы — выгрузка «Площадок» целиком в Excel и разбор формы
# загрузки: минуты с запасом, дольше — процесс завис.
timeout = 180
# При перезапуске начатые запросы дорабатывают.
graceful_timeout = 30
# Процесс перезапускается после стольких запросов: память не копится годами.
max_requests = 1000
max_requests_jitter = 100
# Заголовки X-Forwarded-* ставит Caddy; приложение снаружи недоступно.
forwarded_allow_ips = "*"
# Журнал запросов пишет Caddy; ошибки — в stdout.
accesslog = None
errorlog = "-"
