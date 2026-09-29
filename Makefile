
SHELL := /bin/bash

RD_DIR := .devcontainer/remote-docker
CONFIG := $(RD_DIR)/config.env
# The instance every remote target acts on. Declared once, passed through to
# each remote recipe rather than resolved at parse time: resolution shells out,
# and doing it at parse time would run on every `make` invocation including
# `make help`, in a repository that may have no instances configured at all.
# Empty by default, so the resolver applies its own four-step order.
INSTANCE ?=

# ENGINE addresses one engine explicitly instead of following the active
# docker context: ENGINE=local names this machine's engine, ENGINE=<name> the
# context of that instance under remote-instances/. Exported when set so the
# container-level scripts read it from their environment -- a plain make
# variable does not cross into a recipe's process -- and both `ENGINE=x make
# <target>` and `make <target> ENGINE=x` reach them. Empty or unset, every
# target behaves exactly as before and follows the active context.
ifdef ENGINE
export ENGINE
endif

CONTAINER_SH := $(RD_DIR)/container.sh
SECRETS_SH := $(RD_DIR)/push-secrets.sh
CERTS_SH := $(RD_DIR)/certs.sh
PROXY_SH := .devcontainer/tinyproxy-daemon.sh
KEYBINDINGS_PY := .devcontainer/vscode-keybindings-install.py
# Where devcontainer_config lives (spec Section 4.5). Named once here so no
# target hardcodes this path inline; PYTHONPATH is set to it, not the
# repository root, because the package is not importable from there.
DEVCONTAINER_SCRIPTS_DIR := .claude/plugins/devcontainer/scripts

# Usage guard for a target that acts on exactly one instance: prints its
# usage lines and exits 2 when INSTANCE is empty. $(1) is the target name,
# so the usage line shows the target it came from.
define INSTANCE_USAGE_GUARD
if [ -z "$(INSTANCE)" ]; then \
	printf '\033[0;31m[ERROR]\033[0m INSTANCE is required, e.g.: make $(1) INSTANCE=<instance-name>\n' >&2; \
	printf '        Instance names are project names (e.g. brimbooks), never geographies or stages.\n' >&2; \
	printf '        See what exists: make list-instances\n' >&2; \
	exit 2; \
fi
endef

# Usage guard for a target that acts on one instance or, with ALL=1, every
# configured one: usage lines and exit 2 when neither is given, and a
# refusal when both are, because silently preferring one spelling of "every
# instance but also this one" would run something the caller did not ask for.
define INSTANCE_OR_ALL_GUARD
if [ -z "$(INSTANCE)" ] && [ "$(ALL)" != "1" ]; then \
	printf '\033[0;31m[ERROR]\033[0m INSTANCE is required (or ALL=1 for every instance), e.g.: make $(1) INSTANCE=<instance-name>\n' >&2; \
	printf '        Instance names are project names (e.g. brimbooks), never geographies or stages.\n' >&2; \
	printf '        See what exists: make list-instances\n' >&2; \
	exit 2; \
fi; \
if [ -n "$(INSTANCE)" ] && [ "$(ALL)" = "1" ]; then \
	printf '\033[0;31m[ERROR]\033[0m pass INSTANCE=<name> or ALL=1 to $(1), not both\n' >&2; \
	exit 2; \
fi
endef

# Refusal for the three targets that switch the machine-wide docker context
# (local, remote, disconnect). ENGINE, when set, exists so a run never has
# to: every docker call it makes is aimed at the engine it names without
# touching the context other terminals share. Running a context switcher
# under ENGINE is therefore a contradiction -- silently performing the
# switch would betray exactly what the caller asked ENGINE for -- so the
# target refuses with the reason and exits 2, matching the usage guards'
# convention. $(1) is the target name, for the message.
define ENGINE_CONTEXT_SWITCH_REFUSAL
if [ -n "$${ENGINE:-}" ]; then \
	printf '\033[0;31m[ERROR]\033[0m ENGINE=%s is set: make $(1) switches the machine-wide docker context, which ENGINE exists to avoid; run it without ENGINE\n' "$${ENGINE}" >&2; \
	exit 2; \
fi
endef

# The instance names `instances.discover` reports, one per line, sorted --
# the engine's own discovery (files and _envcommon filtered out), not a
# second shell reimplementation of the same rule. Consumed by every ALL=1
# loop below; an empty listing is handled by the loop preamble, not here.
DISCOVER_INSTANCE_NAMES = PYTHONPATH=$(DEVCONTAINER_SCRIPTS_DIR) python3 -c "from pathlib import Path; from devcontainer_config import instances, repo; print('\n'.join(instances.discover(repo.find_root(Path.cwd()))))"

# REMOTE_AWS_REGION is required, never defaulted, in every target that names
# a region -- the Terragrunt helper and the power/destroy targets alike.
# root.hcl derives the fleet's shared state bucket's name from this variable,
# so a silently substituted default would point a whole run's state at a
# bucket belonging to another region instead of failing, and no region is a
# safe guess. Expanded into each recipe that needs the region, so the
# requirement is stated once and enforced everywhere (fail-fast, per
# CLAUDE.md).
define REMOTE_AWS_REGION_GUARD
: "$${REMOTE_AWS_REGION:?REMOTE_AWS_REGION must be set (no default: root.hcl names the state bucket from it)}";
endef

# Per-instance shell helpers, inlined into each recipe that runs Terragrunt
# (make expands them textually; the shell sees one function definition per
# run). tg_init <name> cds into the instance's directory -- always from
# $(CURDIR), so consecutive iterations never nest relative paths -- and
# inits non-interactively. The backend-bootstrap retry exists for the very
# first init in a fresh account: the fleet's shared remote-state bucket does
# not exist yet and Terragrunt wants a y/n confirmation no non-interactive
# pipe can answer proactively. The retry is attempted only when the failed
# init's output names the missing bucket -- Terragrunt's own "Remote state
# bucket ... does not exist" wording, matched with both phrases
# case-insensitively -- so any other init failure (a versioning refusal, an
# AccessDenied on an existing bucket, anything else) prints its log and
# stops the run at the step that failed instead of triggering a bootstrap
# that would answer the wrong question.
#
# REMOTE_AWS_REGION has no default here (see REMOTE_AWS_REGION_GUARD): the
# helper requires it fail-fast before the first Terragrunt call, because
# root.hcl derives the shared state bucket's name from it.
define TERRAGRUNT_INIT_HELPER
tg_init() { \
	name="$$1"; \
	dir="$(CURDIR)/remote-instances/$$name"; \
	if [ ! -f "$$dir/terragrunt.hcl" ]; then \
		printf '\033[0;31m[ERROR]\033[0m no instance directory at remote-instances/%s\n' "$$name" >&2; \
		printf '        Scaffold it first: make instance-init INSTANCE=%s\n' "$$name" >&2; \
		exit 1; \
	fi; \
	cd "$$dir" || exit 1; \
	$(REMOTE_AWS_REGION_GUARD) \
	export TG_NON_INTERACTIVE=true; \
	if ! init_log=$$(terragrunt init -input=false 2>&1); then \
		printf '%s\n' "$$init_log" >&2; \
		if printf '%s' "$$init_log" | grep -qi 'remote state bucket' && \
		   printf '%s' "$$init_log" | grep -qi 'does not exist'; then \
			echo y | terragrunt init --backend-bootstrap -input=false || exit 1; \
		else \
			exit 1; \
		fi; \
	fi; \
}
endef

