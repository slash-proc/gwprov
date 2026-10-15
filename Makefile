.PHONY: test lint format check

test:
	uv run pytest --cov --cov-report=term-missing

lint:
	uv run ruff check gwprov tests
	uv run black --workers 1 --check gwprov tests

format:
	uv run ruff check --fix gwprov tests
	uv run black --workers 1 gwprov tests

check: lint test
