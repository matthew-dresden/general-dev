
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
DEVCONTAINER_SCRIPTS_DIR := $(CURDIR)/.claude/plugins/devcontainer/scripts

# Usage guard for a target that acts on exactly one instance: prints its
# usage lines and exits 2 when INSTANCE is empty. $(1) is the target name,
# so the usage line shows the target it came from.
define INSTANCE_USAGE_GUARD
if [ -z "$(INSTANCE)" ]; then \
	printf '\033[0;31m[ERROR]\033[0m INSTANCE is required, e.g.: make $(1) INSTANCE=<instance-name>\n' >&2; \
	printf '        Instance names are project names (e.g. acme).\n' >&2; \
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
	printf '        Instance names are project names (e.g. acme).\n' >&2; \
	printf '        See what exists: make list-instances\n' >&2; \
	exit 2; \
fi; \
if [ -n "$(INSTANCE)" ] && [ "$(ALL)" = "1" ]; then \
	printf '\033[0;31m[ERROR]\033[0m pass INSTANCE=<name> or ALL=1 to $(1), not both\n' >&2; \
	exit 2; \
fi
endef

# Refusal for the resolver-loop targets that accept ALL=1 in place of
# INSTANCE. ALL=1 acts on every configured instance, discovered at run time
# through the engine's own discovery, which never consults ENGINE; ENGINE
# pins exactly one. A fleet loop under ENGINE would therefore silently
# ignore one of the two selections the caller made, so the run refuses with
# the reason and exits 2, matching the usage guards' convention.
define ENGINE_FLEET_REFUSAL
if [ -n "$${ENGINE:-}" ] && [ "$(ALL)" = "1" ]; then \
	printf '\033[0;31m[ERROR]\033[0m ENGINE=%s is set: ALL=1 acts on every configured instance and ENGINE pins exactly one; run it with INSTANCE=<name> only, or ALL=1 without ENGINE\n' "$${ENGINE}" >&2; \
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

# The kind refusals. rd_engine_kind/rd_require_engine_kind (lib.sh) are the
# one predicate and the one refusal; these constants are the per-family
# remedies the refusals name, declared once so every target of a family
# states the same guidance and none of them restates the predicate. They are
# comma-free on purpose: they ride $(call ...) arguments, which split on
# commas. The gcp engine kind joins the resolver and the connect surface in
# the same change, and its per-family surfaces (lifecycle, buckets,
# certificates) landed with its provider slice, so an ssh engine is the one
# kind each remedy still points elsewhere for.
EC2_KIND_REMEDY := the lifecycle targets serve ec2 azure and gcp engines; an ssh engine has none of it -- reach one with make engine-verify INSTANCE=<name> or remove it with make engine-remove INSTANCE=<name> CONFIRM=remove and connect with make connect-start INSTANCE=<name>
BUCKET_KIND_REMEDY := state stores are per kind: EC2 buckets answer to the aws CLI azure storage accounts to the az CLI and gcp buckets to the gcloud storage CLI; an ssh engine has neither and no engine-* equivalent for this target
CERT_KIND_REMEDY := the certificate targets serve ec2 azure and gcp engines; an ssh engine has no TLS material and nothing to report for one

# The fleet-wide state-surface guard, for the surfaces that address no
# single instance (bucket-list, bucket-delete ALL=1): the state surfaces are
# per kind -- the EC2 fleet's buckets answer to the aws CLI, the azure
# fleet's storage accounts to the az CLI -- so the guard resolves every
# configured instance fresh and answers with the one kind the fleet carries,
# in fleet_kind, for the caller to dispatch on. A fleet mixing kinds is
# refused naming them, with the per-kind instruction -- fail-fast, never a
# silent skip; a fleet of ssh engines is refused with the family's remedy
# ($(1)), because neither surface reaches one. An empty fleet reads as the
# EC2 kind the surfaces have always served, so an empty listing still
# reaches its nothing-configured branch unchanged.
define FLEET_STATE_KIND_GUARD
. $(RD_DIR)/lib.sh; \
names=$$($(DISCOVER_INSTANCE_NAMES)) || exit 1; \
kinds=""; \
while IFS= read -r name; do \
	[ -n "$$name" ] || continue; \
	unset RD_INSTANCE_RESOLVED RD_RESOLVE_DIAGNOSIS; \
	INSTANCE="$$name" rd_resolve_instance; \
	kinds="$$kinds $$(rd_engine_kind)"; \
