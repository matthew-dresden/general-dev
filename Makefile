
SHELL := /bin/bash

RD_DIR := .devcontainer/remote-docker
CONFIG := $(RD_DIR)/config.env
CONTAINER_SH := $(RD_DIR)/container.sh
TUNNEL_SH := $(RD_DIR)/docker-tunnel.sh
SHELL_SH := $(RD_DIR)/shell.sh
SECRETS_SH := $(RD_DIR)/push-secrets.sh
PROXY_SH := .devcontainer/tinyproxy-daemon.sh
KEYBINDINGS_PY := .devcontainer/vscode-keybindings-install.py
# Where devcontainer_config lives (spec Section 4.5). Named once here so no
# target hardcodes this path inline; PYTHONPATH is set to it, not the
# repository root, because the package is not importable from there.
DEVCONTAINER_SCRIPTS_DIR := .claude/plugins/devcontainer/scripts

PROXY_ENV = set -a; . $(CONFIG); set +a;

LOCAL_CONTEXT = $(shell source $(CONFIG) && echo $$LOCAL_DOCKER_CONTEXT)
REMOTE_CONTEXT = $(shell source $(CONFIG) && echo $$REMOTE_DOCKER_CONTEXT)

UVX ?= uvx
MARKDOWN_LINT ?= $(UVX) pymarkdownlnt --config .pymarkdown.json
SPELL_LINT ?= $(UVX) codespell --builtin clear,rare,en-GB_to_en-US
SHELL_LINT ?= $(UVX) --from shellcheck-py shellcheck
PYTEST ?= uv run --group dev pytest
# repos/ holds clones of other repositories. Their contents are not this
# repo's to lint, and an unparseable file in one of them failed the build here.
LINT_EXCLUDES ?= -not -path './.git/*' -not -path './devbench/*' -not -path './node_modules/*' -not -path './repos/*'
MD_FILES = $(shell find . -name '*.md' $(LINT_EXCLUDES))
SH_FILES = $(shell find . -name '*.sh' $(LINT_EXCLUDES))
JSON_FILES = $(shell find . -name '*.json' $(LINT_EXCLUDES))
# Override to spell-check a set this repo does not own, e.g. docs in a clone
# under repos/:  make lint-spell SPELL_FILES="repos/<name>/*.md"
SPELL_FILES ?= $(MD_FILES)
PRIVATE_FILES ?= shell.env devcontainer-environment-variables.json .devcontainer/aws-profile-map.json

.DEFAULT_GOAL := help
.PHONY: help connect disconnect status shell start stop restart rename check build push-git-creds clean rebuild push-secrets \
        lint lint-md lint-sh lint-dispatch lint-json lint-private lint-nested lint-secrets lint-spell spell-fix format hooks-install hooks-uninstall hooks-run \
        proxy-start proxy-stop proxy-restart proxy-status build-no-cache rebuild-no-cache local remote reopen init up vscode-server \
        keybindings validate test

