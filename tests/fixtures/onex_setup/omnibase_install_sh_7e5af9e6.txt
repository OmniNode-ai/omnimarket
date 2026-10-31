#!/usr/bin/env bash
set -euo pipefail

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}==>${NC} $*"; }
warn()  { echo -e "${YELLOW}==>${NC} $*"; }
error() { echo -e "${RED}ERROR:${NC} $*" >&2; }

# ------------------------------------------------------------------
# 0. Resolve and export OMNIBASE_PATH
# ------------------------------------------------------------------
# OMNIBASE_PATH is the canonical workspace root that every sibling repo
# clone hangs off of ($OMNIBASE_PATH/<repo>). It is the product name for
# that root, and it is the only name this installer asks you to export
# (OMN-16849 / OMN-16855).
#
# A second variable, OMNI_HOME, is exported to the SAME directory a few
# lines below. It is a temporary bridge, not a second parameter: some
# packaged tools still read that older spelling, and the bridge is removed
# by OMN-16856 once they move. See the comment at that export.
#
# Derived, never hardcoded: OMNIBASE_PATH=$REPOS_DIR, computed from this
# script's own resolved location. Fails fast — no silent default — when
# that location cannot be determined, which is exactly the "piped via
# stdin" case (curl ... | bash, or bash < install.sh): bash never
# populates BASH_SOURCE[0] for a script read from stdin, so this is
# checked explicitly, before ever calling dirname/cd on it — a plain
# `${BASH_SOURCE[0]}` reference under `set -u` would abort on "unbound
# variable" here instead of reaching this message, and `dirname ""`
# resolves to "." (the caller's $PWD), which would silently derive
# OMNIBASE_PATH from the wrong directory instead of failing at all.
if [ -z "${BASH_SOURCE[0]:-}" ]; then
    error "Could not determine this script's own location — cannot derive OMNIBASE_PATH."
    error "This installer must be run as './install.sh' or 'bash install.sh' from a real checkout, not piped via stdin (e.g. 'curl ... | bash')."
    exit 1
fi
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPOS_DIR="$SCRIPT_DIR/repos"
export OMNIBASE_PATH="$REPOS_DIR"
info "OMNIBASE_PATH resolved to $OMNIBASE_PATH"

# TEMPORARY BRIDGE — remove with OMN-16856.
# The omnimarket sweep and orchestration nodes (contract_sweep, dod_verify,
# runtime_sweep and the rest of that family) still resolve the workspace
# root from OMNI_HOME, and so does the Market skill co-install script. They
# are not renamed yet: whether a self-hoster runs those nodes against their
# own tree is an open scope question on OMN-16856, and renaming the reads
# before it is answered would move ~200 sites on a guess. Until then this
# installer sets BOTH names to the SAME derived directory, so following the
# documented OMNIBASE_PATH instructions cannot leave those nodes unable to
# resolve a root. Nothing reads two names; one directory has two labels.
export OMNI_HOME="$OMNIBASE_PATH"

# ------------------------------------------------------------------
# 0b. Choose the install path (OMN-20123)
# ------------------------------------------------------------------
# Three install paths, and Docker belongs to exactly one of them:
#   local   fully local runtime: in-memory event bus, local SQLite state,
#           no Docker, Kafka or Postgres. The default.
#   docker  self-hosted Docker stack (PostgreSQL, Redpanda, Valkey) started
#           from repos/omnibase_infra; the runtime uses its Kafka bus.
#   cloud   coming later; listed so the choice is visible, not selectable.
# Chosen by --path, or asked for when run in a terminal. With no flag and no
# terminal (an agent, CI, a pipe) it is local, so an unattended install never
# stops on a prompt and never needs Docker.
#
# The chosen path is written as the workspace's runtime configuration (step
# 7 below). --switch-path rewrites only that configuration, so a user can move
# between paths later without reinstalling.
RUNTIME_CONFIG_DIR="$OMNIBASE_PATH/config/onex/runtime"
RUNTIME_CONFIG_FILE="$RUNTIME_CONFIG_DIR/runtime_config.yaml"
RUNTIME_CONFIG_MARKER="# generated-by: omnibase install.sh"