PROXY_ENV = set -a; . $(CONFIG); set +a;

LOCAL_CONTEXT = $(shell source $(CONFIG) && echo $$LOCAL_DOCKER_CONTEXT)
REMOTE_CONTEXT = $(shell source $(CONFIG) && echo $$REMOTE_DOCKER_CONTEXT)

UVX ?= uvx
# Every `uv run` this Makefile performs (PYTEST, and the hooks that exec it)
# must keep its virtual environment OUTSIDE the repository. The workspace
# directory is bind-mounted into the devcontainer as-is, and a host-OS
# `.venv` inside it is what the container's Python/Ruff extensions resolve
# as the workspace interpreter -- its symlinks point at a macOS Python that
# does not exist in the Linux container, so every window open logs
# `spawn .venv/bin/python ENOENT` before falling back to the bundled tools.
# Redirecting uv's project environment removes the cause instead of
# suppressing the symptom; a developer who already exports their own
# UV_PROJECT_ENVIRONMENT keeps it.
export UV_PROJECT_ENVIRONMENT ?= $(HOME)/.venvs/$(notdir $(CURDIR))
MARKDOWN_LINT ?= $(UVX) pymarkdownlnt --config .pymarkdown.json
SPELL_LINT ?= $(UVX) codespell --builtin clear,rare,en-GB_to_en-US
SHELL_LINT ?= $(UVX) --from shellcheck-py shellcheck
PYTEST ?= uv run --group dev pytest
# E3-F2-S2-T5 AC-DOC-001 / AC-FUNC-001: the `test` target's host prerequisite
# tools and each one's install command, defined once so the PREREQUISITES
# help row and the `test:` recipe's fail-fast guard read the same value and
# can never document or print different remediation for the same tool.
TEST_PREREQUISITE_TOOLS := uv zsh
TEST_INSTALL_HINT_uv := brew install uv
TEST_INSTALL_HINT_zsh := brew install zsh (macOS) or sudo apt-get install -y zsh (Linux, WSL)
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
# The hostcreds manifest is private like the PRIVATE_FILES entries (make init
# creates it from its committed example; lint-private refuses to let it be
# tracked) but it is NOT rendered from setup answers -- the operator and
# 'make creds-init' own its content -- so it stays out of PRIVATE_FILES,
# which tests/test_private_files_consistency.py pins to
# devcontainer_config.repo.PRIVATE_FILES (the files render/verify own). The
# two PRIVATE_FILES consumers that must also cover the manifest iterate this
# union instead.
PRIVATE_FILES_AND_MANIFEST ?= $(PRIVATE_FILES) .devcontainer/hostcreds.map.json

.DEFAULT_GOAL := help
.PHONY: help connect disconnect status exec shell start stop restart rename check build push-creds creds-init verify-container clean rebuild push-secrets \
        lint lint-md lint-sh lint-dispatch lint-json lint-private lint-nested lint-workspace lint-secrets lint-spell spell-fix format hooks-install hooks-uninstall hooks-run hooks-run-push \
        proxy-start proxy-stop proxy-restart proxy-status build-no-cache rebuild-no-cache local remote reopen init up vscode-server \
        keybindings validate test cert-status list-instances instance-init instance-plan instance-deploy instance-status instance-stop instance-start instance-destroy instance-link

