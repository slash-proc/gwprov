.PHONY: test lint format check ci

test:
	uv run pytest --cov --cov-report=term-missing

lint:
	uv run ruff check gwprov tests
	uv run black --workers 1 --check gwprov tests

format:
	uv run ruff check --fix gwprov tests
	uv run black --workers 1 gwprov tests

check: lint test

ci:
	@set -eu; \
	work_dir=$$(mktemp -d "$${TMPDIR:-/tmp}/gwprov-ci.XXXXXX"); \
	trap 'rm -rf "$$work_dir"' EXIT; \
	trap 'exit 1' HUP INT TERM; \
	for python_version in 3.10 3.12 3.14; do \
		echo "=== CI checks: Python $$python_version ==="; \
		uv python install "$$python_version"; \
		project_env="$$work_dir/venv-$$python_version"; \
		UV_PROJECT_ENVIRONMENT="$$project_env" uv sync --locked --group dev --python "$$python_version"; \
		UV_PROJECT_ENVIRONMENT="$$project_env" $(MAKE) check; \
	done

