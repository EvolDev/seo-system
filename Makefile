# Все проверки идут в контейнере: то же окружение, что у приложения.

export APP_UID := $(shell id -u)
export APP_GID := $(shell id -g)

COMPOSE := docker compose
RUN := $(COMPOSE) run --rm app

.PHONY: up down logs restart-queue migrate superuser test check fmt shell docs-check docs-serve

up:  ## Поднять окружение (с пересборкой образа, если менялись зависимости)
	$(COMPOSE) up -d --build

down:  ## Остановить окружение; данные Postgres и очередь Redis сохраняются в томах
	$(COMPOSE) down

logs:  ## Логи всех сервисов
	$(COMPOSE) logs -f

# Воркер и beat не перечитывают код сами, в отличие от runserver. Пересоздаём,
# а не перезапускаем: `restart` оставляет переменные окружения с момента
# создания контейнера, и новый ключ из .env воркер бы не увидел.
restart-queue:  ## Пересоздать воркер и beat — после правки кода задач или .env
	$(COMPOSE) up -d --no-build --force-recreate worker beat

migrate:  ## Применить миграции
	$(RUN) python manage.py migrate

superuser:  ## Создать пользователя для входа в админку
	$(RUN) python manage.py createsuperuser

test:  ## Тесты
	$(RUN) pytest

check:  ## Линтер, форматирование, типы
	$(RUN) sh -c "ruff check . && ruff format --check . && mypy ."

fmt:  ## Автоисправление стиля и форматирование
	$(RUN) sh -c "ruff check --fix . && ruff format ."

shell:  ## Django shell
	$(RUN) python manage.py shell

# Документации не нужны база и Redis: --no-deps их не поднимает.
docs-check:  ## Проверка пользовательской документации и сборка сайта в строгом режиме
	$(COMPOSE) run --rm --no-deps app sh -c \
		"python tools/check_user_docs.py && mkdocs build --strict --site-dir /tmp/site"

docs-serve:  ## Просмотр документации: http://127.0.0.1:8001 (8000 занят приложением)
	$(COMPOSE) run --rm --no-deps -p 127.0.0.1:8001:8001 app mkdocs serve -a 0.0.0.0:8001
