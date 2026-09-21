.DEFAULT_GOAL := check
.PHONY: check package-check audit

check:
	uv lock --check
	uv run --locked ruff format --check .
	uv run --locked ruff check .
	uv run --locked basedpyright
	uv run --locked lint-imports
	uv run --locked pytest
	uv run --locked python tools/check_secrets.py
	git diff --check

package-check:
	uv run --locked python tools/check_package.py

audit:
	uv run --locked python tools/audit_dependencies.py
