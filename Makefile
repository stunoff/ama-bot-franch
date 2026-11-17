.PHONY: up test lint logs stop
up:
	docker compose up -d --build
test:
	python -m pytest -q
lint:
	ruff check ama tests
logs:
	docker compose logs -f --tail=100 api worker
stop:
	docker compose down
