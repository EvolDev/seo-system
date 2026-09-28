FROM python:3.12-slim

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

COPY . .

EXPOSE 8000
CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]
