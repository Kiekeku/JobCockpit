.PHONY: build dev dev-all dev-mock migrate privacy-scan test typecheck

dev:
	pnpm dev

dev-all:
	pnpm dev:all

dev-mock:
	pnpm dev:mock

migrate:
	uv run python scripts/migrate.py

test:
	uv run pytest

privacy-scan:
	uv run python scripts/privacy_scan.py .

typecheck:
	pnpm typecheck

build:
	pnpm build
