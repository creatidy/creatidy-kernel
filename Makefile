.DEFAULT_GOAL := help
.PHONY: help check package-check audit native-proof

help:
	@printf '%s\n' 'Creatidy Kernel development' \
	  '  make check          Locked tooling, formatting, lint, typing, boundaries, tests and secrets' \
	  '  make package-check  Reproducible wheel/sdist and installed-package smoke checks' \
	  '  make audit          Locked dependency advisory audit (network access required)' \
	  '  make native-proof   Required native verification receipt (explicit pinned bwrap setup)' \
	  '' 'These are developer checks, not live task execution or deployment commands.'

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

native-proof:
	@test -n "$(CREATIDY_TEST_BWRAP_BIN)" || { printf '%s\n' 'Native boundary UNPROVED: CREATIDY_TEST_BWRAP_BIN required'; exit 1; }
	uv run --locked pytest tests/test_bubblewrap_verification.py
