FROM python:3.12-slim AS base

COPY --from=ghcr.io/astral-sh/uv:0.12.19 /uv /uvx /bin/

# Окружение пакетов — в /opt/venv, а не в /app/.venv: /app локально
# монтируется с хоста, и там лежит .venv хоста для PyCharm.
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_DOWNLOADS=never \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    # Баннер Material про MkDocs 2.0 на каждой сборке: версия уже ограничена <2.
    NO_MKDOCS_2_WARNING=1

WORKDIR /app

# Контейнеры идут под пользователем хоста (APP_UID/APP_GID из Makefile). Без
# записи о нём в образе воркер Celery считает процесс root-ом и при старте
# пишет тревожное предупреждение. Номер уже занят в образе — берём как есть.
ARG APP_UID=1000
ARG APP_GID=1000
RUN (getent group "$APP_GID" || groupadd --gid "$APP_GID" app) \
    && (getent passwd "$APP_UID" || useradd --uid "$APP_UID" --gid "$APP_GID" --no-create-home app)

# Сначала только зависимости: слой кэшируется, пока не менялся uv.lock.
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-install-project

# Браузерные проверки экранов — make e2e (E9-09, ADR-046): тот же образ плюс
# Chromium Playwright и его системные библиотеки (около 0,5 ГБ). Отдельная цель:
# make up её не собирает, в приложении браузера нет. Chromium ставится до
# копирования кода, иначе каждая правка кода качала бы его заново.
FROM base AS e2e
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN playwright install --with-deps chromium
COPY . .

# Пользовательская документация для раздела «Документация» (E9-07, ADR-056).
# Сайт собирается при сборке образа: битая ссылка роняет сборку, и сломанная
# документация не доезжает до прода. Своя стадия — пересобирается, только когда
# менялась документация. Порядок важен: mkdocs build очищает папку сайта, карты
# проверщика для интерфейса пишутся после. Папка — в /opt, а не в /app: /app
# локально подменяет папка проекта с хоста, и собранный сайт был бы не виден.
FROM base AS docs
COPY mkdocs.yml ./
COPY user-docs user-docs
COPY tools/check_user_docs.py tools/
COPY docs/06-BACKLOG.md docs/15-USER-DOCS.md docs/
RUN mkdocs build --strict --site-dir /opt/user-docs \
    && python tools/check_user_docs.py \
        --screens-out /opt/user-docs/screens.json \
        --whatsnew-out /opt/user-docs/whatsnew.json

# Приложение — последняя цель, её собирает `docker build` без --target.
FROM base AS app
COPY --from=docs /opt/user-docs /opt/user-docs
COPY . .

EXPOSE 8000
CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]
