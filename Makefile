BACKEND := backend
UV := uv run --project $(BACKEND)
.PHONY: check lint types test sync web-check test-fast
check: lint types test
sync:
	cd backend && uv sync --quiet
lint: sync
	cd backend && uv run ruff check src tests && uv run ruff format --check src tests
types: sync
	cd backend && uv run mypy
test: sync
	cd backend && PB_PROVIDER_MODE=preview uv run pytest
test-fast: sync
	cd backend && PB_PROVIDER_MODE=preview uv run pytest -m "not slow_matrix" -x
web-check:
	cd web && npm run lint && npm run type-check && npm run build