usage() {
    cat <<'EOF'
Usage: ./install.sh [--path local|docker] [--allow-brew-python]
       ./install.sh --switch-path local|docker
       ./install.sh --clone-only [--repos FILE] [--into DIR]

  --path local        fully local runtime, no Docker (the default)
  --path docker       self-hosted Docker stack (PostgreSQL, Redpanda, Valkey)
  --switch-path PATH  rewrite this install's runtime configuration for PATH,
                      without cloning or building anything
  --allow-brew-python when no Python 3.12+ is on PATH and uv cannot install
                      one, install python@3.12 with Homebrew without asking
  --clone-only        only the clone step: clone every repository FILE lists
                      into DIR, install nothing. A repository already cloned is
                      skipped; one that fails to clone makes the run exit
                      non-zero and is named. Needs only git, over whatever
                      credential git is logged in with.
  --repos FILE        the repos file --clone-only reads (default: repos.yaml
                      beside this script)
  --into DIR          the directory --clone-only clones into (default: the
                      workspace root OMNIBASE_PATH)

The cloud path is coming later and is not selectable yet.
With no --path, the installer asks when run in a terminal and uses local
otherwise.
EOF
}

# Reads a repos file (simple line-based parsing, no yq dependency) and clones
# each entry into a directory. Prints one line per entry and a summary.
#   $1 repos file   $2 target directory
#   $3 "strict": a clone that errors, a target that is not a git checkout and a
#      name that would leave the target directory are failures, counted, named
#      and returned as a non-zero status after every entry has been tried, and
#      a file with no entries is refused. Otherwise (the full install) a clone
#      that errors is a warning and the install continues without that repo.
clone_repos() {
    local repos_file="$1" into="$2" mode="$3"
    local current_name="" cloned=0 skipped=0 failed=0 total=0
    local failed_names=()
    local line

    process_repo() {
        local name="$1" repo="$2" target
        total=$((total + 1))
        if [ "$mode" = "strict" ]; then
            case "$name" in
                ""|.*|*/*)
                    error "Refusing repository name '$name' ($repo): it is not a plain directory name under $into."
                    failed=$((failed + 1)); failed_names+=("$repo"); return 0 ;;
            esac
        fi
        target="$into/$name"
        if [ -d "$target" ]; then
            if [ "$mode" = "strict" ] && [ ! -e "$target/.git" ]; then
                error "$name: $target exists but is not a git checkout ($repo)."
                failed=$((failed + 1)); failed_names+=("$repo"); return 0
            fi
            warn "$name already cloned, skipping."
            skipped=$((skipped + 1))
        else
            info "Cloning $repo -> $name"
            # Some repos (e.g. private org repos) may not be accessible to
            # every caller. In a full install one inaccessible repo must not
            # abort the whole install under `set -e`: warn and continue.
            if git clone "https://github.com/$repo.git" "$target"; then
                cloned=$((cloned + 1))
            else
                failed=$((failed + 1)); failed_names+=("$repo")
                if [ "$mode" = "strict" ]; then
                    error "Failed to clone $repo into $target."
                else
                    warn "Failed to clone $repo (private repo or no access?), skipping."
                fi
            fi
        fi
    }

    while IFS= read -r line || [ -n "$line" ]; do
        line="${line%$'\r'}"
        if [[ "$line" =~ ^[[:space:]]*-[[:space:]]*name:[[:space:]]*([^[:space:]]+)[[:space:]]*$ ]]; then
            current_name="${BASH_REMATCH[1]}"
        elif [[ "$line" =~ ^[[:space:]]*repo:[[:space:]]*([^[:space:]]+)[[:space:]]*$ ]]; then
            process_repo "$current_name" "${BASH_REMATCH[1]}"
            current_name=""
        fi
    done < "$repos_file"

    if [ "$mode" = "strict" ]; then
        if [ "$total" -eq 0 ]; then
            error "No repositories found in $repos_file."
            return 1
        fi
        info "clone-only: $cloned cloned, $skipped already cloned, $failed failed (of $total in $repos_file)"
        if [ "$failed" -gt 0 ]; then
            error "Failed: ${failed_names[*]}"
            return 1
        fi
    fi
    return 0
}

install_path=""
switch_only=0
clone_only=0
allow_brew_python=0
repos_file=""
into_dir=""
while [ $# -gt 0 ]; do
    case "$1" in
        --clone-only) clone_only=1; shift ;;
        --allow-brew-python) allow_brew_python=1; shift ;;
        --repos|--into)
            if [ $# -lt 2 ]; then
                error "$1 needs a value."
                usage >&2
                exit 2
            fi
            if [ "$1" = "--repos" ]; then repos_file="$2"; else into_dir="$2"; fi
            shift 2
            ;;
        --repos=*) repos_file="${1#--repos=}"; shift ;;
        --into=*) into_dir="${1#--into=}"; shift ;;
        --path|--switch-path)
            [ "$1" = "--switch-path" ] && switch_only=1
            if [ $# -lt 2 ]; then
                error "$1 needs a value: local or docker."
                usage >&2
                exit 2
            fi
            install_path="$2"
            shift 2
            ;;
        --path=*) install_path="${1#--path=}"; shift ;;
        --switch-path=*) install_path="${1#--switch-path=}"; switch_only=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *)
            error "Unknown argument: $1"
            usage >&2
            exit 2
            ;;
    esac
done

if [ "$clone_only" -eq 0 ] && { [ -n "$repos_file" ] || [ -n "$into_dir" ]; }; then
    error "--repos and --into belong to --clone-only."
    usage >&2
    exit 2
fi

if [ "$clone_only" -eq 1 ]; then
    if [ -n "$install_path" ]; then
        error "--clone-only only clones; it takes no --path or --switch-path."
        usage >&2
        exit 2
    fi
    if ! command -v git &>/dev/null; then
        error "git is required for --clone-only."
        exit 1
    fi
    repos_file="${repos_file:-$SCRIPT_DIR/repos.yaml}"
    if [ ! -f "$repos_file" ]; then
        error "Repos file not found: $repos_file"
        exit 1
    fi
    into_dir="${into_dir:-$OMNIBASE_PATH}"
    mkdir -p "$into_dir"
    into_dir="$(cd "$into_dir" && pwd)"
    info "Cloning the repositories in $repos_file into $into_dir (clone only)..."
    clone_repos "$repos_file" "$into_dir" strict
    exit $?
fi

# ------------------------------------------------------------------
# Finish step (scripts/install_finish_step.sh)
# ------------------------------------------------------------------
# A minimal installer copy may omit the optional greeting helper. Keep the
# install usable in that case, with no greeting at the end.
if [ -f "$SCRIPT_DIR/scripts/install_finish_step.sh" ]; then
    # shellcheck source=scripts/install_finish_step.sh
    . "$SCRIPT_DIR/scripts/install_finish_step.sh"
    # --switch-path exits before the end of the install, so it never runs.
    [ "$switch_only" -eq 0 ] && finish_prepare
else
    warn "Optional finish greeting helper not found: $SCRIPT_DIR/scripts/install_finish_step.sh; continuing without the greeting."
    finish_greeting() { return 0; }
fi

if [ -z "$install_path" ]; then
    if [ -t 0 ] && [ -t 1 ]; then
        echo ""
        echo "Choose an install path:"
        echo "  1) local   fully local runtime, no Docker (default)"
        echo "  2) docker  self-hosted Docker stack (PostgreSQL, Redpanda, Valkey)"
        echo "     cloud   coming later, not selectable yet"
        read -r -p "Install path [local]: " answer
        case "$answer" in
            ""|1|local) install_path="local" ;;
            2|docker) install_path="docker" ;;
            *) install_path="$answer" ;;
        esac
    else
        install_path="local"
        info "No --path given and no terminal to ask in: using the local install path."
    fi
fi

case "$install_path" in
    local|docker) ;;
    cloud)
        error "The cloud install path is not available yet. Choose local or docker."
        exit 2
        ;;
    *)
        error "Unknown install path '$install_path'. Choose local or docker."
        usage >&2
        exit 2
        ;;
esac
info "Install path: $install_path"

# Writes the workspace's runtime configuration for the chosen path. This is
# the tier-1 (self-hosted) runtime config the platform already reads, not a
# file of this installer's invention: `onex delegate` reads it from
# $OMNIBASE_PATH/config/onex/runtime/runtime_config.yaml whenever
# OMNIBASE_PATH is set (and refuses to run without it once OMNIBASE_PATH is
# set), and the runtime kernel reads it when ONEX_CONTRACTS_DIR points at
# $OMNIBASE_PATH/config/onex. It declares the transport only; the broker
# address of the docker path comes from the stack's own .env settings.
#
# A file this installer did not write is never overwritten: the first line
# marks the installer's own copies, and anything else is refused by name.
write_runtime_config() {
    local path_choice="$1"
    if [ -f "$RUNTIME_CONFIG_FILE" ] && [ "$(head -n 1 "$RUNTIME_CONFIG_FILE")" != "$RUNTIME_CONFIG_MARKER" ]; then
        error "$RUNTIME_CONFIG_FILE exists and was not written by this installer."
        error "It is left untouched. Move it aside to let the installer write the $path_choice configuration."
        return 1
    fi
    local bus_type description
    case "$path_choice" in
        local)
            bus_type="inmemory"
            description="omnibase install path local: fully local runtime, in-memory event bus, no Docker"
            ;;
        docker)
            bus_type="kafka"
            description="omnibase install path docker: self-hosted Docker stack, Kafka-compatible event bus"
            ;;
    esac
    mkdir -p "$RUNTIME_CONFIG_DIR"
    local tmp
    tmp="$(mktemp "$RUNTIME_CONFIG_DIR/.runtime_config.XXXXXX")"
    cat > "$tmp" <<EOF
$RUNTIME_CONFIG_MARKER
# install-path: $path_choice
# python: $PYTHON_BIN
#
# This workspace's runtime configuration, written for the install path above.
# Rewrite it for another path with:  make switch-path INSTALL_PATH=<local|docker>
# (or ./install.sh --switch-path <local|docker>). Hand edits are kept only
# if you delete the first line, after which the installer never touches it.
name: "omnibase-$path_choice"
description: "$description"
input_topic: "requests"
output_topic: "responses"
group_id: "onex-runtime"
event_bus:
  type: "$bus_type"
  profile: "local"
  environment: "local"
  max_history: 1000
  circuit_breaker_threshold: 5
EOF
    mv "$tmp" "$RUNTIME_CONFIG_FILE"
    info "Wrote the $path_choice runtime configuration to $RUNTIME_CONFIG_FILE"
}

# ------------------------------------------------------------------
# 0a. Platform check (Intel macOS builds from source)
# ------------------------------------------------------------------
# Apple Silicon macOS and Linux (x86_64, arm64) install from prebuilt
# wheels. Intel macOS is supported by building from source: the
# `cryptography` release the platform requires publishes no x86_64 macOS
# wheel, so uv compiles it and needs a Rust toolchain and Homebrew OpenSSL.
# Without them the failure surfaces deep inside an openssl-sys build. This
# check runs before any clone or package install, states the path up front,
# and stops with the exact commands when a prerequisite is missing.
host_os="$(uname -s)"
host_arch="$(uname -m)"
if [ "$host_os" = "Darwin" ] && [ "$host_arch" = "x86_64" ]; then
    if [ "$(sysctl -n sysctl.proc_translated 2>/dev/null || true)" = "1" ]; then
        error "This shell is an x86_64 process running under Rosetta on an Apple Silicon Mac."
        error "Open a native (arm64) terminal and re-run: Apple Silicon installs from prebuilt wheels and needs no compiler."
        exit 1
    fi
    warn "Intel macOS detected. Supported platforms: Apple Silicon macOS and Linux (x86_64, arm64) install from wheels; Intel macOS builds from source."
    intel_missing=()
    if ! command -v brew &>/dev/null; then
        intel_missing+=("Homebrew (https://brew.sh)")
    elif ! brew --prefix openssl@3 &>/dev/null || [ ! -d "$(brew --prefix openssl@3)/include/openssl" ]; then
        intel_missing+=("OpenSSL 3:  brew install openssl@3")
    fi
    if ! command -v cargo &>/dev/null || ! command -v rustc &>/dev/null; then
        intel_missing+=("a Rust toolchain:  brew install rust")
    fi
    if ! xcode-select -p &>/dev/null; then
        intel_missing+=("Apple command line tools:  xcode-select --install")
    fi
    if [ ${#intel_missing[@]} -gt 0 ]; then
        error "Intel macOS builds some dependencies from source. Install these first, then re-run ./install.sh:"
        for dep in "${intel_missing[@]}"; do
            echo "  - $dep" >&2
        done
        error "Nothing has been cloned or installed yet. Details: supported platforms guide in the OmniNode knowledge base."
        exit 1
    fi
    OPENSSL_DIR="$(brew --prefix openssl@3)"
    export OPENSSL_DIR
    info "Intel macOS from-source prerequisites found (OPENSSL_DIR=$OPENSSL_DIR). The first build compiles cryptography and takes several minutes."
fi

# ------------------------------------------------------------------
# 1. Check prerequisites
# ------------------------------------------------------------------
info "Checking prerequisites..."

missing=()

if ! command -v git &>/dev/null; then
    missing+=("git")
fi

# Docker is a prerequisite of the docker path only. The local path never
# starts a container, so a host without Docker installs it fully.
if [ "$install_path" = "docker" ]; then
    if ! command -v docker &>/dev/null; then
        missing+=("docker (the docker install path runs the self-hosted stack in Docker; use --path local to install without it)")
    elif ! docker compose version &>/dev/null; then
        missing+=("docker compose plugin (needed by the docker install path)")
    fi
    # The stack's own setup (repos/omnibase_infra: make local-env, make
    # up-local) generates its passwords with openssl and runs from make.
    if ! command -v openssl &>/dev/null; then
        missing+=("openssl (the docker install path generates the stack's passwords with it)")
    fi
    if ! command -v make &>/dev/null; then
        missing+=("make (the docker install path starts the stack with it)")
    fi
fi

if ! command -v uv &>/dev/null; then
    missing+=("uv (install: curl -LsSf https://astral.sh/uv/install.sh | sh)")
fi

# Check Node 20+
if command -v node &>/dev/null; then
    node_major=$(node -v | sed 's/v//' | cut -d. -f1)
    if [ "$node_major" -lt 20 ]; then
        missing+=("node20+ (found v$node_major)")
    fi
else
    missing+=("node20+")
fi

if [ ${#missing[@]} -gt 0 ]; then
    error "Missing prerequisites:"
    for dep in "${missing[@]}"; do
        echo "  - $dep"
    done
    exit 1
fi

# ------------------------------------------------------------------
# 1b. Provide Python 3.12+ (OMN-21167)
# ------------------------------------------------------------------
# An old or missing python3 is not a reason to stop: uv, which the install
# needs anyway, installs a managed Python. Homebrew is the fallback, used only
# with consent (a terminal answer, or --allow-brew-python), because it changes
# the system rather than a uv-managed directory. "Supported" is 3.12 or 3.13:
# the range every repository's requires-python accepts (omniclaude caps it
# below 3.14), so a newer python3 is passed over rather than forced on a repo
# that refuses it. The interpreter chosen here is
# exported as UV_PYTHON, so every `uv sync` below builds on it instead of the
# system python3, and it is named in the runtime configuration (step 7).
py_version_of() {
    "$1" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null
}

py_supported() {
    local version major minor
    version="$(py_version_of "$1")" || return 1
    major="${version%%.*}"
    minor="${version#*.}"
    case "$major$minor" in ""|*[!0-9]*) return 1 ;; esac
    [ "$major" -eq 3 ] && [ "$minor" -ge 12 ] && [ "$minor" -le 13 ]
}

uv_python() {
    local found
    found="$(uv python find '>=3.12,<3.14' 2>/dev/null)" || return 1
    [ -n "$found" ] && py_supported "$found" && echo "$found"
}

PYTHON_BIN=""
if command -v python3 &>/dev/null && py_supported "$(command -v python3)"; then
    PYTHON_BIN="$(command -v python3)"
else
    if command -v python3 &>/dev/null; then
        info "python3 on PATH is $(py_version_of "$(command -v python3)" || echo unknown); this install needs Python 3.12 or 3.13."
    else
        info "No python3 on PATH; this install needs Python 3.12 or 3.13."
    fi
    PYTHON_BIN="$(uv_python || true)"
    if [ -z "$PYTHON_BIN" ]; then
        info "Installing a uv-managed Python 3.12..."
        if uv python install 3.12; then
            PYTHON_BIN="$(uv_python || true)"
        else
            warn "uv could not install Python 3.12."
        fi
    fi
    if [ -z "$PYTHON_BIN" ] && command -v brew &>/dev/null; then
        consent="$allow_brew_python"
        if [ "$consent" -eq 0 ] && [ -t 0 ] && [ -t 1 ]; then
            read -r -p "Install Python 3.12 with Homebrew (brew install python@3.12)? [y/N] " answer || answer=""
            case "$answer" in y|Y|yes|YES) consent=1 ;; esac
        fi
        if [ "$consent" -eq 1 ]; then
            info "Installing python@3.12 with Homebrew..."
            if brew install python@3.12; then
                brew_python="$(brew --prefix python@3.12)/bin/python3.12"
                py_supported "$brew_python" && PYTHON_BIN="$brew_python"
            else
                warn "Homebrew could not install python@3.12."
            fi
        fi
    fi
    if [ -z "$PYTHON_BIN" ]; then
        error "No Python 3.12+ could be provided. Run: uv python install 3.12 (then re-run ./install.sh)"
        exit 1
    fi
fi
export UV_PYTHON="$PYTHON_BIN"
info "Using Python $(py_version_of "$PYTHON_BIN") at $PYTHON_BIN"

info "All prerequisites satisfied."

if [ "$switch_only" -eq 1 ]; then
    if [ ! -d "$OMNIBASE_PATH" ]; then
        error "No install at $OMNIBASE_PATH to switch. Run ./install.sh --path $install_path first."
        exit 1
    fi
    write_runtime_config "$install_path"
    info "Switched this install to the $install_path path. Nothing was cloned or rebuilt."
    if [ "$install_path" = "docker" ]; then
        echo "Start the self-hosted stack with:"
        echo "  cd $OMNIBASE_PATH/omnibase_infra && make local-env && make up-local"
    fi
    exit 0
fi

# ------------------------------------------------------------------
# 2. Clone repositories
# ------------------------------------------------------------------
info "Cloning repositories into $REPOS_DIR..."
mkdir -p "$REPOS_DIR"

clone_repos "$SCRIPT_DIR/repos.yaml" "$REPOS_DIR" lenient

# ------------------------------------------------------------------
# 3. Build Python environments
# ------------------------------------------------------------------
info "Building Python environments..."

for dir in "$REPOS_DIR"/*/; do
    repo=$(basename "$dir")
    if [ -f "$dir/pyproject.toml" ]; then
        info "Installing $repo (Python)..."
        (cd "$dir" && uv sync 2>&1 | tail -3) || warn "Failed to sync $repo (non-fatal)"
    fi
done

# ------------------------------------------------------------------
# 4. Install Node.js dependencies
# ------------------------------------------------------------------
if [ -d "$REPOS_DIR/omnidash" ] && [ -f "$REPOS_DIR/omnidash/package.json" ]; then
    info "Installing omnidash (Node.js)..."
    (cd "$REPOS_DIR/omnidash" && npm install 2>&1 | tail -3) || warn "Failed to install omnidash deps (non-fatal)"
fi

# ------------------------------------------------------------------
# 5. Install the Market skill package (Market nodes for `onex skill`)
# ------------------------------------------------------------------
# `onex skill` resolves nodes from omnimarket via a co-install into the
# omnibase_infra venv (the canonical mechanism omnibase_infra itself ships
# at scripts/install-node-skill-package.sh — never re-implemented here).
# Both repos and the infra venv must exist for this to be possible; skip
# (non-fatal, matching the rest of this script's degrade-gracefully
# posture) if either prerequisite didn't clone/build successfully above.
skill_installer="$REPOS_DIR/omnibase_infra/scripts/install-node-skill-package.sh"
infra_python="$REPOS_DIR/omnibase_infra/.venv/bin/python"
if [ -d "$REPOS_DIR/omnimarket" ] && [ -x "$skill_installer" ] && [ -x "$infra_python" ]; then
    info "Installing Market skill package (omnimarket) into the omnibase_infra venv..."
    bash "$skill_installer" --execute "$infra_python" ||
        warn "Failed to install the Market skill package (non-fatal) — re-run manually: bash $skill_installer --execute $infra_python"
else
    warn "Skipping Market skill package install: omnimarket and/or the omnibase_infra venv are not present."
    warn "Market skill nodes will show as \"Unknown node\" under 'onex skill' until you run:"
    warn "  bash $skill_installer --execute $infra_python"
fi

# ------------------------------------------------------------------
# 6. Set up environment file
# ------------------------------------------------------------------
if [ ! -f "$SCRIPT_DIR/.env" ]; then
    cp "$SCRIPT_DIR/.env.example" "$SCRIPT_DIR/.env"
    info "Created .env from template. Edit it with your configuration."
else
    info ".env already exists, skipping."
fi

if ! grep -q '^OMNIBASE_PATH=' "$SCRIPT_DIR/.env" 2>/dev/null; then
    { echo ""; echo "# Canonical workspace root (installer-derived) — required by the"; \
      echo "# omnimarket drift guard and the workspace-root node refusals."; \
      echo "OMNIBASE_PATH=$OMNIBASE_PATH"; \
      echo "# Temporary bridge for tools still on the older spelling; removed"; \
      echo "# by OMN-16856. Same directory, not a second parameter."; \
      echo "OMNI_HOME=$OMNIBASE_PATH"; } >> "$SCRIPT_DIR/.env"
fi

# ------------------------------------------------------------------
# 7. Write the runtime configuration for the chosen install path
# ------------------------------------------------------------------
write_runtime_config "$install_path"

# ------------------------------------------------------------------
# Done
# ------------------------------------------------------------------
echo ""
info "Installation complete!"
echo ""
echo "Install path: $install_path (runtime configuration: $RUNTIME_CONFIG_FILE)"
echo ""
echo "Next steps:"
echo "  1. Export OMNIBASE_PATH in your shell (required — see docs/GETTING_STARTED.md):"
echo "       export OMNIBASE_PATH=\"$OMNIBASE_PATH\""
if [ "$install_path" = "docker" ]; then
    echo "  2. Write the stack's settings (two generated passwords and your model overlay):"
    echo "       cd $OMNIBASE_PATH/omnibase_infra && make local-env"
    echo "     then set your model endpoint on the model_endpoint line of ~/.omnibase/local.bifrost.yaml"
    echo "  3. Start the self-hosted stack (PostgreSQL, Redpanda, Valkey and your own runtime):"
    echo "       cd $OMNIBASE_PATH/omnibase_infra && make up-local && make status-local"
    echo "  4. Run 'make dev' to start development servers"
    echo "  5. Run 'make status' to check everything is running"
else
    echo "  2. Run 'make dev' to start development servers"
    echo "  3. Run 'make status' to check everything is running"
    echo ""
    echo "The local path needs no Docker. Docker is used only by the docker install path"
    echo "(the self-hosted stack); switch to it later with: make switch-path INSTALL_PATH=docker"
fi
echo ""
finish_greeting
