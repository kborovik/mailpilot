.EXPORT_ALL_VARIABLES:
.ONESHELL:
.SILENT:

SHELL := /bin/bash
.SHELLFLAGS := -euo pipefail -c
MAKEFLAGS += --no-builtin-rules --no-builtin-variables
export PATH := $(abspath .venv)/bin:/opt/homebrew/opt/postgresql@18/bin:$(PATH)
DATA_DIR := $(HOME)/.mailpilot
DOCUMENTS_DIR := $(HOME)/Documents/MailPilot

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

db-backup: ## Back up tags, companies and contacts to ~/Documents/MailPilot/ (timestamped JSON snapshot)
	$(eval TS := $(shell date +%Y%m%d-%H%M%S))
	$(call header,Backing up the database snapshot to $(DOCUMENTS_DIR))
	mkdir -p $(DOCUMENTS_DIR)
	mailpilot db export --file $(DOCUMENTS_DIR)/snap-$(TS).json > /dev/null

clean: db-backup ## Back up data, re-create database
	$(call header,Re-creating database)
	dropdb --if-exists mailpilot
	createdb mailpilot
	dropdb --if-exists mailpilot_test
	createdb mailpilot_test
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

config-backup:
	gpg -er E4AFCA7FBB19FC029D519A524AEBB5178D5E96C1 -o config.json.gpg ~/.mailpilot/config.json

env-backup:
	gpg --yes -er E4AFCA7FBB19FC029D519A524AEBB5178D5E96C1 -o .env.gpg .env

###############################################################################
# Release
###############################################################################

install: ## Install mailpilot globally as an editable uv tool
	$(call header,Installing mailpilot via uv tool)
	uv tool install --editable .

# `make release <part>` passes the part as an extra goal; pick it out and
# give the part words no-op recipes so make does not try to build them.
part := $(word 1,$(filter major minor patch,$(MAKECMDGOALS)))

release: ## Bump version, commit, tag, and publish a GitHub release; CI publishes to PyPI (make release major|minor|patch)
	test -n "$(part)" || { echo "usage: make release major|minor|patch"; exit 1; }
	git diff --quiet && git diff --cached --quiet \
		|| { echo "working tree not clean — commit or stash first"; exit 1; }
	$(call header,Bumping $(part) version)
	uv version --bump $(part)
	version=$$(uv version --short)
	git add pyproject.toml uv.lock
	git commit -m "chore: release v$$version"
	git tag "v$$version"
	$(call header,Publishing v$$version to GitHub)
	git push && git push --tags
	gh release create "v$$version" --title "v$$version" --generate-notes
	echo "$(green)Released v$$version — PyPI publish runs in GitHub Actions$(reset)"

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