help:
	@printf '\n\033[1m%s\033[0m devcontainer control.   Backend follows the active docker context.\n' "$(notdir $(CURDIR))"
	@printf 'Local engine builds bind-mount this folder. The remote engine clones the repo into a volume on EC2.\n'
	@printf 'Second column: \033[1mboth\033[0m = works on either engine via the active context, \033[1mlocal\033[0m/\033[1mremote\033[0m = that engine only,\n'
	@printf '\033[1mhost\033[0m = runs on this machine and touches no engine at all.\n'
	@printf '\n\033[1mSTART HERE\033[0m\n'
	@printf '  \033[1;36m%-23s\033[0m %-7s %s\n' "make up"               "both"   "Get working from any state: refreshes the tunnel (remote), builds or starts as needed, then opens VS Code."
	@printf '\n\033[1mFIRST RUN\033[0m  once per machine\n'
	@printf '  \033[1;36m%-23s\033[0m %-7s %s\n' "make init"             "host"   "Create the gitignored config files (incl. the hostcreds manifest) from their examples. Never overwrites an existing one."
	@printf '  \033[1;36m%-23s\033[0m %-7s %s\n' "make keybindings"      "host"   "Bind Shift+Enter to a newline in VS Code terminals. Must run on the host, not in the container."
	@printf '\n\033[1mENGINE\033[0m  pick where builds and containers live\n'
	@printf '  \033[1;36m%-23s\033[0m %-7s %s\n' "make local"            "host"   "Point docker and VS Code at the local engine ($(LOCAL_CONTEXT)). Nothing remote is stopped."
	@printf '  \033[1;36m%-23s\033[0m %-7s %s\n' "make disconnect"       "host"   "What 'make local' calls. Only changes where new commands and windows point."
	@printf '\n\033[1mINSTANCES\033[0m  one remote engine per project under remote-instances/; instance names are project names (e.g. brimbooks), never geographies or stages\n'
	@printf '  \033[1;36m%-23s\033[0m %-7s %s\n' "make list-instances"   "host"   "List every instance with live status: EC2 state, id, params, certs, forward, context."
	@printf '  \033[1;36m%-23s\033[0m %-7s %s\n' "make instance-init"    "host"   "Scaffold <project>'s directory; never deploys. INSTANCE=<instance-name> [AMI=] [REGION= for the AMI/AZ lookup only; the deployment region is REMOTE_AWS_REGION]"
	@printf '  \033[1;36m%-23s\033[0m %-7s %s\n' "make instance-deploy"  "remote" "Converge: provision, link id, trust chain if missing, push secrets. Refuses instance replacement without CONFIRM=replace. INSTANCE= | ALL=1"
	@printf '  \033[1;36m%-23s\033[0m %-7s %s\n' "make instance-status"  "host"   "One instance's live state, or every instance with ALL=1. INSTANCE=<instance-name> | ALL=1"
	@printf '  \033[1;36m%-23s\033[0m %-7s %s\n' "make instance-plan"    "remote" "Terragrunt plan per instance; bootstraps the shared state bucket on first run. INSTANCE=<instance-name> | ALL=1"
	@printf '  \033[1;36m%-23s\033[0m %-7s %s\n' "make instance-stop"    "remote" "Stop the EC2 instance and wait until it reports stopped. INSTANCE=<instance-name> | ALL=1"
	@printf '  \033[1;36m%-23s\033[0m %-7s %s\n' "make instance-start"   "remote" "Start it again and wait for its SSM agent to report ready. INSTANCE=<instance-name> | ALL=1"
	@printf '  \033[1;36m%-23s\033[0m %-7s %s\n' "make instance-destroy" "remote" "Destroy + cleanup params, certs, context, id. CONFIRM=destroy only for ALL=1. INSTANCE=<instance-name> | ALL=1"
	@printf '  \033[1;36m%-23s\033[0m %-7s %s\n' "make instance-link"    "host"   "Save the instance's EC2 id for other targets. Deploy does this automatically; run it only after re-provisioning outside make. INSTANCE_ID=<id>"
	@printf '  \033[1;36m%-23s\033[0m %-7s %s\n' "make cert-ca"          "host"   "Create this instance's certificate authority. Once per instance; refuses if one exists."
	@printf '  \033[1;36m%-23s\033[0m %-7s %s\n' "make cert-client"      "host"   "Issue the client certificate 'make connect' presents. Run after cert-ca, and again at renewal."
	@printf '  \033[1;36m%-23s\033[0m %-7s %s\n' "make cert-publish"     "remote" "Issue server material and publish it to Parameter Store. The daemon needs it to open its listener."
	@printf '  \033[1;36m%-23s\033[0m %-7s %s\n' "make cert-install"     "remote" "Have the instance fetch the published material and start its daemon. Run after cert-publish."
	@printf '  \033[1;36m%-23s\033[0m %-7s %s\n' "make cert-status"      "host"   "Client and CA expiry per instance."
	@printf '  \033[1;36m%-23s\033[0m %-7s %s\n' "make push-secrets"     "remote" "Publish shell.env and aws-profile-map.json to Parameter Store. Remote builds do this when needed."
	@printf '  \033[1;36m%-23s\033[0m %-7s %s\n' "make connect"          "remote" "What 'make remote' calls. Opens the forward for INSTANCE=<name> (or ENGINE=<name>); re-run after a reboot, after sleep, or when SSO expires."
	@printf '  \033[1;36m%-23s\033[0m %-7s %s\n' "make remote"           "host"   "Point them at the EC2 engine ($(REMOTE_CONTEXT)), refreshing the SSM port forward first. INSTANCE=<name> targets that instance."
	@printf '\n\033[1mBUILD\033[0m  every target blocks until the container is up and exits non-zero if anything fails\n'
	@printf '  \033[1;36m%-23s\033[0m %-7s %s\n' "make build"            "both"   "Create the container for the active backend. Refuses if one already exists."
	@printf '  \033[1;36m%-23s\033[0m %-7s %s\n' "make rebuild"          "both"   "clean, then build. Prerequisites are checked before anything is destroyed."
	@printf '  \033[1;36m%-23s\033[0m %-7s %s\n' "make build-no-cache"   "both"   "build with the image rebuilt from scratch."
	@printf '  \033[1;36m%-23s\033[0m %-7s %s\n' "make rebuild-no-cache" "both"   "rebuild with the image rebuilt from scratch. Use when a feature or base image changed."
	@printf '  \033[1;36m%-23s\033[0m %-7s %s\n' "make clean"            "both"   "Destroy the container, its private volumes and its image. Shared volumes and the base image are kept."
	@printf '\n\033[1mLIFECYCLE\033[0m\n'
	@printf '  \033[1;36m%-23s\033[0m %-7s %s\n' "make status"           "both"   "Backend, container, image and volumes. Read-only, so start here when something looks wrong."
	@printf '  \033[1;36m%-23s\033[0m %-7s %s\n' "make reopen"           "both"   "Open the container in VS Code. Local opens the folder; remote names the container to attach to."
	@printf '  \033[1;36m%-23s\033[0m %-7s %s\n' "make exec"             "both" "Interactive shell inside the container. CONTAINER_SHELL picks which one."
	@printf '  \033[1;36m%-23s\033[0m %-7s %s\n' "make vscode-server"    "both"   "Fetch the VS Code server this machine needs inside the container. reopen does it for you."
	@printf '  \033[1;36m%-23s\033[0m %-7s %s\n' "make start / stop"     "both"   "Start or stop the container. The checkout survives either way."
	@printf '  \033[1;36m%-23s\033[0m %-7s %s\n' "make restart"          "both"   "Restart in place. Fixes a wedged container without rebuilding anything."
	@printf '  \033[1;36m%-23s\033[0m %-7s %s\n' "make rename NAME=x"    "both"   "Give the container a readable name. New ones are <repo>-<devcontainerId>, which is too long to pick from a list."
	@printf '  \033[1;36m%-23s\033[0m %-7s %s\n' "make check"            "both"   "Remote: report uncommitted or unpushed work in the volume, non-zero when dirty. Local: a no-op, the container shares this folder."
	@printf '\n\033[1mSECRETS AND CERTIFICATES\033[0m\n'
	@printf '  \033[1;36m%-23s\033[0m %-7s %s\n' "make push-creds"       "both"   "Resolve every hostcreds manifest entry on this machine and push it into the container. Git entries also seed ~/.git-credentials."
	@printf '  \033[1;36m%-23s\033[0m %-7s %s\n' "make creds-init"       "host"   "Prompt once per missing keychain item the hostcreds manifest names and store it. CREDS_INIT_ARGS='--stdin NAME' feeds one value from stdin."
	@printf '  \033[1;36m%-23s\033[0m %-7s %s\n' "make verify-container" "both"   "Check the pushed credentials inside the container: fragment modes, startup block, git and aws reachability."
	@printf '\n\033[1mHOST PROXY\033[0m  only needed behind a corporate proxy; remote builds force HOST_PROXY=false\n'
	@printf '  \033[1;36m%-23s\033[0m %-7s %s\n' "make proxy-start"      "local"  "Run tinyproxy on this machine. Local containers reach it via host.docker.internal."
	@printf '  \033[1;36m%-23s\033[0m %-7s %s\n' "make proxy-status"     "local"  "Whether it is running, and on which port."
	@printf '  \033[1;36m%-23s\033[0m %-7s %s\n' "make proxy-restart"    "local"  "Stop then start, picking up changed settings."
	@printf '  \033[1;36m%-23s\033[0m %-7s %s\n' "make proxy-stop"       "local"  "Stop it. Settings come from $(CONFIG); set HOST_PROXY=true in shell.env to make the container use it."
	@printf '\n\033[1mQUALITY\033[0m\n'
	@printf '  \033[1;36m%-23s\033[0m %-7s %s\n' "make lint"             "host"   "Private files untracked, no nested repos, no host venv in the workspace, JSON parses, shellcheck, markdown, US English, staged secrets. Non-zero on any finding."
	@printf '  \033[1;36m%-23s\033[0m %-7s %s\n' "make lint-secrets"     "host"   "Scan staged content for secrets, or RANGE=<a>..<b> for a commit range. Exit 1 on any finding; there is no ignore list."
	@printf '  \033[1;36m%-23s\033[0m %-7s %s\n' "make format"           "host"   "Auto-fix what the markdown tooling can fix, then report what is left."
	@printf '  \033[1;36m%-23s\033[0m %-7s %s\n' "make lint-spell"       "host"   "US English spelling over this repo's markdown. SPELL_FILES overrides the set."
	@printf '  \033[1;36m%-23s\033[0m %-7s %s\n' "make spell-fix"        "host"   "Auto-fix spelling, British-to-American included, in the same set. Rewrites the files."
	@printf '  \033[1;36m%-23s\033[0m %-7s %s\n' "make hooks-install"    "host"   "Install pre-commit and pre-push hooks via devcontainer_config.githooks. Refuses to clobber a hook it did not write."
	@printf '  \033[1;36m%-23s\033[0m %-7s %s\n' "make hooks-run"        "host"   "Exactly what pre-commit runs. Use it to reproduce a pre-commit failure."
	@printf '  \033[1;36m%-23s\033[0m %-7s %s\n' "make hooks-run-push"   "host"   "Exactly what pre-push runs: lint, then a secrets scan of every commit in the pushed range."
	@printf '  \033[1;36m%-23s\033[0m %-7s %s\n' "make test"             "host"   "Run the hermetic pytest suite in tests/. No docker, no AWS, no network."
	@printf '  \033[1;36m%-23s\033[0m %-7s %s\n' "make validate"         "host"   "The green-baseline contract automation depends on. Runs lint then test."
	@printf '\n\033[1mOPTIONS\033[0m\n'
	@printf '  \033[1;36m%-23s\033[0m %-7s %s\n' "ENGINE=local|<name>"   ""       "Address one engine explicitly (ENGINE=x make <target>, or make <target> ENGINE=x): parallel terminals can drive local and remote engines concurrently, without switching contexts. Unset follows the active context."
	@printf '  \033[1;36m%-23s\033[0m %-7s %s\n' "CONTAINER=<name>"      ""       "Pick one instance when several clones of this repo exist. 'make status' lists them."
	@printf '  \033[1;36m%-23s\033[0m %-7s %s\n' "FORCE=1"               ""       "Proceed past the unpushed-work and uncommitted-config guards."
	@printf '  \033[1;36m%-23s\033[0m %-7s %s\n' "SKIP_SECRETS_CHECK=1"  ""       "Do not compare shell.env against Parameter Store, and do not publish it."
	@printf '  \033[1;36m%-23s\033[0m %-7s %s\n' "NO_CACHE=1"            ""       "What the no-cache targets set. Works with build and rebuild directly."
	@printf '\n\033[1mPREREQUISITES\033[0m\n'
	@printf '  %-23s %s\n' "container targets"     "docker"
	@printf '  %-23s %s\n' "remote engine"         "aws, session-manager-plugin, and the instance id recorded by instance-deploy (make instance-link)"
	@printf '  %-23s %s\n' "build and rebuild"     "devcontainer CLI, git, jq, python3      npm install -g @devcontainers/cli"
	@printf '  %-23s %s\n' "lint"                  "uv                                      brew install uv"
	@printf '  %-23s %s\n' "test"                  "uv, zsh                                 uv: $(TEST_INSTALL_HINT_uv)   zsh: $(TEST_INSTALL_HINT_zsh)"
	@printf '  %s\n' "Every target checks what it needs and fails with the command that installs it."
	@printf '\n'

