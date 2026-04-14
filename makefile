.EXPORT_ALL_VARIABLES:
.ONESHELL:
.SILENT:

SHELL := /bin/bash
.SHELLFLAGS := -euo pipefail -c
MAKEFLAGS += --no-builtin-rules --no-builtin-variables
PATH := $(abspath .venv)/bin:$(PATH)
DATA_DIR := $(HOME)/.mailpilot

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

clean: ## Export data, reset DB schema
	$(eval TS := $(shell date +%Y%m%d-%H%M%S))
	$(call header,Exporting companies and contacts)
	mailpilot company export $(DATA_DIR)/companies-$(TS).json
	mailpilot contact export $(DATA_DIR)/contacts-$(TS).json
	$(call header,Resetting database schema)
	psql postgresql://localhost/mailpilot -c "DROP TABLE IF EXISTS email, campaign, contact, company, account CASCADE"
	mailpilot status > /dev/null

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
