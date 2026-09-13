.PHONY: check test build
check:
	uv run ruff check .
	uv run ruff format --check .
	uv run ty check src/
test:
	uv run pytest
build:
	uv build