# Opens the SSM port forward. The instance it opens one for: INSTANCE when
# given, else ENGINE when it names an instance, else none -- the resolver's
# default, addressed through the parse-time config values exactly as before.
# A named target is resolved here through the same lib.sh resolver every
# remote entry point uses (which reads the per-instance id store and derives
# the context the way the rest of the repo does), because the forwarding
# remedy this target backs -- `make remote INSTANCE=<name>` -- was hollow
# otherwise: the variable was accepted and ignored, and the forward for
# whatever instance the resolver defaulted to opened regardless of what was
# asked for. INSTANCE and ENGINE naming different engines is refused by the
# resolver's own guard before anything opens.
connect:
	@set -euo pipefail; \
	$(PROXY_ENV) \
	transport="$${DEVCONTAINER_TRANSPORT:-ssm}"; \
	target=""; \
	if [ -n "$(INSTANCE)" ]; then target="$(INSTANCE)"; \
	elif [ -n "$${ENGINE:-}" ] && [ "$${ENGINE}" != "local" ]; then target="$${ENGINE}"; fi; \
	if [ -n "$$target" ]; then \
		. $(RD_DIR)/lib.sh; \
		INSTANCE="$$target" rd_resolve_instance; \
		ctx="$$(rd_engine_context)"; \
		[ -n "$$ctx" ] || ctx="$${DOCKER_CONTEXT:-}"; \
		if [ -z "$${REMOTE_INSTANCE_ID:-}" ]; then \
			printf '\033[0;31m[ERROR]\033[0m no EC2 instance id is recorded for instance %s\n' "$$target" >&2; \
			printf '        Link it first: make instance-link INSTANCE=%s\n' "$$target" >&2; \
			exit 1; \
		fi; \
		set -- --instance-id "$$REMOTE_INSTANCE_ID" --context "$$ctx"; \
	else \
		set -- --instance-id "$$REMOTE_INSTANCE_ID" --context "$(REMOTE_CONTEXT)"; \
	fi; \
	case "$$transport" in \
		ssm) PYTHONPATH=$(DEVCONTAINER_SCRIPTS_DIR) python3 -m devcontainer_config.transport connect \
			"$$@" --profile "$$REMOTE_AWS_PROFILE" --region "$$REMOTE_AWS_REGION" ;; \
		*) printf '\033[0;31m[ERROR]\033[0m DEVCONTAINER_TRANSPORT="%s" is not recognized.\n' "$$transport" >&2; \
		   printf '        Accepted value: ssm. The ssh transport was removed at cutover.\n' >&2; \
		   exit 1 ;; \
	esac