help:
	@printf '\n\033[1mgeneral-dev\033[0m devcontainer control. Project: \033[1m%s\033[0m   Backend follows the active docker context.\n' "$(notdir $(CURDIR))"
	@printf 'Local engine builds bind-mount this folder. The remote engine clones the repo into a volume on EC2.\n'
	@printf 'Second column: \033[1mboth\033[0m = works on either engine via the active context, \033[1mlocal\033[0m/\033[1mremote\033[0m = that engine only,\n'
	@printf '\033[1mhost\033[0m = runs on this machine and touches no engine at all.\n'
	@printf '\n\033[1mSTART HERE\033[0m\n'
	@printf '  \033[1;36m%-23s\033[0m %-7s %s\n' "make up"               "both"   "Get working from any state: refreshes the tunnel (remote), builds or starts as needed, then opens VS Code."
	@printf '\n\033[1mFIRST RUN\033[0m  once per machine\n'
	@printf '  \033[1;36m%-23s\033[0m %-7s %s\n' "make init"             "host"   "Create the three gitignored config files from their examples. Never overwrites an existing one."
	@printf '  \033[1;36m%-23s\033[0m %-7s %s\n' "make keybindings"      "host"   "Bind Shift+Enter to a newline in VS Code terminals. Must run on the host, not in the container."
	@printf '\n\033[1mENGINE\033[0m  pick where builds and containers live\n'
	@printf '  \033[1;36m%-23s\033[0m %-7s %s\n' "make local"            "host"   "Point docker and VS Code at the local engine ($(LOCAL_CONTEXT)). Nothing remote is stopped."
	@printf '  \033[1;36m%-23s\033[0m %-7s %s\n' "make remote"           "host"   "Point them at the EC2 engine ($(REMOTE_CONTEXT)), refreshing the SSH-over-SSM tunnel first."
	@printf '  \033[1;36m%-23s\033[0m %-7s %s\n' "make connect"          "remote" "What 'make remote' calls. Re-run after a reboot, after sleep, or when SSO expires."
	@printf '  \033[1;36m%-23s\033[0m %-7s %s\n' "make disconnect"       "host"   "What 'make local' calls. Only changes where new commands and windows point."
	@printf '  \033[1;36m%-23s\033[0m %-7s %s\n' "make shell"            "remote" "Interactive zsh on the EC2 host itself, not in a container."
	@printf '\n\033[1mBUILD\033[0m  every target blocks until the container is up and exits non-zero if anything fails\n'
	@printf '  \033[1;36m%-23s\033[0m %-7s %s\n' "make build"            "both"   "Create the container for the active backend. Refuses if one already exists."
	@printf '  \033[1;36m%-23s\033[0m %-7s %s\n' "make rebuild"          "both"   "clean, then build. Prerequisites are checked before anything is destroyed."
	@printf '  \033[1;36m%-23s\033[0m %-7s %s\n' "make build-no-cache"   "both"   "build with the image rebuilt from scratch."
	@printf '  \033[1;36m%-23s\033[0m %-7s %s\n' "make rebuild-no-cache" "both"   "rebuild with the image rebuilt from scratch. Use when a feature or base image changed."
	@printf '  \033[1;36m%-23s\033[0m %-7s %s\n' "make clean"            "both"   "Destroy the container, its private volumes and its image. Shared volumes and the base image are kept."
	@printf '\n\033[1mLIFECYCLE\033[0m\n'
	@printf '  \033[1;36m%-23s\033[0m %-7s %s\n' "make status"           "both"   "Backend, container, image and volumes. Read-only, so start here when something looks wrong."
	@printf '  \033[1;36m%-23s\033[0m %-7s %s\n' "make reopen"           "both"   "Open the container in VS Code. Local opens the folder; remote names the container to attach to."
	@printf '  \033[1;36m%-23s\033[0m %-7s %s\n' "make vscode-server"    "both"   "Fetch the VS Code server this machine needs inside the container. reopen does it for you."
	@printf '  \033[1;36m%-23s\033[0m %-7s %s\n' "make start / stop"     "both"   "Start or stop the container. The checkout survives either way."
	@printf '  \033[1;36m%-23s\033[0m %-7s %s\n' "make restart"          "both"   "Restart in place. Fixes a wedged container without rebuilding anything."
	@printf '  \033[1;36m%-23s\033[0m %-7s %s\n' "make rename NAME=x"    "both"   "Give the container a readable name. New ones are <repo>-<devcontainerId>, which is too long to pick from a list."
	@printf '  \033[1;36m%-23s\033[0m %-7s %s\n' "make check"            "both"   "Remote: report uncommitted or unpushed work in the volume, non-zero when dirty. Local: a no-op, the container shares this folder."
	@printf '\n\033[1mSECRETS AND CREDENTIALS\033[0m\n'
	@printf '  \033[1;36m%-23s\033[0m %-7s %s\n' "make push-secrets"     "remote" "Publish shell.env and aws-profile-map.json to Parameter Store. Remote builds do this when needed."
	@printf '  \033[1;36m%-23s\033[0m %-7s %s\n' "make push-git-creds"   "both"   "Copy this machine's git credentials into the container so it can push with no editor attached."
	@printf '\n\033[1mHOST PROXY\033[0m  only needed behind a corporate proxy; remote builds force HOST_PROXY=false\n'
	@printf '  \033[1;36m%-23s\033[0m %-7s %s\n' "make proxy-start"      "local"  "Run tinyproxy on this machine. Local containers reach it via host.docker.internal."
	@printf '  \033[1;36m%-23s\033[0m %-7s %s\n' "make proxy-status"     "local"  "Whether it is running, and on which port."
	@printf '  \033[1;36m%-23s\033[0m %-7s %s\n' "make proxy-restart"    "local"  "Stop then start, picking up changed settings."
	@printf '  \033[1;36m%-23s\033[0m %-7s %s\n' "make proxy-stop"       "local"  "Stop it. Settings come from $(CONFIG); set HOST_PROXY=true in shell.env to make the container use it."
	@printf '\n\033[1mQUALITY\033[0m\n'
	@printf '  \033[1;36m%-23s\033[0m %-7s %s\n' "make lint"             "host"   "Private files untracked, no nested repos, JSON parses, shellcheck, markdown, US English, staged secrets. Non-zero on any finding."
	@printf '  \033[1;36m%-23s\033[0m %-7s %s\n' "make lint-secrets"     "host"   "Scan staged content for secrets, or RANGE=<a>..<b> for a commit range. Exit 1 on any finding; there is no ignore list."
	@printf '  \033[1;36m%-23s\033[0m %-7s %s\n' "make format"           "host"   "Auto-fix what the markdown tooling can fix, then report what is left."
	@printf '  \033[1;36m%-23s\033[0m %-7s %s\n' "make lint-spell"       "host"   "US English spelling over this repo's markdown. SPELL_FILES overrides the set."
	@printf '  \033[1;36m%-23s\033[0m %-7s %s\n' "make spell-fix"        "host"   "Auto-fix spelling, British-to-American included, in the same set. Rewrites the files."
	@printf '  \033[1;36m%-23s\033[0m %-7s %s\n' "make hooks-install"    "host"   "Install pre-commit and pre-push hooks. Each is one line, 'exec make hooks-run', so they cannot drift."
	@printf '  \033[1;36m%-23s\033[0m %-7s %s\n' "make hooks-run"        "host"   "Exactly what the hooks run. Use it to reproduce a hook failure."
	@printf '  \033[1;36m%-23s\033[0m %-7s %s\n' "make test"             "host"   "Run the hermetic pytest suite in tests/. No docker, no AWS, no network."
	@printf '  \033[1;36m%-23s\033[0m %-7s %s\n' "make validate"         "host"   "The green-baseline contract automation depends on. Runs lint then test."
	@printf '\n\033[1mOPTIONS\033[0m\n'
	@printf '  \033[1;36m%-23s\033[0m %-7s %s\n' "CONTAINER=<name>"      ""       "Pick one instance when several clones of this repo exist. 'make status' lists them."
	@printf '  \033[1;36m%-23s\033[0m %-7s %s\n' "FORCE=1"               ""       "Proceed past the unpushed-work and uncommitted-config guards."
	@printf '  \033[1;36m%-23s\033[0m %-7s %s\n' "SKIP_SECRETS_CHECK=1"  ""       "Do not compare shell.env against Parameter Store, and do not publish it."
	@printf '  \033[1;36m%-23s\033[0m %-7s %s\n' "NO_CACHE=1"            ""       "What the no-cache targets set. Works with build and rebuild directly."
	@printf '\n\033[1mPREREQUISITES\033[0m\n'
	@printf '  %-23s %s\n' "container targets"     "docker"
	@printf '  %-23s %s\n' "remote engine"         "aws, ssh, session-manager-plugin, and REMOTE_INSTANCE_ID in shell.env"
	@printf '  %-23s %s\n' "build and rebuild"     "devcontainer CLI, git, jq, python3      npm install -g @devcontainers/cli"
	@printf '  %-23s %s\n' "lint, test"            "uv                                      brew install uv"
	@printf '  %s\n' "Every target checks what it needs and fails with the command that installs it."
	@printf '\n'