done <<< "$$names"; \
set -- $$kinds; \
if [ $$# -eq 0 ]; then \
	fleet_kind="ec2"; \
elif [ $$# -eq 1 ] && [ "$$1" = "ssh" ]; then \
	rd_fail "every configured instance is an ssh engine" \
		"$(1)"; \
elif [ $$# -eq 1 ]; then \
	fleet_kind="$$1"; \
else \
	rd_fail "this fleet mixes engine kinds ($$kinds)" \
		"state surfaces are per kind and none has a mixed-fleet form; run them" \
		"per kind instead: with only EC2 instances configured the surface answers" \
		"to the aws CLI, with only azure ones to the az CLI, with only gcp ones to" \
		"the gcloud storage CLI, and one instance's store is deleted per instance:" \
		"make bucket-delete INSTANCE=<name>."; \
fi
endef

# The instance names `instances.discover` reports, one per line, sorted --
# the engine's own discovery (files and _envcommon filtered out), not a
# second shell reimplementation of the same rule. Consumed by every ALL=1
# loop below; an empty listing is handled by the loop preamble, not here.
DISCOVER_INSTANCE_NAMES = PYTHONPATH=$(DEVCONTAINER_SCRIPTS_DIR) python3 -c "from pathlib import Path; from devcontainer_config import instances, repo; print('\n'.join(instances.discover(repo.find_root(Path.cwd()))))"

# REMOTE_AWS_REGION is required, never defaulted, in every target that names
# a region -- the Terragrunt helper and the power/destroy targets alike.
# root.hcl derives each instance's state bucket's name from this variable,
# so a silently substituted default would point a whole run's state at a
# bucket belonging to another region instead of failing, and no region is a
# safe guess. Expanded into each recipe that needs the region, so the
# requirement is stated once and enforced everywhere (fail-fast, per
# CLAUDE.md).
define REMOTE_AWS_REGION_GUARD
: "$${REMOTE_AWS_REGION:?REMOTE_AWS_REGION is required and has no default (root.hcl names each state bucket from it). Set it with: export REMOTE_AWS_REGION=<region>}";
endef

# Per-instance shell helpers, inlined into each recipe that runs Terragrunt
# (make expands them textually; the shell sees one function definition per
# run). tg_init <name> cds into the instance's directory -- always from
# $(CURDIR), so consecutive iterations never nest relative paths -- then
# does the init work the instance's engine kind needs, from the kind the
# directory itself declares. The azure kind has no --backend-bootstrap form
# (Terragrunt's azurerm backend bootstraps nothing): its state store -- one
# storage account and its `tfstate` blob container per instance, in a
# dedicated resource group that stands outside the deployment's lifecycle --
# is ensured by devcontainer_config.state_store before any Terragrunt
# command runs, probing each resource and creating it only when confirmed
# missing, every other probe answer raised with the command and its raw
# output. The gcp kind is the same shape: Terragrunt's gcs backend
# bootstraps nothing either, so its state store -- one GCS bucket per
# instance, in the project the engine address record names, standing
# outside the deployment's lifecycle -- is ensured by
# devcontainer_config.state_store_gcp before any Terragrunt command runs,
# probing the bucket and creating it (uniform-bucket-level-access, at the
# region the record names) only when confirmed missing. The EC2 kind
# decides the init form BEFORE running any Terragrunt, from one explicit
# probe instead of a caught error:
# devcontainer_config.state_bucket composes the backend bucket's name from
# remote-instances/root.hcl's own template (the single declaration of it),
# probes the bucket with `aws s3api head-bucket`, and prints `bootstrap`
# when the bucket is confirmed missing and `plain` otherwise. `bootstrap`
# runs `terragrunt init --backend-bootstrap`, which provisions the bucket on
# the first run; every other probe outcome -- a 403 on a bucket this
# identity cannot see, a failed account lookup, anything unexpected -- is
# raised by the module with the command and its raw output, so the run stops
# loudly instead of retrying behind the failure. Neither init form is
# retried, and neither failure is swallowed: both end the target at the step
# that failed.
#
# REMOTE_AWS_REGION has no default on the EC2 branch (see
# REMOTE_AWS_REGION_GUARD): the helper requires it fail-fast before the
# first Terragrunt call, because root.hcl derives each instance's state
# bucket name from it. The azure and gcp branches expand no region guard:
# az is ambiently logged in and its storage account takes the location the
# scaffold pins, and gcloud is ambiently logged in and its bucket takes
# the region the engine address record names, so there is no region for
# either to require.
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
	kind=$$(PYTHONPATH=$(DEVCONTAINER_SCRIPTS_DIR) python3 -c "from pathlib import Path; from devcontainer_config import instances, repo; print(instances.read_kind(repo.find_root(Path.cwd()), '$$name'))"); \
	if [ "$$kind" = "azure" ]; then \
		PYTHONPATH=$(DEVCONTAINER_SCRIPTS_DIR) python3 -m devcontainer_config.state_store ensure "$(CURDIR)" "$$name" || exit 1; \
	elif [ "$$kind" = "gcp" ]; then \
		PYTHONPATH=$(DEVCONTAINER_SCRIPTS_DIR) python3 -m devcontainer_config.state_store_gcp ensure "$(CURDIR)" "$$name" || exit 1; \
	else \
		$(REMOTE_AWS_REGION_GUARD) \
		export TG_NON_INTERACTIVE=true; \
		init_form=$$(PYTHONPATH=$(DEVCONTAINER_SCRIPTS_DIR) python3 -m devcontainer_config.state_bucket init-form "$(CURDIR)" "$$name") || exit 1; \
		if [ "$$init_form" = "bootstrap" ]; then \
			echo y | terragrunt init --backend-bootstrap -input=false || exit 1; \
		else \
			terragrunt init -input=false || exit 1; \
		fi; \
	fi; \
}
endef

PROXY_ENV = set -a; . $(CONFIG); set +a;

LOCAL_CONTEXT = $(shell source $(CONFIG) && echo $$LOCAL_DOCKER_CONTEXT)
REMOTE_CONTEXT = $(shell source $(CONFIG) && echo $$REMOTE_DOCKER_CONTEXT)

UVX ?= uvx
# The skills targets' selectors (U3), defaulted here once so `make
# skills-install` with no variables behaves like the documented default
# (both agents, global scope) and the cli subcommands receive a value even
# when the caller sets neither -- the recipes pass "$(AGENT)"/"$(SCOPE)"
# unconditionally, and an empty value would be an argparse usage error
# instead of the default. Both values are validated (with the valid sets
# named) by the cli's argparse choices=, so a typo like AGENT=code fails
# fast with the accepted values rather than acting on a guess.
AGENT ?= both
SCOPE ?= global
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
# The markdown set the prose gate reads: the same find MD_FILES runs, minus
# the sets the other linters and consumers own -- the skills roster's
# canonical copies (installed into agent surfaces, written against their own
# register), CLAUDE.md (agent tooling instructions) and tests/ (fixtures).
# The prose surfaces: user-facing markdown only. Skills and CLAUDE.md keep
# their own voice, tests/ carries fixtures, and .terragrunt-cache holds
# generated copies of the repo's docs that terragrunt refreshes on its own
# schedule -- restyling a cache copy would be undone by the next init.
PROSE_FILES ?= $(shell find . -name '*.md' $(LINT_EXCLUDES) -not -path './.agents/skills/*' -not -name 'CLAUDE.md' -not -path './tests/*' -not -path '*/.terragrunt-cache/*' -not -path './.pytest_cache/*' -not -path './.venv/*')
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
.PHONY: help connect-start disconnect status exec shell start stop restart rename check build push-creds creds-init verify-container clean rebuild push-secrets list-containers \
        lint lint-md lint-sh lint-dispatch lint-json lint-private lint-nested lint-workspace lint-secrets lint-spell lint-prose spell-fix format hooks-install hooks-uninstall hooks-run hooks-run-push \
        proxy-start proxy-stop proxy-restart proxy-status build-no-cache rebuild-no-cache local remote reopen init up vscode-server \
        keybindings validate coverage test cert-status list-instances instance-init instance-plan instance-deploy instance-status instance-stop instance-start instance-destroy instance-link \
        engine-init engine-verify engine-remove \
        connect-status connect-stop connect-refresh connect-list bucket-list bucket-delete \
        skills-install skills-remove skills-list

# The help surface. Every two-column row (target, scope, description) renders
# through the single `row` helper defined inside the recipe: with a
# description at or under 180 characters it prints exactly what the plain
# printf printed before the cap existed, and past it the row wraps -- the
# description leaves line 1 and continues on 34-space-indented lines (the
# instruction column), each chunk kept inside the cap by
# devcontainer_config.helpline, whose geometry constants
# tests/test_makefile_contract.py pins to this recipe's literals. The legend
# entries render through `row` too, with an empty target, so their names sit
# in the scope column and their descriptions in the description column; the
# column-title rows sit flush left with the section headers. The helpers
# need the rows on one recipe line because a shell function defined on one
# recipe line is invisible to the next; the note() helper places every
# legend and section-header note at the instruction column, aligned with the
# descriptions below it, and the PREREQUISITES block keeps its own printf
# format (it is not a two-column command row).
help:
	@printf '\n\033[1m%s\033[0m devcontainer control.   Backend follows the active docker context.\n' "$(notdir $(CURDIR))"
	@set -euo pipefail; \
	row() { \
		if [ "$${#3}" -le 180 ]; then \
			printf '  \033[1;36m%-23s\033[0m %-7s %s\n' "$$1" "$$2" "$$3"; \
		else \
			PYTHONPATH=$(DEVCONTAINER_SCRIPTS_DIR) python3 -m devcontainer_config.helpline "$$1" "$$2" "$$3"; \
		fi; \
	}; \
	note() { \
		printf '\n\033[1m%-34s\033[0m%s\n' "$$1" "$$2"; \
	}; \
	note "Engines" "the two backends a target can act on:"; \
	row "" "local" "this machine's engine; builds bind-mount this folder"; \
	row "" "remote" "an EC2 engine; the repo is cloned into a volume on it"; \
	note "Scope" "the second column of every row:"; \
	row "" "both" "works on either engine via the active context"; \
	row "" "local" "that engine only"; \
	row "" "remote" "that engine only"; \
	row "" "host" "runs on this machine and touches no engine at all"; \
	printf '\n\033[1m%-25s\033[0m %-7s %s\n' "TARGET" "SCOPE" "WHAT IT DOES"; \
	printf '\n\033[1mSTART HERE\033[0m\n'; \
	row "make up"               "both"   "Get working from any state: refreshes the tunnel (remote), builds or starts as needed, then opens VS Code."; \
	note "FIRST RUN" "once per machine"; \
	row "make init"             "host"   "Create the gitignored config files (incl. the hostcreds manifest) from their examples. Never overwrites an existing one."; \
	row "make keybindings"      "host"   "Bind Shift+Enter to a newline in VS Code terminals. Must run on the host, not in the container."; \
	note "ENGINE" "pick where builds and containers live"; \
	row "make local"            "host"   "Point docker and VS Code at the local engine ($(LOCAL_CONTEXT)). Nothing remote is stopped."; \
	row "make disconnect"       "host"   "What 'make local' calls. Only changes where new commands and windows point."; \
	note "INSTANCES" "one remote engine per project under remote-instances/; instance names are project names (e.g. acme)"; \
	row "make list-instances"   "host"   "List every instance with live status: EC2 state, id, params, certs, forward, context; ssh engines answer reachability instead."; \
	row "make list-containers"  "host"   "Every container this project owns, local engine plus every instance's, with per-engine reachability."; \
	row "make instance-init"    "host"   "Scaffolds only INSTANCE=<n> [AMI=] [REGION= AMI/AZ lookup; deploys in REMOTE_AWS_REGION] Azure: CLOUD=azure LOCATION=<loc> [VM_SIZE=] GCP: CLOUD=gcp PROJECT=<p> ZONE=<z> [VM_SIZE=]"; \
	row "make instance-deploy"  "remote" "Converge: provision, write the address record (EC2 links the id, azure and gcp the record), trust chain, push secrets. Refuses replacement unless CONFIRM=replace. INSTANCE= | ALL=1"; \
	row "make instance-status"  "host"   "One instance's live state, or every instance with ALL=1. INSTANCE=<instance-name> | ALL=1"; \
	row "make instance-plan"    "remote" "Terragrunt plan per instance; bootstraps the state bucket (EC2) or ensures the azure or gcp state store on first run. INSTANCE=<instance-name> | ALL=1"; \
	row "make instance-stop"    "remote" "Stop the instance's VM (EC2, azure or gcp) and wait until it reports down. INSTANCE=<instance-name> | ALL=1"; \
	row "make instance-start"   "remote" "Start it again and wait for its engine agent (or the gcp provisioning marker) to report ready. INSTANCE=<instance-name> | ALL=1"; \
	row "make instance-destroy" "remote" "Destroy + cleanup context, certs (EC2 params, gcp secrets). CONFIRM=destroy only for ALL=1. INSTANCE=<instance-name> | ALL=1"; \
	row "make instance-link"    "host"   "EC2 only: save the instance's EC2 id; deploy does this automatically. Azure has no id: the deploy writes its record. INSTANCE_ID=<id>"; \
	row "make engine-init"      "host"   "Scaffold an ssh engine manifest (engine.json). INSTANCE=<name> HOST=<host> USER=<user> [PORT=22] [DOCKER_ADDR=/var/run/docker.sock; a Windows engine needs tcp://localhost:2375]"; \
	row "make engine-verify"    "host"   "One-shot ssh engine reachability proof: opens the forward, handshakes docker through its context, closes the forward. The ssh engine's connect-start."; \
	row "make engine-remove"    "host"   "Remove an ssh engine whole: forward, docker context, local state, registration. Needs CONFIRM=remove; refuses while a project container exists on the engine."; \
	row "make cert-ca"          "host"   "Create this instance's certificate authority. Once per instance; refuses if one exists."; \
	row "make cert-client"      "host"   "Issue the client certificate 'make connect-start' presents. Run after cert-ca, and again at renewal."; \
	row "make cert-publish"     "remote" "Issue server material and publish it to the engine's store (Parameter Store for EC2, Key Vault for azure, Secret Manager for gcp). The daemon needs it to open its listener."; \
	row "make cert-install"     "remote" "Have the instance fetch the published material and start its daemon. Run after cert-publish."; \
	row "make cert-status"      "host"   "Client and CA expiry per instance. Remote engines only (EC2, azure and gcp); ssh engines carry no TLS material."; \
	row "make push-secrets"     "remote" "Publish shell.env and aws-profile-map.json to the engine's store (Parameter Store for EC2, Key Vault for azure, Secret Manager for gcp). Remote builds do this when needed."; \
	row "make connect-start"    "remote" "Open an engine's forward as a background daemon (SSM for EC2, ssh for engine.json, relay for azure, iap for gcp); returns once docker answers. INSTANCE=<name> (or ENGINE=<name>)."; \
	row "make connect-status"   "host"   "Forward daemon state per instance: pid, port, listening. INSTANCE=<name> | ALL=1"; \
	row "make connect-stop"     "host"   "Close one forward daemon, or every one with ALL=1. INSTANCE=<name> | ALL=1"; \
	row "make connect-refresh"  "host"   "Stop and reopen from the recorded command -- for an expired SSO session or a dropped tunnel. INSTANCE=<name> | ALL=1"; \
	row "make connect-list"     "host"   "Every instance's forward: pid, port, listening or not."; \
	row "make bucket-list"      "host"   "Every state store this fleet's naming template matches (EC2 azure gcp): name, configured or orphaned. Per kind; mixed fleet refused. EC2 needs REMOTE_AWS_REGION."; \
	row "make bucket-delete"    "host"   "Purge + delete state stores: INSTANCE=<name> (EC2 azure or gcp), or ALL=1 CONFIRM=delete for every one. Per kind; mixed fleet refused. EC2 needs REMOTE_AWS_REGION."; \
	row "make remote"           "host"   "Point them at the remote engine ($(REMOTE_CONTEXT)): ensures the engine's forward in the background, then switches docker. INSTANCE=<name> targets that instance."; \
	note "BUILD" "every target blocks until the container is up and exits non-zero if anything fails"; \
	row "make build"            "both"   "Create the container for the active backend. Refuses if one already exists."; \
	row "make rebuild"          "both"   "clean, then build. Prerequisites are checked before anything is destroyed."; \
	row "make build-no-cache"   "both"   "build with the image rebuilt from scratch."; \
	row "make rebuild-no-cache" "both"   "rebuild with the image rebuilt from scratch. Use when a feature or base image changed."; \
	row "make clean"            "both"   "Destroy the container, its private volumes and its image; the image is kept when a sibling still uses it. ALL=1 needs CONFIRM=destroy."; \
	printf '\n\033[1mLIFECYCLE\033[0m\n'; \
	row "make status"           "both"   "Backend, container, image and volumes. Read-only, so start here. ALL=1 reads every engine, one after the other."; \
	row "make reopen"           "both"   "Open the container in VS Code. Local opens the folder; remote names the container to attach to."; \
	row "make exec"             "both"   "Interactive shell inside the container. CONTAINER_SHELL picks which one."; \
	row "make vscode-server"    "both"   "Fetch the VS Code server this machine needs inside the container. reopen does it for you."; \
	row "make start / stop"     "both"   "Start or stop the container. The checkout survives either way. ALL=1 for every engine."; \
	row "make restart"          "both"   "Restart in place. Fixes a wedged container without rebuilding anything. ALL=1 for every engine."; \
	row "make rename NAME=x"    "both"   "Give the container a readable name. New ones are <repo>-<devcontainerId>, which is too long to pick from a list."; \
	row "make check"            "both"   "Remote: report uncommitted or unpushed work in the volume, non-zero when dirty. Local: a no-op. ALL=1 for every engine."; \
	printf '\n\033[1mSECRETS AND CERTIFICATES\033[0m\n'; \
	row "make push-creds"       "both"   "Resolve every hostcreds manifest entry on this machine and push it into the container. Git entries also seed ~/.git-credentials."; \
	row "make creds-init"       "host"   "Prompt once per missing keychain item the hostcreds manifest names and store it. CREDS_INIT_ARGS='--stdin NAME' feeds one value from stdin."; \
	row "make verify-container" "both"   "Check the pushed credentials inside the container: fragment modes, startup block, git and aws reachability."; \
	note "HOST PROXY" "only needed behind a corporate proxy; remote builds force HOST_PROXY=false"; \
	row "make proxy-start"      "local"  "Run tinyproxy on this machine. Local containers reach it via host.docker.internal."; \
	row "make proxy-status"     "local"  "Whether it is running, and on which port."; \
	row "make proxy-restart"    "local"  "Stop then start, picking up changed settings."; \
	row "make proxy-stop"       "local"  "Stop it. Settings come from $(CONFIG); set HOST_PROXY=true in shell.env to make the container use it."; \
	printf '\n\033[1mQUALITY\033[0m\n'; \
	row "make lint"             "host"   "Private files untracked, no nested repos, no host venv in the workspace, JSON parses, shellcheck, markdown, US English, prose rules, staged secrets. Non-zero on any finding."; \
	row "make lint-secrets"     "host"   "Scan staged content for secrets, or RANGE=<a>..<b> for a commit range. Exit 1 on any finding; there is no ignore list."; \
	row "make format"           "host"   "Auto-fix what the markdown tooling can fix, then report what is left."; \
	row "make lint-spell"       "host"   "US English spelling over this repo's markdown. SPELL_FILES overrides the set."; \
	row "make spell-fix"        "host"   "Auto-fix spelling, British-to-American included, in the same set. Rewrites the files."; \
	row "make hooks-install"    "host"   "Install pre-commit and pre-push hooks via devcontainer_config.githooks. Refuses to clobber a hook it did not write."; \
	row "make hooks-run"        "host"   "Exactly what pre-commit runs. Use it to reproduce a pre-commit failure."; \
	row "make hooks-run-push"   "host"   "Exactly what pre-push runs: lint, the pushed-range secrets scan, then the test and coverage steps CI runs."; \
	row "make test"             "host"   "Run the hermetic pytest suite in tests/. No docker, no AWS, no network."; \
	row "make coverage"         "host"   "The coverage gate CI runs: the same suite with the 90% floor over devcontainer_config enforced."; \
	row "make validate"         "host"   "The green-baseline contract automation depends on. Runs lint then test."; \
	note "SKILLS" "wire this repo's .agents/skills roster into an agent on this Mac; no devcontainer involved"; \
	row "make skills-install"   "host"   "Wire an agent to the skills home: global symlink (default), project verify/wire, or runtime one-shot (printed, never run). AGENT=opencode|claude|both SCOPE=global|project|runtime"; \
	row "make skills-remove"    "host"   "Delete only a general-dev-skills symlink resolving inside this repo; refuses everything else. AGENT=opencode|claude|both SCOPE=global|project|runtime"; \
	row "make skills-list"      "host"   "Show each agent scope: installed (target), native (project), or not installed. AGENT=opencode|claude|both SCOPE=global|project|runtime"; \
	printf '\n\033[1mOPTIONS\033[0m\n'; \
	row "ENGINE=local|<name>"   ""       "Address one engine explicitly (ENGINE=x make <target> or make <target> ENGINE=x): drive local and remote engines in parallel; unset follows the active context."; \
	row "CONTAINER=<name>"      ""       "Pick one container when several clones of this repo share an engine. 'make list-containers' lists them."; \
	row "NAME=<x>"              ""       "Name the container build/up creates at creation; make rename NAME=x does the same after the fact."; \
	row "FORCE=1"               ""       "Proceed past the unpushed-work and uncommitted-config guards."; \
	row "SKIP_SECRETS_CHECK=1"  ""       "Do not compare shell.env against Parameter Store, and do not publish it."; \
	row "NO_CACHE=1"            ""       "What the no-cache targets set. Works with build and rebuild directly."
	@printf '\n\033[1mPREREQUISITES\033[0m\n'
	@printf '\033[1m%-25s\033[0m %s\n' "TARGETS" "REQUIREMENTS"
	@printf '  %-23s %s\n' "container targets"     "docker"
	@printf '  %-23s %s\n' "remote engine"         "aws, session-manager-plugin, and the instance id recorded by instance-deploy (make instance-link)"
	@printf '  %-23s %s\n' "build and rebuild"     "devcontainer CLI, git, jq, python3      npm install -g @devcontainers/cli"
	@printf '  %-23s %s\n' "lint"                  "uv                                      brew install uv"
	@printf '  %-23s %s\n' "test"                  "uv, zsh   uv: $(TEST_INSTALL_HINT_uv)   zsh: $(TEST_INSTALL_HINT_zsh)"
	@printf '\n  %s\n' "Every target checks what it needs and fails with the command that installs it."
	@printf '\n'

# Opens an engine's forward as a detached daemon and returns once the
# connection is confirmed. The instance it opens one for: INSTANCE when
# given, else ENGINE when it names an instance, else none -- the resolver's
# default, addressed through the parse-time config values exactly as before.
# A named target is resolved here through the same lib.sh resolver every
# remote entry point uses (which reads the per-instance id store and derives
# the context the way the rest of the repo does), because the forwarding
# remedy this target backs -- `make remote INSTANCE=<name>` -- was hollow
# otherwise: the variable was accepted and ignored, and the forward for
# whatever instance the resolver defaulted to opened regardless of what was
# asked for. INSTANCE and ENGINE naming different engines is refused by the
# resolver's own guard before anything opens. The transport is then chosen
# by the engine's own kind, read through rd_engine_kind off the resolved
# address block's KIND line: an SSH engine (engine.json, KIND=ssh) opens
# through devcontainer_config.transport_ssh with the engine's own values and
# no AWS requirement at all, while an EC2 instance opens the SSM forward
# through devcontainer_config.forwards' ssm form -- the AWS guards live
# inside that branch only, so an ssh engine is never asked for a region, a
# profile or an instance id. DEVCONTAINER_TRANSPORT is a guard on that
# dispatch, never the selector: unset, the kind alone decides, so an
# operator naming an ssh engine never sets a variable; set, the value must
# agree with the kind (ssm behind an ec2 engine, ssh behind an ssh engine,
# iap behind a gcp engine) or the run is refused naming the conflict, and
# any other value is refused by name. The daemon is managed by
# devcontainer_config.forwards (spawn, readiness poll, record); this recipe
# only resolves WHICH instance and hands the resolved values over. The
# lifecycle companions: connect-start (this target), connect-status,
# connect-stop, connect-refresh and connect-list. lib.sh is sourced once,
# unconditionally, before the dispatch: the kind checks below read the kind
# through rd_engine_kind, the one predicate, and the ssh branch can only run
# for a resolved engine anyway. The azure kind (KIND=azure) routes to the
# relay form and the gcp kind (KIND=gcp) to the iap form, which add only the
# name and the context (iap adds --zone, read through the cli's gcp-zone
# reader -- the one distinguishing flag the forwards opener needs): the
# transports read their own address records the deploy wrote, so neither
# branch needs env guards at all -- engine-address.json is the config.
connect-start:
	@set -euo pipefail; \
	$(PROXY_ENV) \
	. $(RD_DIR)/lib.sh; \
	transport="$${DEVCONTAINER_TRANSPORT:-}"; \
	target=""; \
	if [ -n "$(INSTANCE)" ]; then target="$(INSTANCE)"; \
	elif [ -n "$${ENGINE:-}" ] && [ "$${ENGINE}" != "local" ]; then target="$${ENGINE}"; fi; \
	if [ -n "$$target" ]; then \
		INSTANCE="$$target" rd_resolve_instance; \
		kind="$$(rd_engine_kind)"; \
		ctx="$$(rd_engine_context)"; \
		[ -n "$$ctx" ] || ctx="$${REMOTE_DOCKER_CONTEXT:-}"; \
	else \
		kind=""; \
		ctx="$(REMOTE_CONTEXT)"; \
	fi; \
	case "$$transport" in \
		ssh|ssm|relay|iap|"") ;; \
		*) printf '\033[0;31m[ERROR]\033[0m DEVCONTAINER_TRANSPORT="%s" is not recognized.\n' "$$transport" >&2; \
		   printf '        Accepted values: ssm (the EC2 SSM forward), ssh (the SSH engine forward), relay (the azure relay forward), and iap (the gcp IAP tunnel forward).\n' >&2; \
		   exit 1 ;; \
	esac; \
	route="ssm"; \
	if [ -n "$$target" ]; then \
		if [ "$$kind" = "ssh" ]; then \
			route="ssh"; \
			if [ "$$transport" = "ssm" ]; then \
				printf '\033[0;31m[ERROR]\033[0m instance %s is an ssh engine; DEVCONTAINER_TRANSPORT=ssm selects the ssm transport, which cannot reach it\n' "$$target" >&2; \
				printf "        The engine's own kind selects the transport here: unset DEVCONTAINER_TRANSPORT or set it to ssh.\n" >&2; \
				exit 1; \
			fi; \
			if [ "$$transport" = "relay" ]; then \
				printf '\033[0;31m[ERROR]\033[0m instance %s is an ssh engine; DEVCONTAINER_TRANSPORT=relay selects the azure relay transport, which cannot reach it\n' "$$target" >&2; \
				printf "        The engine's own kind selects the transport here: unset DEVCONTAINER_TRANSPORT or set it to ssh.\n" >&2; \
				exit 1; \
			fi; \
			if [ "$$transport" = "iap" ]; then \
				printf '\033[0;31m[ERROR]\033[0m instance %s is an ssh engine; DEVCONTAINER_TRANSPORT=iap selects the gcp IAP tunnel transport, which cannot reach it\n' "$$target" >&2; \
				printf "        The engine's own kind selects the transport here: unset DEVCONTAINER_TRANSPORT or set it to ssh.\n" >&2; \
				exit 1; \
			fi; \
		elif [ "$$kind" = "azure" ]; then \
			route="relay"; \
			if [ "$$transport" = "ssm" ]; then \
				printf '\033[0;31m[ERROR]\033[0m instance %s is an azure engine; DEVCONTAINER_TRANSPORT=ssm selects the ssm transport, which cannot reach it\n' "$$target" >&2; \
				printf "        The engine's own kind selects the transport here: unset DEVCONTAINER_TRANSPORT or set it to relay.\n" >&2; \
				exit 1; \
			fi; \
			if [ "$$transport" = "ssh" ]; then \
				printf '\033[0;31m[ERROR]\033[0m instance %s is an azure engine; DEVCONTAINER_TRANSPORT=ssh selects the ssh transport, which cannot reach it\n' "$$target" >&2; \
				printf "        The engine's own kind selects the transport here: unset DEVCONTAINER_TRANSPORT or set it to relay.\n" >&2; \
				exit 1; \
			fi; \
			if [ "$$transport" = "iap" ]; then \
				printf '\033[0;31m[ERROR]\033[0m instance %s is an azure engine; DEVCONTAINER_TRANSPORT=iap selects the gcp IAP tunnel transport, which cannot reach it\n' "$$target" >&2; \
				printf "        The engine's own kind selects the transport here: unset DEVCONTAINER_TRANSPORT or set it to relay.\n" >&2; \
				exit 1; \
			fi; \
		elif [ "$$kind" = "gcp" ]; then \
			route="iap"; \
			if [ "$$transport" = "ssm" ]; then \
				printf '\033[0;31m[ERROR]\033[0m instance %s is a gcp engine; DEVCONTAINER_TRANSPORT=ssm selects the ssm transport, which cannot reach it\n' "$$target" >&2; \
				printf "        The engine's own kind selects the transport here: unset DEVCONTAINER_TRANSPORT or set it to iap.\n" >&2; \
				exit 1; \
			fi; \
			if [ "$$transport" = "ssh" ]; then \
				printf '\033[0;31m[ERROR]\033[0m instance %s is a gcp engine; DEVCONTAINER_TRANSPORT=ssh selects the ssh transport, which cannot reach it\n' "$$target" >&2; \
				printf "        The engine's own kind selects the transport here: unset DEVCONTAINER_TRANSPORT or set it to iap.\n" >&2; \
				exit 1; \
			fi; \
			if [ "$$transport" = "relay" ]; then \
				printf '\033[0;31m[ERROR]\033[0m instance %s is a gcp engine; DEVCONTAINER_TRANSPORT=relay selects the azure relay transport, which cannot reach it\n' "$$target" >&2; \
				printf "        The engine's own kind selects the transport here: unset DEVCONTAINER_TRANSPORT or set it to iap.\n" >&2; \
				exit 1; \
			fi; \
		elif [ "$$transport" = "ssh" ]; then \
			printf '\033[0;31m[ERROR]\033[0m instance %s is an EC2 engine; DEVCONTAINER_TRANSPORT=ssh selects the ssh transport, which only reaches ssh engines\n' "$$target" >&2; \
			printf '        Unset DEVCONTAINER_TRANSPORT or set it to ssm.\n' >&2; \
			exit 1; \
		elif [ "$$transport" = "relay" ]; then \
			printf '\033[0;31m[ERROR]\033[0m instance %s is an EC2 engine; DEVCONTAINER_TRANSPORT=relay selects the azure relay transport, which only reaches azure engines\n' "$$target" >&2; \
			printf '        Unset DEVCONTAINER_TRANSPORT or set it to ssm.\n' >&2; \
			exit 1; \
		elif [ "$$transport" = "iap" ]; then \
			printf '\033[0;31m[ERROR]\033[0m instance %s is an EC2 engine; DEVCONTAINER_TRANSPORT=iap selects the gcp IAP tunnel transport, which only reaches gcp engines\n' "$$target" >&2; \
			printf '        Unset DEVCONTAINER_TRANSPORT or set it to ssm.\n' >&2; \
			exit 1; \
		fi; \
	elif [ "$$transport" = "ssh" ]; then \
		printf '\033[0;31m[ERROR]\033[0m DEVCONTAINER_TRANSPORT=ssh needs the engine named\n' >&2; \
		printf '        The ssh form forwards the values of one resolved engine; run:\n' >&2; \
		printf '        make connect-start INSTANCE=<name>\n' >&2; \
		exit 2; \
	elif [ "$$transport" = "relay" ]; then \
		printf '\033[0;31m[ERROR]\033[0m DEVCONTAINER_TRANSPORT=relay needs the engine named\n' >&2; \
		printf '        The relay form forwards one resolved azure engine; run:\n' >&2; \
		printf '        make connect-start INSTANCE=<name>\n' >&2; \
		exit 2; \
	elif [ "$$transport" = "iap" ]; then \
		printf '\033[0;31m[ERROR]\033[0m DEVCONTAINER_TRANSPORT=iap needs the engine named\n' >&2; \
		printf '        The iap form forwards one resolved gcp engine; run:\n' >&2; \
		printf '        make connect-start INSTANCE=<name>\n' >&2; \
		exit 2; \
	fi; \
	case "$$route" in \
		iap) \
			gcp_zone="$$(PYTHONPATH=$(DEVCONTAINER_SCRIPTS_DIR) python3 -m devcontainer_config.cli gcp-zone "$$target")"; \
			PYTHONPATH=$(DEVCONTAINER_SCRIPTS_DIR) python3 -m devcontainer_config.forwards open \
				--name "$$target" --zone "$$gcp_zone" --context "$$ctx" ;; \
		relay) \
			PYTHONPATH=$(DEVCONTAINER_SCRIPTS_DIR) python3 -m devcontainer_config.forwards open \
				--name "$$target" --context "$$ctx" ;; \
		ssh) \
			PYTHONPATH=$(DEVCONTAINER_SCRIPTS_DIR) python3 -m devcontainer_config.forwards open \
				--name "$$target" --host "$$ENGINE_HOST" --port "$$ENGINE_PORT" \
				--user "$$ENGINE_USER" --docker-addr "$$ENGINE_DOCKER_ADDR" --context "$$ctx" ;; \
		ssm) \
			if [ -n "$$target" ]; then \
				[ -n "$${REMOTE_INSTANCE_ID:-}" ] || { \
					printf '\033[0;31m[ERROR]\033[0m no EC2 instance id is recorded for instance %s\n' "$$target" >&2; \
					printf '        Link it first: make instance-link INSTANCE=%s\n' "$$target" >&2; \
					exit 1; \
				}; \
			else \
				[ -n "$${REMOTE_INSTANCE_ID:-}" ] || { \
					printf '\033[0;31m[ERROR]\033[0m no instance is selected and no default id is recorded\n' >&2; \
					printf '        Name one: make connect-start INSTANCE=<instance-name>\n' >&2; \
					exit 1; \
				}; \
			fi; \
			[ -n "$${REMOTE_AWS_REGION:-}" ] || { \
				printf '\033[0;31m[ERROR]\033[0m REMOTE_AWS_REGION is required and has no default: the forward opens an SSM session in it\n' >&2; \
				printf '        Set it with: export REMOTE_AWS_REGION=<region>\n' >&2; \
				exit 2; \
			}; \
			[ -n "$${REMOTE_AWS_PROFILE:-}" ] || { \
				printf '\033[0;31m[ERROR]\033[0m REMOTE_AWS_PROFILE is required and has no default: the SSM session resolves credentials through it\n' >&2; \
				printf '        Set it with: export REMOTE_AWS_PROFILE=<sso-profile>, after: aws sso login --profile <sso-profile>\n' >&2; \
				exit 2; \
			}; \
			PYTHONPATH=$(DEVCONTAINER_SCRIPTS_DIR) python3 -m devcontainer_config.forwards open \
				--instance-id "$$REMOTE_INSTANCE_ID" --context "$$ctx" \
				--profile "$$REMOTE_AWS_PROFILE" --region "$$REMOTE_AWS_REGION" ;; \
	esac