disconnect:
	@$(call ENGINE_CONTEXT_SWITCH_REFUSAL,disconnect)
	@docker context inspect $(LOCAL_CONTEXT) > /dev/null 2>&1 || { \
		printf '\033[0;31m[ERROR]\033[0m docker context "%s" does not exist on this machine.\n' "$(LOCAL_CONTEXT)" >&2; \
		printf 'Set LOCAL_DOCKER_CONTEXT in %s to one of:\n' "$(CONFIG)" >&2; \
		docker context ls --format '  {{.Name}}' >&2; \
		exit 1; \
	}
	@docker context use $(LOCAL_CONTEXT)

status:
	@INSTANCE="$(INSTANCE)" $(CONTAINER_SH) status

start:
	@INSTANCE="$(INSTANCE)" $(CONTAINER_SH) start

stop:
	@INSTANCE="$(INSTANCE)" $(CONTAINER_SH) stop

restart:
	@INSTANCE="$(INSTANCE)" $(CONTAINER_SH) restart

rename:
	@INSTANCE="$(INSTANCE)" $(CONTAINER_SH) rename

check:
	@INSTANCE="$(INSTANCE)" $(CONTAINER_SH) check

init:
	@printf '\033[0;36m[INIT]\033[0m creating config files from their examples\n'
	@for target in $(PRIVATE_FILES_AND_MANIFEST); do \
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
	for target in $(PRIVATE_FILES_AND_MANIFEST); do \
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
	@INSTANCE="$(INSTANCE)" $(CONTAINER_SH) up

build:
	@INSTANCE="$(INSTANCE)" $(CONTAINER_SH) build

build-no-cache:
	@INSTANCE="$(INSTANCE)" NO_CACHE=1 $(CONTAINER_SH) build

rebuild-no-cache:
	@INSTANCE="$(INSTANCE)" NO_CACHE=1 $(CONTAINER_SH) rebuild

# The refusal fires in the prerequisite first (make builds disconnect before
# local's own recipe can run), so under ENGINE `make local` stops before any
# context is touched; the guard line here keeps this target refusing on its
# own should the dependency ever move.
local: disconnect
	@$(call ENGINE_CONTEXT_SWITCH_REFUSAL,local)
	@printf '\033[0;32m[DONE]\033[0m targeting the local engine, "make build" bind-mounts this folder\n'

# The guard runs before the connect prerequisite deliberately: `make remote`
# under ENGINE must refuse before any forward is opened, and connect itself
# must keep honoring ENGINE (it is how a single forward is refreshed for one
# engine without switching anything). So remote does not hang connect off its
# prerequisite list any more; it guards, then delegates through a sub-make.
# INSTANCE needs no explicit hand-off: a command-line definition travels to
# sub-makes inside MAKEFLAGS, and the environment spelling travels in the
# recipe's own environment -- both spellings reach connect's recipe either way.
remote:
	@$(call ENGINE_CONTEXT_SWITCH_REFUSAL,remote)
	@$(MAKE) --no-print-directory connect
	@printf '\033[0;32m[DONE]\033[0m targeting the remote engine, "make build" clones into a volume\n'

reopen:
	@INSTANCE="$(INSTANCE)" $(CONTAINER_SH) reopen

exec:
	@INSTANCE="$(INSTANCE)" $(CONTAINER_SH) exec

# Every instance-* target is a thin loop over the devcontainer_config.cli
# subcommands of the same name (the engine is devcontainer_config.instance_ops,
# spec Section 4.5): discovery, naming, addressing and the aws/docker calls
# all live there, and this layer only decides WHICH instance or instances to
# act on. list-instances is the one no-argument member: it always lists every
# configured instance, so INSTANCE= and ALL=1 are simply irrelevant to it.
list-instances:
	@PYTHONPATH=$(DEVCONTAINER_SCRIPTS_DIR) python3 -m devcontainer_config.cli instance-list

# Writes remote-instances/<name>/terragrunt.hcl and its guidance. Never
# deploys; the cli prints the guidance messages verbatim. AMI= and REGION=
# pass through when given; REGION is the scaffold-time AMI/AZ lookup ONLY --
# it never selects where anything deploys. The deployment region is
# REMOTE_AWS_REGION, required without a default by every Terragrunt-running
# target (REMOTE_AWS_REGION_GUARD above), because root.hcl names the shared
# state bucket from it.
instance-init:
	@$(call INSTANCE_USAGE_GUARD,instance-init)
	@PYTHONPATH=$(DEVCONTAINER_SCRIPTS_DIR) python3 -m devcontainer_config.cli instance-init "$(INSTANCE)" $(if $(REGION),--region $(REGION),) $(if $(AMI),--ami $(AMI),)

# Read-only: one instance's live state, or every instance's with ALL=1. The
# cli exits non-zero when any probe failed, so a loop abort names the
# instance whose surface could not be reached.
instance-status:
	@$(call INSTANCE_OR_ALL_GUARD,instance-status)
	@set -euo pipefail; \
	if [ "$(ALL)" = "1" ]; then names=$$($(DISCOVER_INSTANCE_NAMES)); else names="$(INSTANCE)"; fi; \
	[ -n "$$names" ] || { printf 'No instances configured under remote-instances/; nothing to report.\n'; exit 0; }; \
	while IFS= read -r name; do \
		PYTHONPATH=$(DEVCONTAINER_SCRIPTS_DIR) python3 -m devcontainer_config.cli instance-status "$$name" || exit 1; \
	done <<< "$$names"