connect:
	@$(TUNNEL_SH)

disconnect:
	@docker context inspect $(LOCAL_CONTEXT) > /dev/null 2>&1 || { \
		printf '\033[0;31m[ERROR]\033[0m docker context "%s" does not exist on this machine.\n' "$(LOCAL_CONTEXT)" >&2; \
		printf 'Set LOCAL_DOCKER_CONTEXT in %s to one of:\n' "$(CONFIG)" >&2; \
		docker context ls --format '  {{.Name}}' >&2; \
		exit 1; \
	}
	@docker context use $(LOCAL_CONTEXT)

shell:
	@$(SHELL_SH)

status:
	@$(CONTAINER_SH) status

start:
	@$(CONTAINER_SH) start

stop:
	@$(CONTAINER_SH) stop

restart:
	@$(CONTAINER_SH) restart

rename:
	@$(CONTAINER_SH) rename

check:
	@$(CONTAINER_SH) check

init:
	@printf '\033[0;36m[INIT]\033[0m creating config files from their examples\n'
	@for target in $(PRIVATE_FILES); do \
		source="$$target.example"; \
		if [ ! -f "$$source" ]; then \
			printf '\033[0;31m[ERROR]\033[0m %s is missing, so %s cannot be created\n' "$$source" "$$target" >&2; \
			exit 1; \
		fi; \
		if [ -f "$$target" ]; then \
			printf '  \033[1;33m%-44s\033[0m already exists, left untouched\n' "$$target"; \
		else \
			cp "$$source" "$$target" || exit 1; \
			printf '  \033[0;32m%-44s\033[0m created\n' "$$target"; \
		fi; \
	done
	@printf '\n'
	@remaining=0; \
	for target in $(PRIVATE_FILES); do \
		[ -f "$$target" ] || continue; \
		n=$$(grep -o '<[^<>]*>' "$$target" 2>/dev/null | sort -u | wc -l | tr -d ' '); \
		if [ "$$n" -gt 0 ]; then \
			printf '\033[1;33m[TODO]\033[0m %s has %s placeholder(s) to replace:\n' "$$target" "$$n"; \
			grep -o '<[^<>]*>' "$$target" | sort -u | sed 's/^/          /'; \
			remaining=$$((remaining + n)); \
		fi; \
	done; \
	if [ "$$remaining" -eq 0 ]; then \
		printf '\033[0;32m[DONE]\033[0m no placeholders left. Next: make local (or make remote), then make build\n'; \
	else \
		printf '\n\033[0;36m[NEXT]\033[0m replace the placeholders above, then: make local (or make remote), then make build\n'; \
		printf '        What each value does: docs/environment-files.md\n'; \
	fi