# The forward daemon's lifecycle, one instance at a time or every configured
# one with ALL=1, each a thin loop over the devcontainer_config.forwards
# subcommands of the same concern (the engine is that module, the same shape
# as the instance-* layer). status and list are reports -- any state they
# answer is a success, and only an unanswerable probe fails them; stop
# verifies the forwarded port actually
# closed before reporting; refresh re-runs the recorded command, for an
# expired SSO session or a dropped tunnel; list renders every instance's
# forward state and is the one no-argument member.
connect-status:
	@$(call INSTANCE_OR_ALL_GUARD,connect-status)
	@$(call ENGINE_FLEET_REFUSAL)
	@set -euo pipefail; \
	if [ "$(ALL)" = "1" ]; then names=$$($(DISCOVER_INSTANCE_NAMES)); else names="$(INSTANCE)"; fi; \
	[ -n "$$names" ] || { printf 'No instances configured under remote-instances/; no forwards to report.\n'; exit 0; }; \
	while IFS= read -r name; do \
		PYTHONPATH=$(DEVCONTAINER_SCRIPTS_DIR) python3 -m devcontainer_config.forwards status "$$name" || exit 1; \
	done <<< "$$names"

connect-stop:
	@$(call INSTANCE_OR_ALL_GUARD,connect-stop)
	@$(call ENGINE_FLEET_REFUSAL)
	@set -euo pipefail; \
	if [ "$(ALL)" = "1" ]; then names=$$($(DISCOVER_INSTANCE_NAMES)); else names="$(INSTANCE)"; fi; \
	[ -n "$$names" ] || { printf 'No instances configured under remote-instances/; no forwards to stop.\n'; exit 0; }; \
	while IFS= read -r name; do \
		PYTHONPATH=$(DEVCONTAINER_SCRIPTS_DIR) python3 -m devcontainer_config.forwards stop "$$name" || exit 1; \
	done <<< "$$names"