# Dry run: init (bootstrapping the shared state bucket on the very first
# run, see TERRAGRUNT_INIT_HELPER), then plan. Never applies anything.
instance-plan:
	@$(call INSTANCE_OR_ALL_GUARD,instance-plan)
	@set -euo pipefail; \
	$(TERRAGRUNT_INIT_HELPER); \
	if [ "$(ALL)" = "1" ]; then names=$$($(DISCOVER_INSTANCE_NAMES)); else names="$(INSTANCE)"; fi; \
	[ -n "$$names" ] || { printf 'No instances configured under remote-instances/; nothing to plan.\n'; exit 0; }; \
	while IFS= read -r name; do \
		printf '\033[0;36m[PLAN]\033[0m %s\n' "$$name"; \
		tg_init "$$name"; \
		terragrunt plan || exit 1; \
	done <<< "$$names"

# Power: stop or start, waiting for the target state (and, on start, for the
# SSM agent) before reporting done. Abort-on-fail so a half-powered fleet
# never looks converged.
instance-stop:
	@$(call INSTANCE_OR_ALL_GUARD,instance-stop)
	@set -euo pipefail; \
	$(REMOTE_AWS_REGION_GUARD) \
	if [ "$(ALL)" = "1" ]; then names=$$($(DISCOVER_INSTANCE_NAMES)); else names="$(INSTANCE)"; fi; \
	[ -n "$$names" ] || { printf 'No instances configured under remote-instances/; nothing to stop.\n'; exit 0; }; \
	while IFS= read -r name; do \
		PYTHONPATH=$(DEVCONTAINER_SCRIPTS_DIR) python3 -m devcontainer_config.cli instance-stop "$$name" --region "$$REMOTE_AWS_REGION" || exit 1; \
	done <<< "$$names"

instance-start:
	@$(call INSTANCE_OR_ALL_GUARD,instance-start)
	@set -euo pipefail; \
	$(REMOTE_AWS_REGION_GUARD) \
	if [ "$(ALL)" = "1" ]; then names=$$($(DISCOVER_INSTANCE_NAMES)); else names="$(INSTANCE)"; fi; \
	[ -n "$$names" ] || { printf 'No instances configured under remote-instances/; nothing to start.\n'; exit 0; }; \
	while IFS= read -r name; do \
		PYTHONPATH=$(DEVCONTAINER_SCRIPTS_DIR) python3 -m devcontainer_config.cli instance-start "$$name" --region "$$REMOTE_AWS_REGION" || exit 1; \
	done <<< "$$names"

# Records the EC2 id Terragrunt output in the instance's per-instance id
# store (devcontainer_config.instance_ops.link_id) -- the place the power,
# status and remote targets read it from. Deploy does this automatically;
# this target exists for an instance re-provisioned outside make. The id's
# shape is validated by the cli, which refuses anything but i- plus
# lowercase hex.
instance-link:
	@$(call INSTANCE_USAGE_GUARD,instance-link)
	@if [ -z "$(INSTANCE_ID)" ]; then \
		printf '\033[0;31m[ERROR]\033[0m INSTANCE_ID is required, e.g.: make instance-link INSTANCE=<name> INSTANCE_ID=i-0123456789abcdefg\n' >&2; \
		exit 2; \
	fi
	@PYTHONPATH=$(DEVCONTAINER_SCRIPTS_DIR) python3 -m devcontainer_config.cli instance-link "$(INSTANCE)" --instance-id "$(INSTANCE_ID)"

