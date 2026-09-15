.PHONY: check test build extension-check install
check:
	uv run ruff check .
	uv run ruff format --check .
	uv run ty check src/
test:
	uv run pytest
build:
	uv build
extension-check:
	cd extension && npm run check
install:
	uv tool install --force .
	cd extension && npm install
