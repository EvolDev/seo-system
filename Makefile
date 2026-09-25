# Все проверки идут в контейнере: то же окружение, что у приложения.

export APP_UID := $(shell id -u)
export APP_GID := $(shell id -g)

COMPOSE := docker compose
RUN := $(COMPOSE) run --rm app

.PHONY: up down logs migrate superuser test check fmt shell

up:  ## Поднять окружение (с пересборкой образа, если менялись зависимости)
	$(COMPOSE) up -d --build

down:  ## Остановить окружение; данные Postgres сохраняются в томе
	$(COMPOSE) down

logs:  ## Логи всех сервисов
	$(COMPOSE) logs -f

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