# Converge one instance, or every configured instance with ALL=1, aborting
# on the first failure named. Per instance, in order: init, validate, a plan
# guarded against accidental replacement (REFUSES below unless
# CONFIRM=replace) whose saved plan file IS what apply consumes -- the guard
# reads the plan's own output, then `apply -auto-approve tfplan.deploy`
# applies exactly what was guarded, never a re-plan; the file is removed by
# the per-instance EXIT trap on every exit path -- then link the output id,
# converge the missing trust chain (cert material and published TLS params
# are created only when the status probes report them absent -- when params
# are already present the publish/install steps are skipped entirely, so a
# live daemon is never restarted; each probe must answer exactly true or
# false -- empty, null or malformed fails the deploy naming the instance and
# the raw status output), then push-secrets. Prints the follow-on chain,
# which blocks on `make remote` and so is printed rather than run.
instance-deploy:
	@$(call INSTANCE_OR_ALL_GUARD,instance-deploy)
	@set -euo pipefail; \
	command -v jq > /dev/null 2>&1 || { \
		printf '\033[0;31m[ERROR]\033[0m jq is not installed; the converge step reads instance status with it.\n' >&2; \
		printf '        Install it: brew install jq (macOS) or sudo apt-get install -y jq (Linux)\n' >&2; \
		exit 1; \
	}; \
	probe_bool() { \
		instance="$$1" field="$$2" json="$$3"; \
		value=$$(printf '%s' "$$json" | jq -r ".$$field") || { \
			printf '\033[0;31m[ERROR]\033[0m %s: jq could not read %s from the status output below\n' "$$instance" "$$field" >&2; \
			printf '        Raw status output: %s\n' "$$json" >&2; \
			exit 1; \
		}; \
		case "$$value" in \
			true|false) printf '%s' "$$value" ;; \
			*) \
				printf '\033[0;31m[ERROR]\033[0m %s: the %s probe answered (got: %s), not true or false\n' "$$instance" "$$field" "$$value" >&2; \
				printf '        Raw status output: %s\n' "$$json" >&2; \
				exit 1 ;; \
		esac; \
	}; \
	$(TERRAGRUNT_INIT_HELPER); \
	if [ "$(ALL)" = "1" ]; then names=$$($(DISCOVER_INSTANCE_NAMES)); else names="$(INSTANCE)"; fi; \
	[ -n "$$names" ] || { printf 'No instances configured under remote-instances/; nothing to deploy.\n'; exit 0; }; \
	while IFS= read -r name; do \
	( \
		printf '\033[0;36m[DEPLOY]\033[0m %s\n' "$$name"; \
		tg_init "$$name"; \
		plan_file="$${PWD}/tfplan.deploy"; \
		trap 'rm -f "$$plan_file"' EXIT; \
		terragrunt validate; \
		plan_log=$$(terragrunt plan -out=tfplan.deploy 2>&1) || { printf '%s\n' "$$plan_log" >&2; exit 1; }; \
		if printf '%s\n' "$$plan_log" | grep -q 'must be replaced' || \
			printf '%s\n' "$$plan_log" | grep -Eq 'Plan: [0-9]+ to add, [0-9]+ to change, [1-9][0-9]* to destroy'; then \
			if [ "$(CONFIRM)" != "replace" ]; then \
				printf '%s\n' "$$plan_log" | grep -E 'must be replaced|Plan: ' >&2; \
				printf '\033[0;31m[ERROR]\033[0m %s: this plan replaces or destroys resources; refusing.\n' "$$name" >&2; \
				printf '        Review the offending plan lines above. Proceed deliberately with:\n' >&2; \
				printf '          make instance-deploy INSTANCE=%s CONFIRM=replace\n' "$$name" >&2; \
				exit 1; \
			fi; \
		fi; \
		terragrunt apply -auto-approve tfplan.deploy; \
		id=$$(terragrunt output -raw instance_id); \
		[ -n "$$id" ] || { \
			printf '\033[0;31m[ERROR]\033[0m terragrunt output -raw instance_id returned nothing for %s\n' "$$name" >&2; \
			printf '        Did the apply above succeed? Inspect it: cd remote-instances/%s && terragrunt output\n' "$$name" >&2; \
			exit 1; \
		}; \
		PYTHONPATH=$(DEVCONTAINER_SCRIPTS_DIR) python3 -m devcontainer_config.cli instance-link "$$name" --instance-id "$$id"; \
		cd "$(CURDIR)"; \
		status_json=$$(PYTHONPATH=$(DEVCONTAINER_SCRIPTS_DIR) python3 -m devcontainer_config.cli instance-status "$$name" --json) || { \
			printf '\033[0;31m[ERROR]\033[0m %s: instance-status --json failed; the trust chain cannot be inspected\n' "$$name" >&2; \
			exit 1; \
		}; \
		certs_present=$$(probe_bool "$$name" certs_present "$$status_json"); \
		params_present=$$(probe_bool "$$name" params_present "$$status_json"); \
		if [ "$$certs_present" != "true" ]; then \
			ca_out=$$(make --no-print-directory cert-ca INSTANCE="$$name" 2>&1) || { \
				printf '%s\n' "$$ca_out" | grep -q 'already exists' || { printf '%s\n' "$$ca_out" >&2; exit 1; }; \
			}; \
			make --no-print-directory cert-client INSTANCE="$$name"; \
		fi; \
		if [ "$$params_present" != "true" ]; then \
			make --no-print-directory cert-publish INSTANCE="$$name"; \
			make --no-print-directory cert-install INSTANCE="$$name"; \
		fi; \
		make --no-print-directory push-secrets INSTANCE="$$name"; \
		printf '\033[0;32m[DONE]\033[0m %s converged. Next, in order:\n' "$$name"; \
		printf '  make remote INSTANCE=%s      # refreshes the SSM port forward; blocks until interrupted\n' "$$name"; \
		printf '  make build INSTANCE=%s\n' "$$name"; \
		printf '  make reopen INSTANCE=%s\n' "$$name"; \
	) || exit 1; \
	done <<< "$$names"

# Destroy one instance, or every configured one with ALL=1 -- and ALL=1
# additionally requires CONFIRM=destroy, because a typo'd ALL should never
# be all it takes to end the fleet. Per instance: a best-effort note of what
# dies (the EC2 instance and its volumes always; the containers on it only
# when its docker context answers within DOCKER_CHECK_TIMEOUT_SECONDS, since
# a dead daemon is no evidence either way), then terragrunt destroy, then
# the cli's instance-cleanup for the params, context, certs and id that
# Terragrunt does not know about.
instance-destroy:
	@$(call INSTANCE_OR_ALL_GUARD,instance-destroy)
	@set -euo pipefail; \
	$(REMOTE_AWS_REGION_GUARD) \
	if [ "$(ALL)" = "1" ]; then \
		if [ "$(CONFIRM)" != "destroy" ]; then \
			printf '\033[0;31m[ERROR]\033[0m ALL=1 destroys every configured instance; confirm it: make instance-destroy ALL=1 CONFIRM=destroy\n' >&2; \
			exit 1; \
		fi; \
		names=$$($(DISCOVER_INSTANCE_NAMES)); \
	else \
		names="$(INSTANCE)"; \
	fi; \
	[ -n "$$names" ] || { printf 'No instances configured under remote-instances/; nothing to destroy.\n'; exit 0; }; \
	$(TERRAGRUNT_INIT_HELPER); \
	while IFS= read -r name; do \
	( \
		printf '\033[0;36m[DESTROY]\033[0m %s: the EC2 instance and its volumes are deleted\n' "$$name"; \
		ctx=$$(PYTHONPATH=$(DEVCONTAINER_SCRIPTS_DIR) python3 -c "from pathlib import Path; from devcontainer_config import instances, repo; print(instances.docker_context(repo.find_root(Path.cwd()), '$$name'))"); \
		docker_timeout="$${DOCKER_CHECK_TIMEOUT_SECONDS:-10}"; \
		timer=""; \
		if command -v timeout > /dev/null 2>&1; then timer="timeout"; elif command -v gtimeout > /dev/null 2>&1; then timer="gtimeout"; fi; \
		docker_reachable=1; \
		if [ -n "$$timer" ]; then \
			"$$timer" "$$docker_timeout" docker --context "$$ctx" version > /dev/null 2>&1 || docker_reachable=0; \
		else \
			docker --context "$$ctx" version > /dev/null 2>&1 || docker_reachable=0; \
		fi; \
		if [ "$$docker_reachable" -eq 1 ]; then \
			printf '  its containers die with it; they could be reached via %s, so inspect them first if unsure\n' "$$ctx"; \
		else \
			printf '  its containers could not be checked (docker unreachable via %s); they die with the instance regardless\n' "$$ctx"; \
		fi; \
		tg_init "$$name"; \
		terragrunt destroy -auto-approve; \
		cd "$(CURDIR)"; \
		PYTHONPATH=$(DEVCONTAINER_SCRIPTS_DIR) python3 -m devcontainer_config.cli instance-cleanup "$$name" --region "$$REMOTE_AWS_REGION"; \
	) || exit 1; \
	done <<< "$$names"