connect-refresh:
	@$(call INSTANCE_OR_ALL_GUARD,connect-refresh)
	@$(call ENGINE_FLEET_REFUSAL)
	@set -euo pipefail; \
	if [ "$(ALL)" = "1" ]; then names=$$($(DISCOVER_INSTANCE_NAMES)); else names="$(INSTANCE)"; fi; \
	[ -n "$$names" ] || { printf 'No instances configured under remote-instances/; no forwards to refresh.\n'; exit 0; }; \
	while IFS= read -r name; do \
		PYTHONPATH=$(DEVCONTAINER_SCRIPTS_DIR) python3 -m devcontainer_config.forwards refresh "$$name" || exit 1; \
	done <<< "$$names"

connect-list:
	@PYTHONPATH=$(DEVCONTAINER_SCRIPTS_DIR) python3 -m devcontainer_config.forwards list

# State stores: one per instance, named from the template and its committed
# suffix in each cloud's root file (the only place both are declared --
# the EC2 bucket is tg-state-<instance>-<region>-<account>-<suffix>, the
# azure storage account composes its lowercase-alphanumeric form and the
# gcp bucket its gdstate-<sanitized>-<digest> form around the same
# per-instance naming idea). Read-only listing, and deletion, of the
# fleet's stores. All are per kind, decided by FLEET_STATE_KIND_GUARD
# above: an all-EC2 fleet's buckets list and delete through
# devcontainer_config.state_bucket exactly as they always did; an all-azure
# fleet's storage accounts through devcontainer_config.state_store -- the
# account whose name the template matches, marking the ones a configured
# azure instance owns; an all-gcp fleet's buckets through
# devcontainer_config.state_store_gcp the same way -- delete purges every
# container/blob then deletes the store (the azure account and its emptied
# state group, the gcp bucket after its objects) -- one instance's with
# INSTANCE=<name>, every fleet store with ALL=1, which additionally
# requires CONFIRM=delete because a typo'd ALL should never be all it takes
# to erase every instance's state history. Only the EC2 branches take
# REMOTE_AWS_REGION (the region guard runs there; azure takes the location
# the scaffold pins and gcp the region the engine address record names);
# deleting is deliberate, and the stores stand outside the instance
# lifecycle, so no instance-* target ever calls these.
bucket-list:
	@$(call FLEET_STATE_KIND_GUARD,$(BUCKET_KIND_REMEDY)); \
	if [ "$$fleet_kind" = "azure" ]; then \
		PYTHONPATH=$(DEVCONTAINER_SCRIPTS_DIR) python3 -m devcontainer_config.state_store list; \
	elif [ "$$fleet_kind" = "gcp" ]; then \
		PYTHONPATH=$(DEVCONTAINER_SCRIPTS_DIR) python3 -m devcontainer_config.state_store_gcp list; \
	else \
		$(REMOTE_AWS_REGION_GUARD) \
		PYTHONPATH=$(DEVCONTAINER_SCRIPTS_DIR) python3 -m devcontainer_config.state_bucket list; \
	fi

bucket-delete:
	@$(call INSTANCE_OR_ALL_GUARD,bucket-delete)
	@$(call ENGINE_FLEET_REFUSAL)
	@if [ "$(ALL)" = "1" ] && [ "$(CONFIRM)" != "delete" ]; then \
		printf '\033[0;31m[ERROR]\033[0m ALL=1 deletes every state store this fleet names (EC2 buckets, azure storage accounts, gcp buckets); confirm it: make bucket-delete ALL=1 CONFIRM=delete\n' >&2; \
		exit 1; \
	fi; \
	if [ "$(ALL)" = "1" ]; then \
		$(call FLEET_STATE_KIND_GUARD,$(BUCKET_KIND_REMEDY)); \
		if [ "$$fleet_kind" = "azure" ]; then \
			PYTHONPATH=$(DEVCONTAINER_SCRIPTS_DIR) python3 -m devcontainer_config.state_store delete-all; \
		elif [ "$$fleet_kind" = "gcp" ]; then \
			PYTHONPATH=$(DEVCONTAINER_SCRIPTS_DIR) python3 -m devcontainer_config.state_store_gcp delete-all; \
		else \
			$(REMOTE_AWS_REGION_GUARD) \
			PYTHONPATH=$(DEVCONTAINER_SCRIPTS_DIR) python3 -m devcontainer_config.state_bucket delete-all; \
		fi; \
	else \
		. $(RD_DIR)/lib.sh; \
		INSTANCE="$(INSTANCE)" rd_resolve_instance; \
		if [ "$$(rd_engine_kind)" = "azure" ]; then \
			PYTHONPATH=$(DEVCONTAINER_SCRIPTS_DIR) python3 -m devcontainer_config.state_store delete "$(INSTANCE)"; \
		elif [ "$$(rd_engine_kind)" = "gcp" ]; then \
			PYTHONPATH=$(DEVCONTAINER_SCRIPTS_DIR) python3 -m devcontainer_config.state_store_gcp delete "$(INSTANCE)"; \
		else \
			rd_require_engine_kind ec2 "$(BUCKET_KIND_REMEDY)"; \
			$(REMOTE_AWS_REGION_GUARD) \
			PYTHONPATH=$(DEVCONTAINER_SCRIPTS_DIR) python3 -m devcontainer_config.state_bucket delete "$(INSTANCE)"; \
		fi; \
	fi