keybindings:
	@python3 $(KEYBINDINGS_PY)

up:
	@$(CONTAINER_SH) up

build:
	@$(CONTAINER_SH) build

build-no-cache:
	@NO_CACHE=1 $(CONTAINER_SH) build

rebuild-no-cache:
	@NO_CACHE=1 $(CONTAINER_SH) rebuild

local: disconnect
	@printf '\033[0;32m[DONE]\033[0m targeting the local engine, "make build" bind-mounts this folder\n'

remote: connect
	@printf '\033[0;32m[DONE]\033[0m targeting the remote engine, "make build" clones into a volume\n'

reopen:
	@$(CONTAINER_SH) reopen

vscode-server:
	@$(CONTAINER_SH) vscode-server

push-git-creds:
	@$(CONTAINER_SH) push-git-creds

clean:
	@$(CONTAINER_SH) clean

rebuild:
	@$(CONTAINER_SH) rebuild

push-secrets:
	@$(SECRETS_SH)

proxy-start:
	@$(PROXY_ENV) $(PROXY_SH) start

proxy-stop:
	@$(PROXY_ENV) $(PROXY_SH) stop

proxy-restart:
	@$(PROXY_ENV) $(PROXY_SH) restart

proxy-status:
	@$(PROXY_ENV) $(PROXY_SH) status

lint: lint-private lint-nested lint-json lint-sh lint-dispatch lint-md lint-spell lint-secrets
	@printf '\033[0;32m[DONE]\033[0m all checks passed\n'

# The single entry point external automation calls to decide whether this
# checkout is green. Kept separate from lint so that adding a test suite widens
# what "green" means without every caller having to learn a new target name.
validate: lint test
	@printf '\033[0;32m[DONE]\033[0m validate passed\n'

# Host only, hermetic: no docker, no AWS, no network (AC-10.14).
test:
	@printf '\033[0;36m[TEST]\033[0m running pytest suite\n'
	@$(PYTEST) tests

lint-nested:
	@printf '\033[0;36m[LINT]\033[0m no nested repos tracked\n'
	@gitlinks=$$(git ls-files -s | awk '$$1 == 160000 { $$1=""; $$2=""; $$3=""; sub(/^ +/, ""); print }'); \
	if [ -n "$$gitlinks" ]; then \
		printf '\033[0;31m[ERROR]\033[0m these are separate git repositories recorded in this one:\n' >&2; \
		printf '%s\n' "$$gitlinks" | sed 's/^/          /' >&2; \
		printf '        Committing them stores a pointer to another repo, not its contents.\n' >&2; \
		printf '        Untrack:  git rm -r --cached <path>\n' >&2; \
		exit 1; \
	fi
	@printf '  none tracked\n'

