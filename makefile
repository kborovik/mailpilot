.EXPORT_ALL_VARIABLES:
.ONESHELL:
.SILENT:

SHELL := /bin/bash
.SHELLFLAGS := -euo pipefail -c
MAKEFLAGS += --no-builtin-rules --no-builtin-variables
export PATH := $(abspath .venv)/bin:/opt/homebrew/opt/postgresql@18/bin:$(PATH)

default: .venv help

check: lint py-test ## Run lint and tests

lint: py-format py-lint py-types ## Lint Python code

py-test: ## Run tests
	uv run pytest -x

py-types:
	$(call header,Running basedpyright typecheck)
	uv run basedpyright

py-format:
	$(call header,Running Ruff format)
	uv run ruff format

py-lint:
	$(call header,Running Ruff lint)
	uv run ruff check --fix

kill-run: ## Force-kill runaway mailpilot run processes (SIGTERM then SIGKILL)
	$(call header,Force-killing mailpilot run processes)
	pids=$$(pgrep -f 'mailpilot run' 2>/dev/null || true)
	if [[ -n "$$pids" ]]; then
		echo "Sending SIGTERM to pid(s): $$pids"
		kill -TERM $$pids 2>/dev/null || true
		sleep 1
		still=$$(pgrep -f 'mailpilot run' 2>/dev/null || true)
		if [[ -n "$$still" ]]; then
			echo "Sending SIGKILL to pid(s): $$still"
			kill -KILL $$still 2>/dev/null || true
		fi
		echo "$(green)mailpilot run processes cleared$(reset)"
	else
		echo "No mailpilot run processes found"
	fi

clean: kill-run ## Kill runaway run processes, re-create database, restore missing config from pass
	$(call header,Re-creating database)
	dropdb --if-exists mailpilot
	createdb mailpilot
	dropdb --if-exists mailpilot_test
	createdb mailpilot_test
	$(call header,Restoring missing config from pass mailpilot/)
	store="$${PASSWORD_STORE_DIR:-$$HOME/.password-store}/mailpilot"
	if [[ ! -d "$$store" ]]; then
		echo "pass mailpilot/ not found at $$store"
	else
		shopt -s nullglob
		for gpg in "$$store"/*.gpg; do
			key=$$(basename "$$gpg" .gpg)
			if mailpilot config get "$$key" 2>/dev/null | python3 -c 'import json,sys; v=json.load(sys.stdin)["value"]; raise SystemExit(0 if v in (None, "", {}) else 1)'; then
				echo "Setting $$key from pass mailpilot/$$key"
				mailpilot config set "$$key" "$$(pass show "mailpilot/$$key")" > /dev/null
			fi
		done
	fi
	mailpilot status

py-update:
	uv venv --clear && hash -r && uv sync --upgrade

py-reset:
	rm -rf build/ dist/ *.egg-info
	find . -type d -name "__pycache__" -exec rm -rf {} +
	uv venv --clear && hash -r && uv sync --quiet

.venv: uv.lock
	uv venv --clear && hash -r && uv sync

uv.lock: pyproject.toml
	uv lock --upgrade && touch $(@)

.gitignore:
	cat << EOF > $(@)
	**/__pycache__/
	.venv/
	.env
	EOF

###############################################################################
# Release
###############################################################################

install: ## Install mailpilot globally as an editable uv tool with the tui extra
	$(call header,Installing mailpilot via uv tool)
	uv tool install --force --editable '.[tui]'

# `make release <part>` passes the part as an extra goal; pick it out and
# give the part words no-op recipes so make does not try to build them.
part := $(word 1,$(filter major minor patch,$(MAKECMDGOALS)))

release: check ## Bump version, promote CHANGELOG, commit, tag, and push; GitHub Actions runs check then publishes GH release + PyPI (make release major|minor|patch)
	test -n "$(part)" || { echo "usage: make release major|minor|patch"; exit 1; }
	git diff --quiet && git diff --cached --quiet \
		|| { echo "working tree not clean — commit or stash first"; exit 1; }
	$(call header,Checking CHANGELOG Unreleased has shippable bullets)
	./scripts/changelog check
	$(call header,Bumping $(part) version)
	uv version --bump $(part)
	version=$$(uv version --short)
	$(call header,Promoting CHANGELOG Unreleased to v$$version)
	./scripts/changelog promote "$$version"
	git add pyproject.toml uv.lock CHANGELOG.md
	git commit -m "chore: release v$$version"
	git tag "v$$version"
	$(call header,Pushing v$$version tag (CI will check, then publish GH release + PyPI))
	git push && git push --tags
	echo "$(green)Tagged v$$version — GitHub Actions runs make check, then publishes release + PyPI$(reset)"

major minor patch:
	@:

###############################################################################
# Colors and Headers
###############################################################################

TERM := xterm-256color

blue := $$(tput setaf 4)
green := $$(tput setaf 2)
yellow := $$(tput setaf 3)
reset := $$(tput sgr0)

define header
echo "$(blue)==> $(1) <==$(reset)"
endef

help:
	echo "$(blue)Usage: $(green)make [recipe]$(reset)"
	echo "$(blue)Recipes:$(reset)"
	awk 'BEGIN {FS = ":.*?## "; sort_cmd = "sort"} /^[a-zA-Z0-9_-]+:.*?## / \
	{ printf "  \033[33m%-10s\033[0m %s\n", $$1, $$2 | sort_cmd; } \
	END {close(sort_cmd)}' $(MAKEFILE_LIST)
