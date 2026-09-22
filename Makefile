SHELL := /bin/sh

.PHONY: init up ai down logs stats test rebuild ui

init:
	@test -f .env || cp .env.example .env
	@mkdir -p data

up: init
	docker compose up -d --build nats indexer extractor crawler api scheduler

ai: init
	docker compose --profile ai up -d --build

down:
	docker compose --profile ai down

logs:
	docker compose logs -f --tail=100 crawler extractor indexer api scheduler

stats:
	curl -fsS http://localhost:8080/v1/stats | python3 -m json.tool

ui:
	@echo "Open Search & Discovery UI: http://localhost:8080/"

test:
	docker compose run --rm --entrypoint sh api -c "pip install -q pytest && pytest -q"

rebuild:
	docker compose run --rm --entrypoint python indexer -m cia_brain.cli rebuild