disconnect:
	@$(call ENGINE_CONTEXT_SWITCH_REFUSAL,disconnect)
	@docker context inspect $(LOCAL_CONTEXT) > /dev/null 2>&1 || { \
		printf '\033[0;31m[ERROR]\033[0m docker context "%s" does not exist on this machine.\n' "$(LOCAL_CONTEXT)" >&2; \
		printf 'Set LOCAL_DOCKER_CONTEXT in %s to one of:\n' "$(CONFIG)" >&2; \
		docker context ls --format '  {{.Name}}' >&2; \
		exit 1; \
	}
	@docker context use $(LOCAL_CONTEXT)

# Fleet dispatcher for the container-level targets: $(1) is the container.sh
# subcommand, whose name is also the make target's. The default is the
# single-engine run every target always was. ALL=1 runs the subcommand once
# per engine -- the local engine plus every configured instance, discovered
# at run time, never parse time (the same discipline INSTANCE ?= follows) --
# exporting RDC_ALL so the script takes every project container on that
# engine instead of refusing ambiguity, and ENGINE so each iteration aims
# one engine without touching the context other terminals share. CONTAINER
# alongside ALL is refused, the one-or-the-other rule INSTANCE and ALL
# enforce for the instance targets. clean gates its fleet form behind
# CONFIRM=destroy (mirroring instance-destroy and bucket-delete): ALL=1
# destroys every container this project owns on every engine, so the exact
# confirming command is stated in the refusal; the per-container
# unpushed-work guards still run, and FORCE=1 bypasses those too, exactly
# as the single-container clean.
define CONTAINER_FLEET_DISPATCH
if [ "$(ALL)" = "1" ]; then \
	if [ -n "$(CONTAINER)" ]; then \
		printf '\033[0;31m[ERROR]\033[0m pass CONTAINER=<name> or ALL=1, not both\n' >&2; \
		printf '        ALL=1 already acts on every container this project owns, on every engine.\n' >&2; \
		exit 2; \
	fi; \
	if [ "$(1)" = "clean" ] && [ "$(CONFIRM)" != "destroy" ]; then \
		printf '\033[0;31m[ERROR]\033[0m ALL=1 destroys every container of this project on every engine -- containers, private volumes and unshared images; confirm it: make clean ALL=1 CONFIRM=destroy\n' >&2; \
		printf '        The unpushed-work guard still runs per container; FORCE=1 bypasses those too.\n' >&2; \
		exit 1; \
	fi; \
	engines="local $$($(DISCOVER_INSTANCE_NAMES))"; \
	for engine in $$engines; do \
		printf '\033[0;36m[FLEET]\033[0m engine %s\n' "$$engine"; \
		ENGINE="$$engine" RDC_ALL=1 INSTANCE="$(INSTANCE)" $(CONTAINER_SH) $(1) || exit 1; \
	done; \
else \
	INSTANCE="$(INSTANCE)" $(CONTAINER_SH) $(1); \
fi
endef

status:
	@$(call CONTAINER_FLEET_DISPATCH,status)

start:
	@$(call CONTAINER_FLEET_DISPATCH,start)

stop:
	@$(call CONTAINER_FLEET_DISPATCH,stop)

restart:
	@$(call CONTAINER_FLEET_DISPATCH,restart)

rename:
	@INSTANCE="$(INSTANCE)" $(CONTAINER_SH) rename

check:
	@$(call CONTAINER_FLEET_DISPATCH,check)

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
# recipe's own environment -- both spellings reach connect's recipe either
# way. connect opens the forward as a background daemon and returns once the
# docker context answers through it; remote then switches the context.
remote:
	@$(call ENGINE_CONTEXT_SWITCH_REFUSAL,remote)
	@$(MAKE) --no-print-directory connect-start
	@printf '\033[0;32m[DONE]\033[0m targeting the remote engine, the forward runs in the background, "make build" clones into a volume\n'

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

# The containers-side counterpart of list-instances: every container this
# project's label owns, on the local engine and on every configured
# instance's engine, one probe per engine. An engine that cannot answer
# (docker absent, a remote forward that is not open) degrades to a row and
# the listing keeps going; the exit code is 1 when any engine failed, so a
# script consuming it can tell an incomplete sweep from a complete one.
# No-argument member like list-instances: INSTANCE= and ALL=1 are irrelevant.
list-containers:
	@PYTHONPATH=$(DEVCONTAINER_SCRIPTS_DIR) python3 -m devcontainer_config.cli container-list --local-context "$(LOCAL_CONTEXT)"

# Writes remote-instances/<name>/terragrunt.hcl and its guidance. Never
# deploys; the cli prints the guidance messages verbatim. AMI=, REGION=,
# CLOUD=, LOCATION= and VM_SIZE= pass through when given; REGION is the
# scaffold-time AMI/AZ lookup ONLY -- it never selects where anything
# deploys. The deployment region is REMOTE_AWS_REGION, required without a
# default by every Terragrunt-running target (REMOTE_AWS_REGION_GUARD
# above), because root.hcl names the shared state bucket from it.
# CLOUD=azure switches the scaffold to the azure kind, which requires
# LOCATION= (the azure location) and takes VM_SIZE=; REGION=/AMI= are
# refused on azure.
instance-init:
	@$(call INSTANCE_USAGE_GUARD,instance-init)
	@PYTHONPATH=$(DEVCONTAINER_SCRIPTS_DIR) python3 -m devcontainer_config.cli instance-init "$(INSTANCE)" $(if $(CLOUD),--cloud $(CLOUD),) $(if $(LOCATION),--location $(LOCATION),) $(if $(PROJECT),--project $(PROJECT),) $(if $(ZONE),--zone $(ZONE),) $(if $(VM_SIZE),--vm-size $(VM_SIZE),) $(if $(REGION),--region $(REGION),) $(if $(AMI),--ami $(AMI),)

# Read-only: one instance's live state, or every instance's with ALL=1. The
# cli exits non-zero when any probe failed, so a loop abort names the
# instance whose surface could not be reached.
instance-status:
	@$(call INSTANCE_OR_ALL_GUARD,instance-status)
	@$(call ENGINE_FLEET_REFUSAL)
	@set -euo pipefail; \
	if [ "$(ALL)" = "1" ]; then names=$$($(DISCOVER_INSTANCE_NAMES)); else names="$(INSTANCE)"; fi; \
	[ -n "$$names" ] || { printf 'No instances configured under remote-instances/; nothing to report.\n'; exit 0; }; \
	while IFS= read -r name; do \
		PYTHONPATH=$(DEVCONTAINER_SCRIPTS_DIR) python3 -m devcontainer_config.cli instance-status "$$name" || exit 1; \
	done <<< "$$names"

# Dry run: init (bootstrapping the EC2 instance's state bucket, or ensuring
# the azure instance's state store, on the very first run, see
# TERRAGRUNT_INIT_HELPER), then plan. Never applies anything. ec2 and azure
# by kind: Terragrunt reaches either kind's deployment, and the init work
# the kind needs runs inside tg_init, so each instance is resolved and
# ssh-refused before any Terragrunt runs; an ssh engine is refused naming
# the engine-* surface.
instance-plan:
	@$(call INSTANCE_OR_ALL_GUARD,instance-plan)
	@$(call ENGINE_FLEET_REFUSAL)
	@set -euo pipefail; \
	. $(RD_DIR)/lib.sh; \
	$(TERRAGRUNT_INIT_HELPER); \
	if [ "$(ALL)" = "1" ]; then names=$$($(DISCOVER_INSTANCE_NAMES)); else names="$(INSTANCE)"; fi; \
	[ -n "$$names" ] || { printf 'No instances configured under remote-instances/; nothing to plan.\n'; exit 0; }; \
	while IFS= read -r name; do \
		printf '\033[0;36m[PLAN]\033[0m %s\n' "$$name"; \
		unset RD_INSTANCE_RESOLVED RD_RESOLVE_DIAGNOSIS; \
		INSTANCE="$$name" rd_resolve_instance; \
		kind="$$(rd_engine_kind)"; \
		case "$$kind" in ec2|azure|gcp) ;; *) rd_require_engine_kind ec2 "$(EC2_KIND_REMEDY)" ;; esac; \
		tg_init "$$name"; \
		terragrunt plan || exit 1; \
	done <<< "$$names"

# Power: stop or start, waiting for the target state (and, on start, for
# the engine agent) before reporting done. Abort-on-fail so a half-powered
# fleet never looks converged. ec2 azure and gcp by kind: each instance is
# resolved and ssh-refused before its platform is called, the EC2 branch
# keeps its --region flag and the region guard that requires it, and the
# azure and gcp branches carry neither (az is ambiently logged in and the
# VM is addressed by the record the deploy wrote; gcloud is ambiently
# logged in and the VM is addressed by the same record, whose
# zone and project the cli's own arms read).
instance-stop:
	@$(call INSTANCE_OR_ALL_GUARD,instance-stop)
	@$(call ENGINE_FLEET_REFUSAL)
	@set -euo pipefail; \
	. $(RD_DIR)/lib.sh; \
	if [ "$(ALL)" = "1" ]; then names=$$($(DISCOVER_INSTANCE_NAMES)); else names="$(INSTANCE)"; fi; \
	[ -n "$$names" ] || { printf 'No instances configured under remote-instances/; nothing to stop.\n'; exit 0; }; \
	while IFS= read -r name; do \
		unset RD_INSTANCE_RESOLVED RD_RESOLVE_DIAGNOSIS; \
		INSTANCE="$$name" rd_resolve_instance; \
		kind="$$(rd_engine_kind)"; \
		case "$$kind" in ec2|azure|gcp) ;; *) rd_require_engine_kind ec2 "$(EC2_KIND_REMEDY)" ;; esac; \
		if [ "$$kind" = "ec2" ]; then \
			$(REMOTE_AWS_REGION_GUARD) \
			PYTHONPATH=$(DEVCONTAINER_SCRIPTS_DIR) python3 -m devcontainer_config.cli instance-stop "$$name" --region "$$REMOTE_AWS_REGION" || exit 1; \
		else \
			PYTHONPATH=$(DEVCONTAINER_SCRIPTS_DIR) python3 -m devcontainer_config.cli instance-stop "$$name" || exit 1; \
		fi; \
	done <<< "$$names"