lint-md:
	@printf '\033[0;36m[LINT]\033[0m markdown (%s files)\n' "$(words $(MD_FILES))"
	@$(MARKDOWN_LINT) scan $(MD_FILES)

lint-spell:
	@printf '\033[0;36m[LINT]\033[0m spelling, US English (%s files)\n' "$(words $(SPELL_FILES))"
	@if [ -z "$(strip $(SPELL_FILES))" ]; then \
		printf '\033[0;31m[ERROR]\033[0m SPELL_FILES resolved to nothing, so no file would be checked.\n' >&2; \
		printf '        Name the files to check in SPELL_FILES, or unset it to use this repo'"'"'s markdown.\n' >&2; \
		exit 1; \
	fi
	@$(SPELL_LINT) $(SPELL_FILES)

lint-dispatch:
	@printf '\033[0;36m[LINT]\033[0m dispatched commands resolve\n'
	@$(RD_DIR)/lint-dispatch.sh

lint-sh:
	@printf '\033[0;36m[LINT]\033[0m shell (%s files)\n' "$(words $(SH_FILES))"
	@$(SHELL_LINT) -S warning $(SH_FILES)

lint-json:
	@printf '\033[0;36m[LINT]\033[0m json (%s files)\n' "$(words $(JSON_FILES))"
	@python3 .devcontainer/lint-json.py $(JSON_FILES)

lint-private:
	@printf '\033[0;36m[LINT]\033[0m private files not tracked\n'
	@for f in $(PRIVATE_FILES); do \
		if git ls-files --error-unmatch "$$f" > /dev/null 2>&1; then \
			printf '\033[0;31m[ERROR]\033[0m %s is tracked by git, it holds identity/secrets. Untrack it: git rm --cached %s\n' "$$f" "$$f" >&2; \
			exit 1; \
		fi; \
	done
	@printf '  none tracked\n'

# Staged content by default (spec Section 4.6); RANGE=<a>..<b> scans every
# commit in that range instead, oldest first (E2-F1-S2-T1). Exit 1 on any
# finding; there is no ignore list.
lint-secrets:
	@PYTHONPATH=$(DEVCONTAINER_SCRIPTS_DIR) python3 -m devcontainer_config.cli lint-secrets $(if $(RANGE),--range $(RANGE),)

format:
	@printf '\033[0;36m[FORMAT]\033[0m markdown (%s files)\n' "$(words $(MD_FILES))"
	@$(MARKDOWN_LINT) fix $(MD_FILES) || true
	@printf '\033[0;32m[DONE]\033[0m formatted, re-run "make lint" to see what remains\n'

# Separate from format: a dictionary rewrite can be wrong on a proper noun, so
# it stays an explicit request rather than part of the routine formatting pass.
spell-fix:
	@printf '\033[0;36m[FIX]\033[0m spelling, US English (%s files)\n' "$(words $(SPELL_FILES))"
	@if [ -z "$(strip $(SPELL_FILES))" ]; then \
		printf '\033[0;31m[ERROR]\033[0m SPELL_FILES resolved to nothing, so no file would be fixed.\n' >&2; \
		printf '        Name the files to fix in SPELL_FILES, or unset it to use this repo'"'"'s markdown.\n' >&2; \
		exit 1; \
	fi
	@$(SPELL_LINT) -w $(SPELL_FILES)
	@printf '\033[0;32m[DONE]\033[0m fixed what the dictionary maps, re-run "make lint" to see what remains\n'

hooks-run: lint

hooks-install:
	@mkdir -p .git/hooks
	@for hook in pre-commit pre-push; do \
		printf '#!/usr/bin/env sh\n# Installed by "make hooks-install". Runs the same checks as "make hooks-run".\nexec make hooks-run\n' > .git/hooks/$$hook; \
		chmod +x .git/hooks/$$hook; \
		printf '\033[0;32m[DONE]\033[0m installed .git/hooks/%s\n' "$$hook"; \
	done

hooks-uninstall:
	@rm -f .git/hooks/pre-commit .git/hooks/pre-push
	@printf '\033[0;32m[DONE]\033[0m removed pre-commit and pre-push hooks\n'
