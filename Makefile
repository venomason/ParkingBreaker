BACKEND := backend
UV := uv run --project $(BACKEND)
PYTHON ?= /usr/bin/python3.12
.PHONY: check lint types test sync web-check test-fast qualification-check
check: lint types test
sync:
	cd backend && uv sync --quiet --python "$(PYTHON)" --no-managed-python
	cd backend && uv run python -c 'import fcntl; assert hasattr(fcntl, "F_OFD_SETLK"), "ParkingBreaker requires a Linux Python build with OFD file locks; set PYTHON to a compatible interpreter"'
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
qualification-check: sync
	PB_PROVIDER_MODE=preview $(UV) python tools/qualification/research_campaign.py --self-test
	PB_PROVIDER_MODE=preview $(UV) python tools/qualification/vision_campaign.py --self-test