instance-start:
	@$(call INSTANCE_OR_ALL_GUARD,instance-start)
	@$(call ENGINE_FLEET_REFUSAL)
	@set -euo pipefail; \
	. $(RD_DIR)/lib.sh; \
	if [ "$(ALL)" = "1" ]; then names=$$($(DISCOVER_INSTANCE_NAMES)); else names="$(INSTANCE)"; fi; \
	[ -n "$$names" ] || { printf 'No instances configured under remote-instances/; nothing to start.\n'; exit 0; }; \
	while IFS= read -r name; do \
		unset RD_INSTANCE_RESOLVED RD_RESOLVE_DIAGNOSIS; \
		INSTANCE="$$name" rd_resolve_instance; \
		kind="$$(rd_engine_kind)"; \
		case "$$kind" in ec2|azure|gcp) ;; *) rd_require_engine_kind ec2 "$(EC2_KIND_REMEDY)" ;; esac; \
		if [ "$$kind" = "ec2" ]; then \
			$(REMOTE_AWS_REGION_GUARD) \
			PYTHONPATH=$(DEVCONTAINER_SCRIPTS_DIR) python3 -m devcontainer_config.cli instance-start "$$name" --region "$$REMOTE_AWS_REGION" || exit 1; \
		else \
			PYTHONPATH=$(DEVCONTAINER_SCRIPTS_DIR) python3 -m devcontainer_config.cli instance-start "$$name" || exit 1; \
		fi; \
		make --no-print-directory cert-install INSTANCE="$$name" || exit 1; \
	done <<< "$$names"

# Records the EC2 id Terragrunt output in the instance's per-instance id
# store (devcontainer_config.instance_ops.link_id) -- the place the power,
# status and remote targets read it from. Deploy does this automatically;
# this target exists for an EC2 instance re-provisioned outside make. The
# id's shape is validated by the cli, which refuses anything but i- plus
# lowercase hex. EC2-only by kind: an azure engine has no EC2 id to link --
# the deploy writes its address record from the terragrunt outputs, and an
# azure engine re-provisioned outside make re-links by re-running that
# deploy; an ssh engine is refused naming the engine-* surface.
instance-link:
	@$(call INSTANCE_USAGE_GUARD,instance-link)
	@if [ -z "$(INSTANCE_ID)" ]; then \
		printf '\033[0;31m[ERROR]\033[0m INSTANCE_ID is required, e.g.: make instance-link INSTANCE=<name> INSTANCE_ID=i-0123456789abcdefg\n' >&2; \
		exit 2; \
	fi
	@set -euo pipefail; \
	. $(RD_DIR)/lib.sh; \
	INSTANCE="$(INSTANCE)" rd_resolve_instance; \
	case "$$(rd_engine_kind)" in \
		ec2) ;; \
		azure) rd_fail "instance '${INSTANCE}' is an azure engine; there is no EC2 id to link" \
			"the deploy writes the azure engine's address record from the terragrunt outputs; an engine re-provisioned outside make re-links by re-running that deploy:" \
			"  make instance-deploy INSTANCE=${INSTANCE}" ;; \
		*) rd_require_engine_kind ec2 "$(EC2_KIND_REMEDY)" ;; \
	esac
	@PYTHONPATH=$(DEVCONTAINER_SCRIPTS_DIR) python3 -m devcontainer_config.cli instance-link "$(INSTANCE)" --instance-id "$(INSTANCE_ID)"

# The SSH engine's own lifecycle. engine-init scaffolds the manifest
# (devcontainer_config.cli engine-init; the usage guard covers INSTANCE and
# the recipe covers HOST/USER, both required, with PORT and DOCKER_ADDR
# defaulted by the cli from the manifest constants). engine-verify is the
# ssh-only reachability check -- the EC2 analog is connect-start -- as ONE
# shot: resolve, refuse the wrong kind naming the analog, then drive
# devcontainer_config.transport_ssh verify, which opens the forward,
# handshakes docker through the resulting context and stops the forward,
# leaving the docker context created and no daemon running. engine-remove
# tears the engine whole down through devcontainer_config.cli engine-remove
# (devcontainer_config.instance_ops.engine_remove), behind its own
# CONFIRM=remove gate and the kind refusal.
engine-init:
	@$(call INSTANCE_USAGE_GUARD,engine-init)
	@if [ -z "$(HOST)" ] || [ -z "$(USER)" ]; then \
		printf '\033[0;31m[ERROR]\033[0m HOST and USER are required, e.g.: make engine-init INSTANCE=<name> HOST=<host> USER=<user>\n' >&2; \
		printf '        PORT= (default 22) and DOCKER_ADDR= (default /var/run/docker.sock) are optional.\n' >&2; \
		printf '        Linux engines usually serve the default socket; a Windows engine cannot forward to one -- expose Docker Desktop on tcp://localhost:2375 and pass DOCKER_ADDR=tcp://localhost:2375.\n' >&2; \
		exit 2; \
	fi
	@PYTHONPATH=$(DEVCONTAINER_SCRIPTS_DIR) python3 -m devcontainer_config.cli engine-init "$(INSTANCE)" --host "$(HOST)" --user "$(USER)" $(if $(PORT),--port $(PORT),) $(if $(DOCKER_ADDR),--docker-addr $(DOCKER_ADDR),)

engine-verify:
	@$(call INSTANCE_USAGE_GUARD,engine-verify)
	@set -euo pipefail; \
	. $(RD_DIR)/lib.sh; \
	INSTANCE="$(INSTANCE)" rd_resolve_instance; \
	rd_require_engine_kind ssh "this is the ssh engine reachability check; the EC2 analog is: make connect-start INSTANCE=$(INSTANCE)"; \
	ctx="$$(rd_engine_context)"; \
	[ -n "$$ctx" ] || ctx="$${REMOTE_DOCKER_CONTEXT:-}"; \
	PYTHONPATH=$(DEVCONTAINER_SCRIPTS_DIR) python3 -m devcontainer_config.transport_ssh verify \
		--name "$$INSTANCE" --host "$$ENGINE_HOST" --port "$$ENGINE_PORT" \
		--user "$$ENGINE_USER" --docker-addr "$$ENGINE_DOCKER_ADDR" --context "$$ctx"

engine-remove:
	@$(call INSTANCE_USAGE_GUARD,engine-remove)
	@if [ "$(CONFIRM)" != "remove" ]; then \
		printf '\033[0;31m[ERROR]\033[0m engine-remove deletes the engine registration, its docker context, its forward and its local state; confirm it: make engine-remove INSTANCE=<name> CONFIRM=remove\n' >&2; \
		exit 1; \
	fi
	@set -euo pipefail; \
	. $(RD_DIR)/lib.sh; \
	INSTANCE="$(INSTANCE)" rd_resolve_instance; \
	rd_require_engine_kind ssh "an EC2 instance is removed by its own target, which also tears down everything engine-remove would: make instance-destroy INSTANCE=$(INSTANCE)"; \
	PYTHONPATH=$(DEVCONTAINER_SCRIPTS_DIR) python3 -m devcontainer_config.cli engine-remove "$(INSTANCE)"