shell:
	@printf '\033[0;31m[ERROR]\033[0m make shell is gone: the EC2 host has no interactive access path.\n' >&2
	@printf '        The SSH transport was removed at cutover, and the remote engine is now\n' >&2
	@printf '        reached over an SSM port forward that carries the docker API only.\n' >&2
	@printf '        For a shell inside the container:  \033[1mmake exec\033[0m\n' >&2
	@exit 1

vscode-server:
	@INSTANCE="$(INSTANCE)" $(CONTAINER_SH) vscode-server

# The hostcreds push: resolves every manifest entry on this machine (the
# container never resolves anything itself) and writes the fragments into
# the container. Delegates to container.sh, which pipes each fragment over
# stdin so no value rides a docker exec's argv.
push-creds:
	@INSTANCE="$(INSTANCE)" $(CONTAINER_SH) push-creds

# Host only: prompts once per keychain credential the manifest names whose
# item is missing, storing each through 'security -i' with the value on
# stdin. CREDS_INIT_ARGS passes flags through to the subcommand for
# automation: make creds-init CREDS_INIT_ARGS="--stdin NAME" with the value
# piped on stdin.
creds-init:
	@PYTHONPATH=$(DEVCONTAINER_SCRIPTS_DIR) python3 -m devcontainer_config.cli creds-init $(CREDS_INIT_ARGS)

# Structural plus functional verification of the pushed credentials, through
# docker exec against the active context, so it works identically on either
# engine.
verify-container:
	@INSTANCE="$(INSTANCE)" $(CONTAINER_SH) verify

clean:
	@INSTANCE="$(INSTANCE)" $(CONTAINER_SH) clean

rebuild:
	@INSTANCE="$(INSTANCE)" $(CONTAINER_SH) rebuild

push-secrets:
	@INSTANCE="$(INSTANCE)" $(SECRETS_SH)

# spec Section 4.1.2 (E6-F1-S1-T2): client and CA expiry per instance, the
# inspection half of the `certs` module. Exit 0 when every certificate is
# outside the warning window (including a RENEW row for one still valid but
# inside it), exit 1 when any has expired.
cert-status:
	@PYTHONPATH=$(DEVCONTAINER_SCRIPTS_DIR) python3 -m devcontainer_config.certs status

# spec Section 4.5: the issuing half. Separate targets, not one idempotent
# "ensure": each underlying operation refuses to overwrite existing material,
# so running the wrong one names the path that already exists rather than
# silently replacing a certificate the other half of the pair still trusts.
cert-ca:
	@INSTANCE="$(INSTANCE)" $(CERTS_SH) ca

cert-client:
	@INSTANCE="$(INSTANCE)" $(CERTS_SH) client

cert-publish:
	@INSTANCE="$(INSTANCE)" $(CERTS_SH) publish

cert-install:
	@INSTANCE="$(INSTANCE)" $(CERTS_SH) install

proxy-start:
	@$(PROXY_ENV) $(PROXY_SH) start

proxy-stop:
	@$(PROXY_ENV) $(PROXY_SH) stop

proxy-restart:
	@$(PROXY_ENV) $(PROXY_SH) restart

proxy-status:
	@$(PROXY_ENV) $(PROXY_SH) status

lint: lint-private lint-nested lint-workspace lint-json lint-sh lint-dispatch lint-md lint-spell lint-secrets
	@printf '\033[0;32m[DONE]\033[0m all checks passed\n'

# The single entry point external automation calls to decide whether this
# checkout is green. Kept separate from lint so that adding a test suite widens
# what "green" means without every caller having to learn a new target name.
validate: lint test
	@printf '\033[0;32m[DONE]\033[0m validate passed\n'

# Host only, hermetic: no docker, no AWS, no network (AC-10.14). Every tool
# named in the "test" PREREQUISITES row above is checked here, in one loop
# over TEST_PREREQUISITE_TOOLS, before pytest ever runs, so a missing
# prerequisite fails with the command that installs it instead of failing
# deep inside the suite with no such hint.
test:
	@printf '\033[0;36m[TEST]\033[0m running pytest suite\n'
	@for tool in $(TEST_PREREQUISITE_TOOLS); do \
		command -v "$$tool" > /dev/null 2>&1 && continue; \
		case "$$tool" in \
			uv) hint="$(TEST_INSTALL_HINT_uv)" ;; \
			zsh) hint="$(TEST_INSTALL_HINT_zsh)" ;; \
		esac; \
		printf '\033[0;31m[ERROR]\033[0m %s is not installed.\n' "$$tool" >&2; \
		printf '        Install it: %s\n' "$$hint" >&2; \
		exit 1; \
	done
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

lint-workspace:
	@if [ -e .venv ]; then \
		printf '\033[0;31m[ERROR]\033[0m .venv exists at the repository root\n' >&2; \
		printf '        A host-OS virtual environment here is bind-mounted into the devcontainer\n' >&2; \
		printf '        as-is, whose Python tooling resolves it as the workspace interpreter\n' >&2; \
		printf '        and fails to spawn it (ENOENT in the Ruff and Python logs).\n' >&2; \
		printf '        Delete it, then use the make targets, which keep every uv environment\n' >&2; \
		printf '        outside the workspace (UV_PROJECT_ENVIRONMENT):\n' >&2; \
		printf '            rm -rf .venv\n' >&2; \
		exit 1; \
	fi
	@printf '\033[0;36m[LINT]\033[0m workspace carries no virtual environment\n'

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
	@for f in $(PRIVATE_FILES_AND_MANIFEST); do \
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

# Runs on pre-push (E2-F2-S1-T1): the same lint pre-commit runs, then a
# secrets scan of every commit in the pushed range, derived from git's own
# pre-push stdin and read by devcontainer_config.githooks. lint runs first
# so a pre-push failure never reaches the (slower) history scan needlessly;
# git's stdin flows through both recipe lines unredirected, into whichever
# one actually reads it.
hooks-run-push: lint
	@PYTHONPATH=$(DEVCONTAINER_SCRIPTS_DIR) python3 -m devcontainer_config.cli hooks-pre-push

# Hook content lives in devcontainer_config.githooks, not here (E2-F2-S1-T1):
# this delegates instead of writing hook bodies inline, so that content has
# exactly one source. install_hooks is idempotent and refuses to overwrite a
# hook it did not author.
hooks-install:
	@PYTHONPATH=$(DEVCONTAINER_SCRIPTS_DIR) python3 -m devcontainer_config.cli hooks-install

hooks-uninstall:
	@rm -f .git/hooks/pre-commit .git/hooks/pre-push
	@printf '\033[0;32m[DONE]\033[0m removed pre-commit and pre-push hooks\n'
