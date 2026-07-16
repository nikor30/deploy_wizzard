.PHONY: dev lint test e2e build image run

dev:  ## backend with reload on :8060 + vite dev server (proxies /api)
	uv run uvicorn app.main:app --reload --port 8060 & \
	cd frontend && npm run dev; kill %1

lint:
	uv run ruff check app tests
	uv run ruff format --check app tests
	uv run mypy
	cd frontend && npm run lint

test:
	uv run pytest
	cd frontend && npm run test

e2e:
	@echo "Playwright e2e suite lands in P7 (see PLAN.md)"; exit 1

build:
	cd frontend && npm run build

image:
	podman build -t pnp-bridge:dev -f Containerfile .

run:
	podman run --rm -p 8060:8060 -e PNPB_SECRET_KEY=$${PNPB_SECRET_KEY} -v pnpb-data:/data pnp-bridge:dev