# Converge one instance, or every configured instance with ALL=1, aborting
# on the first failure named. Per instance, in order: init, validate, a plan
# guarded against accidental replacement (REFUSES below unless
# CONFIRM=replace) whose saved plan file IS what apply consumes -- the guard
# reads the plan's own output, then `apply -auto-approve tfplan.deploy`
# applies exactly what was guarded, never a re-plan; the file is removed by
# the per-instance EXIT trap on every exit path -- and a plan that fails
# prints its captured output plus the stale-lock remedy, because a deploy
# killed mid-plan leaves the state backend's lock in place and the next
# plan dies acquiring it (the lock id to force-unlock is in that output) --
# then the kind's own
# addressing record: the azure branch reads `terragrunt output -json` and
# writes the engine-address record from the module outputs (every required
# key must answer or the deploy fails naming the missing ones), the gcp
# branch reads the same output and writes the same record from its own
# outputs (instance_name, zone, project_id, region, secrets_prefix and
# state_bucket, the keys the tunnel, the publication and the state store
# read back), the EC2 branch links the output id -- then converge the
# missing trust chain (cert
# material and published TLS material are created only when the status
# probes report them absent -- when params are already present the
# publish/install steps are skipped entirely, so a live daemon is never
# restarted; each probe must answer exactly true or false -- empty, null or
# malformed fails the deploy naming the instance and the raw status output;
# and the installed marker, once present, must name the VM it was installed
# on, so an azure instance replaced under CONFIRM=replace reinstalls its
# daemon material onto the new VM),
# then push-secrets. Prints the follow-on chain, which blocks on `make
# remote` and so is printed rather than run.
instance-deploy:
	@$(call INSTANCE_OR_ALL_GUARD,instance-deploy)
	@$(call ENGINE_FLEET_REFUSAL)
	@set -euo pipefail; \
	. $(RD_DIR)/lib.sh; \
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
		set -euo pipefail; \
		printf '\033[0;36m[DEPLOY]\033[0m %s\n' "$$name"; \
		unset RD_INSTANCE_RESOLVED RD_RESOLVE_DIAGNOSIS; \
		INSTANCE="$$name" rd_resolve_instance; \
		kind="$$(rd_engine_kind)"; \
		case "$$kind" in ec2|azure|gcp) ;; *) rd_require_engine_kind ec2 "$(EC2_KIND_REMEDY)" ;; esac; \
		tg_init "$$name"; \
		plan_file="$${PWD}/tfplan.deploy"; \
		trap 'rm -f "$$plan_file"' EXIT; \
		terragrunt validate || { printf '\033[0;31m[ERROR]\033[0m %s: terragrunt validate failed (output above)\n' "$$name" >&2; exit 1; }; \
		plan_log=$$(terragrunt plan -out=tfplan.deploy 2>&1) || { \
			printf '%s\n' "$$plan_log" >&2; \
			printf '\033[0;31m[ERROR]\033[0m %s: terragrunt plan failed. If the output above names a state lock (Error acquiring the state lock), an earlier deploy was killed mid-plan and its lock survived; clear it and retry:\n' "$$name" >&2; \
			printf '        cd remote-instances/%s && terragrunt force-unlock <lock-id>   # <lock-id> is the lock id the output above names\n' "$$name" >&2; \
			printf '        then re-run: make instance-deploy INSTANCE=%s\n' "$$name" >&2; \
			exit 1; \
		}; \
		printf '%s\n' "$$plan_log" | grep -qE 'No changes\.|To perform exactly these actions' || { \
			printf '%s\n' "$$plan_log" >&2; \
			printf '\033[0;31m[ERROR]\033[0m %s: the plan produced no outcome summary -- Terragrunt can exit zero on a failed plan, so the guard requires an explicit success marker (No changes, or an apply hint).\n' "$$name" >&2; \
			exit 1; \
		}; \
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
		terragrunt apply -auto-approve tfplan.deploy || { printf '\033[0;31m[ERROR]\033[0m %s: terragrunt apply failed (output above)\n' "$$name" >&2; exit 1; }; \
		if [ "$$kind" = "azure" ]; then \
			outputs_json=$$(terragrunt output -json) || { \
				printf '%s\n' "$$outputs_json" >&2; \
				printf '\033[0;31m[ERROR]\033[0m %s: terragrunt output -json failed. If the output above names a state lock, a killed deploy left it behind; clear it and retry:\n' "$$name" >&2; \
				printf '        cd remote-instances/%s && terragrunt force-unlock <lock-id>   # <lock-id> is the lock id the output above names\n' "$$name" >&2; \
				printf '        then re-run: make instance-deploy INSTANCE=%s\n' "$$name" >&2; \
				exit 1; \
			}; \
			printf '%s' "$$outputs_json" | PYTHONPATH=$(DEVCONTAINER_SCRIPTS_DIR) python3 -c "import json,sys; from pathlib import Path; from devcontainer_config import instances, repo; required=['vm_name','vm_resource_id','resource_group','relay_namespace','hybrid_connection','vault_name','vault_uri','location']; payload=json.load(sys.stdin); missing=[k for k in required if k not in payload]; sys.exit('missing required terragrunt outputs: '+', '.join(missing)) if missing else None; record={k: payload[k].get('value') for k in required}; unusable=[k for k,v in record.items() if not isinstance(v,str) or not v]; sys.exit('terragrunt outputs carry no usable string value for: '+', '.join(unusable)) if unusable else None; instances.write_engine_address(repo.find_root(Path.cwd()), '$$name', record); print('recorded the engine address for $$name from the terragrunt outputs')" || { printf '\033[0;31m[ERROR]\033[0m %s: the engine address record could not be written from the terragrunt outputs\n' "$$name" >&2; exit 1; }; \
		elif [ "$$kind" = "gcp" ]; then \
			outputs_json=$$(terragrunt output -json) || { \
				printf '%s\n' "$$outputs_json" >&2; \
				printf '\033[0;31m[ERROR]\033[0m %s: terragrunt output -json failed. If the output above names a state lock, a killed deploy left it behind; clear it and retry:\n' "$$name" >&2; \
				printf '        cd remote-instances/%s && terragrunt force-unlock <lock-id>   # <lock-id> is the lock id the output above names\n' "$$name" >&2; \
				printf '        then re-run: make instance-deploy INSTANCE=%s\n' "$$name" >&2; \
				exit 1; \
			}; \
			printf '%s' "$$outputs_json" | PYTHONPATH=$(DEVCONTAINER_SCRIPTS_DIR) python3 -c "import json,sys; from pathlib import Path; from devcontainer_config import instances, repo; required=['instance_name','zone','project_id','region','secrets_prefix','state_bucket']; payload=json.load(sys.stdin); missing=[k for k in required if k not in payload]; sys.exit('missing required terragrunt outputs: '+', '.join(missing)) if missing else None; record={k: payload[k].get('value') for k in required}; unusable=[k for k,v in record.items() if not isinstance(v,str) or not v]; sys.exit('terragrunt outputs carry no usable string value for: '+', '.join(unusable)) if unusable else None; instances.write_engine_address(repo.find_root(Path.cwd()), '$$name', record); print('recorded the engine address for $$name from the terragrunt outputs')" || { printf '\033[0;31m[ERROR]\033[0m %s: the engine address record could not be written from the terragrunt outputs\n' "$$name" >&2; exit 1; }; \
		else \
			id=$$(terragrunt output -raw instance_id); \
			[ -n "$$id" ] || { \
				printf '\033[0;31m[ERROR]\033[0m terragrunt output -raw instance_id returned nothing for %s\n' "$$name" >&2; \
				printf '        Did the apply above succeed? Inspect it: cd remote-instances/%s && terragrunt output\n' "$$name" >&2; \
				exit 1; \
			}; \
			PYTHONPATH=$(DEVCONTAINER_SCRIPTS_DIR) python3 -m devcontainer_config.cli instance-link "$$name" --instance-id "$$id" || exit 1; \
		fi; \
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
			make --no-print-directory cert-client INSTANCE="$$name" || exit 1; \
		fi; \
		marker="$$HOME/.docker/certs/$$name/installed"; \
		install_needed=0; \
		if [ "$$params_present" != "true" ] || [ ! -f "$$marker" ]; then install_needed=1; fi; \
		if [ "$$install_needed" = "0" ] && [ "$$kind" = "azure" ]; then \
			recorded_vm=$$(PYTHONPATH=$(DEVCONTAINER_SCRIPTS_DIR) python3 -c "from pathlib import Path; from devcontainer_config import instances, repo; print(instances.engine_address(repo.find_root(Path.cwd()), '$$name').get('vm_resource_id', ''))") || { printf '\033[0;31m[ERROR]\033[0m %s: the engine address record could not be read for the installed-marker check\n' "$$name" >&2; exit 1; }; \
			[ "$$(cat "$$marker")" = "$$recorded_vm" ] || install_needed=1; \
		fi; \
		if [ "$$install_needed" = "0" ] && [ "$$kind" = "gcp" ]; then \
			gcp_vm=$$(PYTHONPATH=$(DEVCONTAINER_SCRIPTS_DIR) python3 -c "from pathlib import Path; from devcontainer_config import instances, repo; print(instances.engine_address(repo.find_root(Path.cwd()), '$$name')['instance_name'])") || { printf '\033[0;31m[ERROR]\033[0m %s: the engine address record could not be read for the installed-marker check\n' "$$name" >&2; exit 1; }; \
			gcp_zone=$$(PYTHONPATH=$(DEVCONTAINER_SCRIPTS_DIR) python3 -c "from pathlib import Path; from devcontainer_config import instances, repo; print(instances.engine_address(repo.find_root(Path.cwd()), '$$name')['zone'])") || exit 1; \
			gcp_project=$$(PYTHONPATH=$(DEVCONTAINER_SCRIPTS_DIR) python3 -c "from pathlib import Path; from devcontainer_config import instances, repo; print(instances.engine_address(repo.find_root(Path.cwd()), '$$name')['project_id'])") || exit 1; \
			recorded_vm=$$(gcloud compute instances describe "$$gcp_vm" --zone "$$gcp_zone" --project "$$gcp_project" --format="value(id)") || { printf '\033[0;31m[ERROR]\033[0m %s: the VM could not be described for the installed-marker check; if gcloud reported an authentication failure, run gcloud auth login and retry\n' "$$name" >&2; exit 1; }; \
			[ "$$(cat "$$marker")" = "$$recorded_vm" ] || install_needed=1; \
		fi; \
		if [ "$$install_needed" = "1" ]; then \
			[ "$$params_present" = "true" ] || make --no-print-directory cert-publish INSTANCE="$$name" || exit 1; \
			make --no-print-directory cert-install INSTANCE="$$name" || exit 1; \
		fi; \
		make --no-print-directory push-secrets INSTANCE="$$name" || exit 1; \
		printf '\033[0;32m[DONE]\033[0m %s converged. Next, in order:\n' "$$name"; \
		printf '  make remote INSTANCE=%s      # opens the forward as a background daemon, points docker at the engine\n' "$$name"; \
		printf '  make build INSTANCE=%s\n' "$$name"; \
		printf '  make reopen INSTANCE=%s\n' "$$name"; \
	) || exit 1; \
	done <<< "$$names"

# Destroy one instance, or every configured one with ALL=1 -- and ALL=1
# additionally requires CONFIRM=destroy, because a typo'd ALL should never
# be all it takes to end the fleet. Per instance: a best-effort note of what
# dies (the EC2 instance and its volumes, the azure VM and its disks, the
# gcp VM and its disks -- the instance's six secrets die with cleanup, not
# with Terragrunt; the containers on it only when its docker context
# answers within DOCKER_CHECK_TIMEOUT_SECONDS, since a dead daemon is no
# evidence either way), then terragrunt destroy, then the cli's
# instance-cleanup for the out-of-band state Terragrunt does not know
# about (EC2 params, gcp secrets, azure's local record, all kinds'
# context, certs and id). ec2 azure and gcp by kind: the EC2 branch keeps
# the region guard and the init-before-destroy tg_init, the azure branch
# carries neither (az is ambiently logged in, and the azure backend is
# Terragrunt's own azurerm state, which needs no bootstrap before a
# destroy), and the gcp branch keeps the tg_init but no region guard (the
# gcs backend must reach its bucket to read the state a destroy needs, so
# the ensure runs first; gcloud is ambiently logged in and the VM's zone
# and project come from the record the deploy wrote).
instance-destroy:
	@$(call INSTANCE_OR_ALL_GUARD,instance-destroy)
	@$(call ENGINE_FLEET_REFUSAL)
	@set -euo pipefail; \
	. $(RD_DIR)/lib.sh; \
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
		unset RD_INSTANCE_RESOLVED RD_RESOLVE_DIAGNOSIS; \
		INSTANCE="$$name" rd_resolve_instance; \
		kind="$$(rd_engine_kind)"; \
		case "$$kind" in ec2|azure|gcp) ;; *) rd_require_engine_kind ec2 "$(EC2_KIND_REMEDY)" ;; esac; \
		if [ "$$kind" = "azure" ]; then \
			printf '\033[0;36m[DESTROY]\033[0m %s: the azure VM and its disks are deleted\n' "$$name"; \
		elif [ "$$kind" = "gcp" ]; then \
			printf '\033[0;36m[DESTROY]\033[0m %s: the gcp VM and its disks are deleted\n' "$$name"; \
			printf '  its six secrets are deleted by cleanup, not by Terragrunt\n'; \
		else \
			printf '\033[0;36m[DESTROY]\033[0m %s: the EC2 instance and its volumes are deleted\n' "$$name"; \
		fi; \
		printf '  stopping any open forward first\n'; \
		PYTHONPATH=$(DEVCONTAINER_SCRIPTS_DIR) python3 -m devcontainer_config.forwards stop "$$name" || exit 1; \
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
		if [ "$$kind" = "azure" ]; then \
			cd "$(CURDIR)/remote-instances/$$name" || exit 1; \
			terragrunt destroy -auto-approve; \
			cd "$(CURDIR)"; \
			PYTHONPATH=$(DEVCONTAINER_SCRIPTS_DIR) python3 -m devcontainer_config.cli instance-cleanup "$$name"; \
		elif [ "$$kind" = "gcp" ]; then \
			tg_init "$$name"; \
			terragrunt destroy -auto-approve; \
			cd "$(CURDIR)"; \
			PYTHONPATH=$(DEVCONTAINER_SCRIPTS_DIR) python3 -m devcontainer_config.cli instance-cleanup "$$name"; \
		else \
			$(REMOTE_AWS_REGION_GUARD) \
			tg_init "$$name"; \
			terragrunt destroy -auto-approve; \
			cd "$(CURDIR)"; \
			PYTHONPATH=$(DEVCONTAINER_SCRIPTS_DIR) python3 -m devcontainer_config.cli instance-cleanup "$$name" --region "$$REMOTE_AWS_REGION"; \
		fi; \
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
	@$(call CONTAINER_FLEET_DISPATCH,clean)

rebuild:
	@INSTANCE="$(INSTANCE)" $(CONTAINER_SH) rebuild

push-secrets:
	@INSTANCE="$(INSTANCE)" $(SECRETS_SH)

# spec Section 4.1.2 (E6-F1-S1-T2): client and CA expiry per instance, the
# inspection half of the `certs` module. Exit 0 when every certificate is
# outside the warning window (including a RENEW row for one still valid but
# inside it), exit 1 when any has expired. Fleet-wide over the remote
# engines: both the EC2 and the azure kind carry TLS material locally, so a
# fleet mixing the two is reported; only an ssh engine is refused, naming
# its kind's remedy rather than rendering silence for it.
cert-status:
	@. $(RD_DIR)/lib.sh; \
	names=$$($(DISCOVER_INSTANCE_NAMES)) || exit 1; \
	while IFS= read -r name; do \
		[ -n "$$name" ] || continue; \
		unset RD_INSTANCE_RESOLVED RD_RESOLVE_DIAGNOSIS; \
		INSTANCE="$$name" rd_resolve_instance; \
		case "$$(rd_engine_kind)" in ec2|azure|gcp) ;; *) rd_require_engine_kind ec2 "$(CERT_KIND_REMEDY)" ;; esac; \
	done <<< "$$names"; \
	PYTHONPATH=$(DEVCONTAINER_SCRIPTS_DIR) python3 -m devcontainer_config.certs status

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

# Readiness poll before cert-install: a freshly applied (or started)
# instance's engine agent answers before its install path can succeed, and
# an install issued too early fails on a daemon that does not exist yet.
# This waits (readiness detection, never a fixed sleep) per kind: the azure
# branch polls through the cli's azure-wait-agent (the shared
# transport_azure readiness poll, bounded by AZURE_AGENT_READY_TIMEOUT),
# and the EC2 branch waits until both EC2 status checks report Ok for the
# instance the id store names, polling at the same cadence instance_ops
# uses for power transitions. Then certs.sh install runs the kind's own
# install arm, and the installed marker is written for the converge step
# to check, per kind: on EC2 the bare marker the converge has always
# checked, whose presence alone means installed; on azure the marker
# records the current VM's resource id, read from the engine address
# record, so converge can tell an install performed on a VM that no longer
# exists -- a CONFIRM=replace replaces the VM while the vault secrets and
# this marker survive -- from one performed on the VM now running, and
# reinstalls the daemon material when they disagree.
# Have the instance fetch the published material and start its daemon.
# Per kind: an azure VM pulls through its managed identity over the relay
# (the wait-agent gate first, then the install, then the marker naming the
# VM); an EC2 instance pulls over SSM (the status-check gate first, then
# the install, then the marker); a gcp VM has no run-command channel at
# all, so the install is boot-applied -- the published checksum is compared
# against the VM's applied checksum guest attribute, an already-applied VM
# is done without a reboot, and a drifted or absent one is reset (started
# first when TERMINATED, which refuses a reset) before the bounded wait
# `gcp-applied-checksum` runs, the same accessor the azure arm reaches its
# own wait through. The marker names the VM the material was applied to in
# every kind's own spelling, so an instance replaced under CONFIRM=replace
# reinstalls.
cert-install:
	@set -euo pipefail; \
	. $(RD_DIR)/lib.sh; \
	INSTANCE="$(INSTANCE)" rd_resolve_instance; \
	kind="$$(rd_engine_kind)"; \
	if [ "$$kind" = "azure" ]; then \
		PYTHONPATH=$(DEVCONTAINER_SCRIPTS_DIR) python3 -m devcontainer_config.cli azure-wait-agent "$(INSTANCE)" || exit 1; \
	elif [ "$$kind" = "gcp" ]; then \
		applied=$$(PYTHONPATH=$(DEVCONTAINER_SCRIPTS_DIR) python3 -c "from pathlib import Path; from devcontainer_config import instance_ops, instances, repo, transport_gcp; name='$(INSTANCE)'; record=instances.engine_address(repo.find_root(Path.cwd()), name); print('applied' if transport_gcp.applied_checksum_matches(record, instance_ops.subprocess_runner, instance=name) else 'drifted')") || { printf '\033[0;31m[ERROR]\033[0m %s: the applied-checksum comparison could not run; the record and the gcloud session are named above\n' "$(INSTANCE)" >&2; exit 1; }; \
		gcp_vm=$$(PYTHONPATH=$(DEVCONTAINER_SCRIPTS_DIR) python3 -c "from pathlib import Path; from devcontainer_config import instances, repo; print(instances.engine_address(repo.find_root(Path.cwd()), '$(INSTANCE)')['instance_name'])") || exit 1; \
		gcp_zone=$$(PYTHONPATH=$(DEVCONTAINER_SCRIPTS_DIR) python3 -m devcontainer_config.cli gcp-zone "$(INSTANCE)") || exit 1; \
		gcp_project=$$(PYTHONPATH=$(DEVCONTAINER_SCRIPTS_DIR) python3 -m devcontainer_config.cli gcp-project-id "$(INSTANCE)") || exit 1; \
		gcp_vm_id=$$(gcloud compute instances describe "$$gcp_vm" --zone "$$gcp_zone" --project "$$gcp_project" --format="value(id)") || { printf '\033[0;31m[ERROR]\033[0m %s: the VM could not be described; if gcloud reported an authentication failure, run gcloud auth login and retry\n' "$(INSTANCE)" >&2; exit 1; }; \
		if [ -f "$$HOME/.docker/certs/$(INSTANCE)/installed" ] && [ "$$(cat "$$HOME/.docker/certs/$(INSTANCE)/installed")" != "$$gcp_vm_id" ]; then \
			printf '\033[0;36m[APPLY]\033[0m %s: the marker names a different VM id than the one running (a replaced VM); reinstalling\n' "$(INSTANCE)"; \
			applied=drifted; \
		fi; \
		if [ "$$applied" != "applied" ]; then \
			gcp_status=$$(gcloud compute instances describe "$$gcp_vm" --zone "$$gcp_zone" --project "$$gcp_project" --format="value(status)") || { printf '\033[0;31m[ERROR]\033[0m %s: the VM status could not be read; if gcloud reported an authentication failure, run gcloud auth login and retry\n' "$(INSTANCE)" >&2; exit 1; }; \
			if [ "$$gcp_status" = "TERMINATED" ]; then \
				printf '\033[0;36m[APPLY]\033[0m %s: the VM is stopped; starting it to apply the material\n' "$(INSTANCE)"; \
				gcloud compute instances start "$$gcp_vm" --zone "$$gcp_zone" --project "$$gcp_project" || exit 1; \
			else \
				printf '\033[0;36m[APPLY]\033[0m %s: the applied material has drifted; resetting the VM to apply it\n' "$(INSTANCE)"; \
				gcloud compute instances reset "$$gcp_vm" --zone "$$gcp_zone" --project "$$gcp_project" || exit 1; \
			fi; \
		else \
			printf '\033[0;36m[APPLY]\033[0m %s: the applied material already matches the published checksum; no reboot\n' "$(INSTANCE)"; \
		fi; \
		PYTHONPATH=$(DEVCONTAINER_SCRIPTS_DIR) python3 -m devcontainer_config.cli gcp-applied-checksum "$(INSTANCE)" || exit 1; \
	elif [ "$$kind" = "ec2" ]; then \
		id=$$(PYTHONPATH=$(DEVCONTAINER_SCRIPTS_DIR) python3 -m devcontainer_config.cli instance-status "$(INSTANCE)" --json | python3 -c "import json,sys; print(json.loads(sys.stdin.read())['recorded_id'] or '')"); \
		[ -n "$$id" ] || { printf '\033[0;31m[ERROR]\033[0m no EC2 instance id is recorded for instance '%s'. Link it first: make instance-link INSTANCE=<name>\n' "$(INSTANCE)" >&2; exit 2; }; \
		ok=0; \
		for i in $$(seq 1 60); do \
			state=$$(aws ec2 describe-instance-status --instance-ids "$$id" --include-all-instances --query 'InstanceStatuses[0].[SystemStatus.Status,InstanceStatus.Status]' --output text 2>/dev/null || true); \
			[ "$$state" = "ok	ok" ] && { ok=1; break; }; \
			sleep 5; \
		done; \
		[ "$$ok" = "1" ] || { printf '\033[0;31m[ERROR]\033[0m %s: EC2 status checks did not reach ok within the poll window (last: %s)\n' "$(INSTANCE)" "$$state" >&2; exit 1; }; \
	else \
		rd_require_engine_kind ec2 "$(CERT_KIND_REMEDY)"; \
	fi; \
	if [ "$$kind" != "gcp" ]; then \
		INSTANCE="$(INSTANCE)" $(CERTS_SH) install; \
	fi; \
	mkdir -p "$$HOME/.docker/certs/$(INSTANCE)"; \
	if [ "$$kind" = "azure" ]; then \
		PYTHONPATH=$(DEVCONTAINER_SCRIPTS_DIR) python3 -c "from pathlib import Path; from devcontainer_config import instances, repo; print(instances.engine_address(repo.find_root(Path.cwd()), '$(INSTANCE)').get('vm_resource_id', ''))" > "$$HOME/.docker/certs/$(INSTANCE)/installed"; \
	elif [ "$$kind" = "gcp" ]; then \
		printf '%s\n' "$$gcp_vm_id" > "$$HOME/.docker/certs/$(INSTANCE)/installed"; \
	else \
		touch "$$HOME/.docker/certs/$(INSTANCE)/installed"; \
	fi

proxy-start:
	@$(PROXY_ENV) $(PROXY_SH) start

proxy-stop:
	@$(PROXY_ENV) $(PROXY_SH) stop

proxy-restart:
	@$(PROXY_ENV) $(PROXY_SH) restart

proxy-status:
	@$(PROXY_ENV) $(PROXY_SH) status

lint: lint-private lint-nested lint-workspace lint-json lint-sh lint-dispatch lint-md lint-spell lint-prose lint-secrets
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

# The CI test job's coverage step, local: the same command CI runs, guarded
# by the same prerequisite tools `test` guards. The step's own
# --cov-fail-under overrides pyproject.toml [tool.coverage.report] fail_under
# should the two ever diverge; a red coverage step always reproduces here.
coverage:
	@printf '\033[0;36m[TEST]\033[0m coverage gate over devcontainer_config\n'
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
	@$(PYTEST) --cov=.claude/plugins/devcontainer/scripts/devcontainer_config --cov-fail-under=90

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

# The prose gate (.devcontainer/lint-prose.py): no em or en dash in prose,
# no run of two or more consecutive prose lines outside fences, tables,
# headings, list items and quotes. Findings name file, line and the
# offending text; there is no ignore list (fail-fast, per CLAUDE.md).
lint-prose:
	@printf '\033[0;36m[LINT]\033[0m prose, dashes and paragraphs (%s files)\n' "$(words $(PROSE_FILES))"
	@python3 .devcontainer/lint-prose.py $(PROSE_FILES)

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

# Runs on pre-push (E2-F2-S1-T1), now the full CI set in order: the same
# lint pre-commit runs, then a secrets scan of every commit in the pushed
# range, derived from git's own pre-push stdin and read by
# devcontainer_config.githooks, then the same test and coverage steps the
# CI test job runs. lint runs first so a pre-push failure never reaches the
# (slower) history scan needlessly. stdin: git's ref list flows through the
# range scan's recipe line unredirected, into hooks-pre-push, the one
# reader; the two make invocations after it take < /dev/null so neither
# pytest run can consume git's stdin.
hooks-run-push: lint
	@PYTHONPATH=$(DEVCONTAINER_SCRIPTS_DIR) python3 -m devcontainer_config.cli hooks-pre-push
	@$(MAKE) --no-print-directory test < /dev/null
	@$(MAKE) --no-print-directory coverage < /dev/null

# Hook content lives in devcontainer_config.githooks, not here (E2-F2-S1-T1):
# this delegates instead of writing hook bodies inline, so that content has
# exactly one source. install_hooks is idempotent and refuses to overwrite a
# hook it did not author.
hooks-install:
	@PYTHONPATH=$(DEVCONTAINER_SCRIPTS_DIR) python3 -m devcontainer_config.cli hooks-install

hooks-uninstall:
	@rm -f .git/hooks/pre-commit .git/hooks/pre-push
	@printf '\033[0;32m[DONE]\033[0m removed pre-commit and pre-push hooks\n'

# The skills surface (U3): thin delegations to the devcontainer_config.cli
# subcommands of the same name, exactly like the instance-* layer. The
# engine -- symlink creation, refusals, listing and the printed (never
# executed) runtime incantations -- lives in
# devcontainer_config.skills_install; this layer only passes the AGENT and
# SCOPE selectors (defaulted above, validated with their accepted sets by
# the cli's argparse choices=). Native-Mac operations: no devcontainer,
# docker or AWS involvement anywhere under these targets.
skills-install:
	@PYTHONPATH=$(DEVCONTAINER_SCRIPTS_DIR) python3 -m devcontainer_config.cli skills-install --agent "$(AGENT)" --scope "$(SCOPE)"

skills-remove:
	@PYTHONPATH=$(DEVCONTAINER_SCRIPTS_DIR) python3 -m devcontainer_config.cli skills-remove --agent "$(AGENT)" --scope "$(SCOPE)"

skills-list:
	@PYTHONPATH=$(DEVCONTAINER_SCRIPTS_DIR) python3 -m devcontainer_config.cli skills-list --agent "$(AGENT)" --scope "$(SCOPE)"
