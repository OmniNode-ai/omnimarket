#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
#
# Install the OmniNode onex command line on macOS or Linux:
#
#     curl -fsSL https://omninode.ai/install.sh | bash
#
# What it does, in order:
#   1. Detects the OS and CPU (macOS arm64 or x86_64, Linux x86_64 or arm64).
#   2. Checks the tools it needs (curl, tar, mktemp, a sha256 tool).
#   3. Uses uv if a recent one is already on PATH; otherwise downloads the
#      pinned uv release from GitHub, checks its sha256 against the value
#      written in this script, and keeps it in a private directory
#      (~/.local/share/omninode/uv). It never changes your own uv.
#   4. Refuses, before installing anything, if an onex or omni-agent it did
#      not install is already in uv's bin directory, and names the file.
#   5. Runs `uv tool install --python 3.12 --no-build omninode-onex==<version>`
#      from PyPI. uv downloads Python 3.12 itself if you do not have it.
#      Running the installer again is safe: the same version is a no-op and a
#      newer installer upgrades in place.
#   6. Runs `onex --version` and tells you how to put uv's bin directory on
#      PATH if it is not there yet.
#
# No sudo, no git, no repository clone. Everything is written under $HOME.
#
# Settings for support and tests (not needed for a normal install):
#   ONEX_INSTALL_DRY_RUN=1      detect the platform, check tools, print the
#                               plan and stop before any download.
#   ONEX_INSTALL_FIND_LINKS=DIR extra local directory of wheels for uv.
#   ONEX_INSTALL_UV_BASE_URL=U  where the pinned uv release is fetched from;
#                               the sha256 check still applies.
#
# The whole script is functions; `main` runs on the last line, so a partly
# downloaded script does nothing.

set -euo pipefail

ONEX_PACKAGE="omninode-onex"
ONEX_VERSION="0.0.2"
ONEX_PYTHON="3.12"
ONEX_LAUNCHERS="onex omni-agent"
UV_VERSION="0.13.8"
UV_MIN_VERSION="0.8.0"
UV_DEFAULT_BASE_URL="https://github.com/astral-sh/uv/releases/download"
SUPPORT_URL="https://omninode.ai"

TMP_DIR=""

if [ -t 2 ]; then
    BOLD="$(printf '\033[1m')"
    RED="$(printf '\033[31m')"
    YELLOW="$(printf '\033[33m')"
    RESET="$(printf '\033[0m')"
else
    BOLD=""
    RED=""
    YELLOW=""
    RESET=""
fi

say() { printf '%s==>%s %s\n' "$BOLD" "$RESET" "$*" >&2; }
warn() { printf '%swarning:%s %s\n' "$YELLOW" "$RESET" "$*" >&2; }
fail() {
    printf '%serror:%s %s\n' "$RED" "$RESET" "$1" >&2
    shift
    while [ "$#" -gt 0 ]; do
        printf '       %s\n' "$1" >&2
        shift
    done
    exit 1
}

cleanup() {
    if [ -n "$TMP_DIR" ] && [ -d "$TMP_DIR" ]; then
        rm -rf "$TMP_DIR"
    fi
}

# The sha256 of each pinned uv release archive, from the uv release's own
# .sha256 files. A download that does not match is never extracted or run.
uv_sha256() {
    case "$1" in
        aarch64-apple-darwin) echo "6b71028a6b904f2ae3340ef52e595512de807d06c0a20eaef2f692581b246558" ;;
        x86_64-apple-darwin) echo "a04c2ffeac28b2003a2c13caf538ab425fcd7def3e4a41571f4a8fb370a66af3" ;;
        x86_64-unknown-linux-gnu) echo "5acfd9177cfecf0bf87578f059728215ccc884388988f55f7a65041b57387d91" ;;
        aarch64-unknown-linux-gnu) echo "74f2f5c6024d397481db228db8fc25eed420db192243f88438c118e7e013e585" ;;
        *) echo "" ;;
    esac
}

need_cmd() {
    if ! command -v "$1" >/dev/null 2>&1; then
        fail "this installer needs '$1', which is not on PATH." "$2"
    fi
}

sha256_of() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | awk '{print $1}'
    else
        shasum -a 256 "$1" | awk '{print $1}'
    fi
}

# version_ge A B: true when dotted version A >= B (numeric parts only).
version_ge() {
    local a="$1" b="$2" x y
    while [ -n "$a" ] || [ -n "$b" ]; do
        x="${a%%.*}"
        y="${b%%.*}"
        x="${x%%[!0-9]*}"
        y="${y%%[!0-9]*}"
        [ -z "$x" ] && x=0
        [ -z "$y" ] && y=0
        if [ "$x" -gt "$y" ]; then return 0; fi
        if [ "$x" -lt "$y" ]; then return 1; fi
        case "$a" in *.*) a="${a#*.}" ;; *) a="" ;; esac
        case "$b" in *.*) b="${b#*.}" ;; *) b="" ;; esac
    done
    return 0
}

detect_target() {
    local os arch
    os="$(uname -s)"
    arch="$(uname -m)"
    case "$os" in
        Darwin)
            case "$arch" in
                arm64 | aarch64) echo "aarch64-apple-darwin" ;;
                x86_64)
                    # An Apple Silicon Mac running this shell under Rosetta
                    # reports x86_64; install the native build instead.
                    if [ "$(sysctl -n sysctl.proc_translated 2>/dev/null || echo 0)" = "1" ]; then
                        echo "aarch64-apple-darwin"
                    else
                        echo "x86_64-apple-darwin"
                    fi
                    ;;
                *) fail "unsupported Mac CPU '$arch'." "Supported: Apple Silicon (arm64) and Intel (x86_64)." ;;
            esac
            ;;
        Linux)
            # musl's `ldd --version` exits 1, so capture its text before matching.
            local libc_info=""
            if command -v ldd >/dev/null 2>&1; then
                libc_info="$(ldd --version 2>&1 || true)"
            fi
            case "$libc_info" in
                *[Mm]usl*)
                    fail "musl-based Linux (for example Alpine) is not supported." \
                        "Use a glibc-based distribution such as Ubuntu, Debian or Fedora."
                    ;;
            esac
            case "$arch" in
                x86_64 | amd64) echo "x86_64-unknown-linux-gnu" ;;
                aarch64 | arm64) echo "aarch64-unknown-linux-gnu" ;;
                *) fail "unsupported Linux CPU '$arch'." "Supported: x86_64 and arm64 (aarch64)." ;;
            esac
            ;;
        MINGW* | MSYS* | CYGWIN* | Windows_NT)
            fail "Windows is not supported directly." \
                "Install WSL (https://learn.microsoft.com/windows/wsl/install) and run this command inside it."
            ;;
        *)
            fail "unsupported operating system '$os'." "Supported: macOS and Linux (and Windows through WSL)."
            ;;
    esac
}

private_uv_dir() {
    echo "${XDG_DATA_HOME:-$HOME/.local/share}/omninode/uv"
}

# Prints the path of a uv this installer may use, downloading the pinned one
# when no recent uv is on PATH.
ensure_uv() {
    local target="$1" found version dir url archive expected actual
    found="$(command -v uv 2>/dev/null || true)"
    if [ -n "$found" ]; then
        version="$("$found" --version 2>/dev/null | awk '{print $2}')"
        if [ -n "$version" ] && version_ge "$version" "$UV_MIN_VERSION"; then
            say "Using uv $version from $found" >&2
            echo "$found"
            return 0
        fi
        warn "uv ${version:-of unknown version} at $found is older than $UV_MIN_VERSION; using a private copy instead."
    fi
    dir="$(private_uv_dir)"
    if [ -x "$dir/uv" ] && [ "$("$dir/uv" --version 2>/dev/null | awk '{print $2}')" = "$UV_VERSION" ]; then
        say "Using uv $UV_VERSION from $dir" >&2
        echo "$dir/uv"
        return 0
    fi
    expected="$(uv_sha256 "$target")"
    [ -n "$expected" ] || fail "no pinned uv checksum for '$target'."
    url="${ONEX_INSTALL_UV_BASE_URL:-$UV_DEFAULT_BASE_URL}/$UV_VERSION/uv-$target.tar.gz"
    archive="$TMP_DIR/uv-$target.tar.gz"
    say "Downloading uv $UV_VERSION for $target" >&2
    case "$url" in
        https://*) curl --proto '=https' --tlsv1.2 -fsSL --retry 3 -o "$archive" "$url" ||
            fail "could not download uv from $url." "Check your network connection (and any proxy) and run the installer again." ;;
        *) curl -fsSL -o "$archive" "$url" || fail "could not download uv from $url." ;;
    esac
    actual="$(sha256_of "$archive")"
    if [ "$actual" != "$expected" ]; then
        fail "the downloaded uv does not match its published sha256, so it was not installed." \
            "expected $expected" "got      $actual" \
            "Nothing was changed. Try again later, or report it at $SUPPORT_URL."
    fi
    tar -xzf "$archive" -C "$TMP_DIR" || fail "could not unpack the uv archive."
    [ -x "$TMP_DIR/uv-$target/uv" ] || fail "the uv archive did not contain uv-$target/uv."
    mkdir -p "$dir"
    cp "$TMP_DIR/uv-$target/uv" "$dir/uv.new"
    chmod 755 "$dir/uv.new"
    mv -f "$dir/uv.new" "$dir/uv"
    say "Installed uv $UV_VERSION in $dir (checksum verified)" >&2
    echo "$dir/uv"
}

# Refuse when a launcher already in uv's bin directory belongs to anything
# other than this package, and leave that file untouched.
check_conflicts() {
    local uv="$1" bin_dir="$2" name path owner
    for name in $ONEX_LAUNCHERS; do
        path="$bin_dir/$name"
        if [ ! -e "$path" ] && [ ! -L "$path" ]; then
            continue
        fi
        owner="$("$uv" tool list 2>/dev/null | awk -v want="- $name" '
            /^- / { if ($0 == want) { print tool; exit } ; next }
            NF { tool = $1 }')"
        if [ "$owner" = "$ONEX_PACKAGE" ]; then
            continue
        fi
        if [ -n "$owner" ]; then
            fail "$path is already provided by the uv tool '$owner'." \
                "Remove it with:  $uv tool uninstall $owner" \
                "then run this installer again. Nothing was changed."
        fi
        fail "$path already exists and was not installed by uv (for example by pipx or by hand)." \
            "Remove it (for example 'pipx uninstall <package>' or 'rm $path')," \
            "then run this installer again. Nothing was changed."
    done
}

path_guidance() {
    local bin_dir="$1" shell_name rc
    case ":$PATH:" in
        *":$bin_dir:"*) return 0 ;;
    esac
    shell_name="$(basename "${SHELL:-sh}")"
    case "$shell_name" in
        zsh) rc="$HOME/.zshrc" ;;
        bash) if [ "$(uname -s)" = "Darwin" ]; then rc="$HOME/.bash_profile"; else rc="$HOME/.bashrc"; fi ;;
        *) rc="your shell's startup file" ;;
    esac
    warn "$bin_dir is not on your PATH yet."
    if [ "$shell_name" = "fish" ]; then
        printf '       Run:  fish_add_path %s\n' "$bin_dir" >&2
    else
        printf '       Add this line to %s, then open a new terminal:\n' "$rc" >&2
        # shellcheck disable=SC2016 # $PATH is printed literally for the user to paste.
        printf '           export PATH="%s:$PATH"\n' "$bin_dir" >&2
    fi
}

main() {
    if [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ]; then
        # Piped into bash, $0 is the shell, not this script, so the help is inline.
        cat <<EOF
Install the OmniNode onex command line on macOS or Linux:

    curl -fsSL https://omninode.ai/install.sh | bash

Installs $ONEX_PACKAGE $ONEX_VERSION with uv into your home directory (no sudo,
no git) and puts onex and omni-agent in uv's bin directory. Running it again is
safe; a newer installer upgrades in place.
EOF
        return 0
    fi
    [ -n "${HOME:-}" ] || fail "HOME is not set."
    need_cmd uname "It is part of every macOS and Linux system."
    local target
    target="$(detect_target)"
    need_cmd curl "Install curl with your system's package manager and run the installer again."
    need_cmd tar "Install tar with your system's package manager and run the installer again."
    need_cmd mktemp "Install coreutils with your system's package manager and run the installer again."
    need_cmd awk "Install awk with your system's package manager and run the installer again."
    if ! command -v sha256sum >/dev/null 2>&1 && ! command -v shasum >/dev/null 2>&1; then
        fail "this installer needs sha256sum or shasum to verify downloads." \
            "Install coreutils (Linux) or perl (macOS ships shasum) and run the installer again."
    fi

    local build_args="--no-build" openssl_dir=""
    if [ "$target" = "x86_64-apple-darwin" ]; then
        # The cryptography release the platform requires publishes no Intel
        # Mac wheel, so that one package is built from source with Homebrew's
        # OpenSSL and Rust; every other package still comes from a wheel.
        if ! command -v brew >/dev/null 2>&1 || ! brew --prefix openssl@3 >/dev/null 2>&1; then
            fail "on Intel Macs one dependency (cryptography) has no prebuilt package and must be built." \
                "Install the build tools first:" \
                "    brew install openssl@3 rust" \
                "then run this installer again. (Homebrew: https://brew.sh)"
        fi
        if ! cargo --version >/dev/null 2>&1; then
            fail "on Intel Macs the build needs a working Rust toolchain, and 'cargo --version' failed." \
                "Install it with 'brew install rust', or if you use rustup run 'rustup default stable'," \
                "then run this installer again."
        fi
        openssl_dir="$(brew --prefix openssl@3)"
        build_args=""
    fi

    say "Platform: $target"
    say "Installing $ONEX_PACKAGE $ONEX_VERSION (Python $ONEX_PYTHON) with uv"
    if [ "${ONEX_INSTALL_DRY_RUN:-0}" = "1" ]; then
        say "Dry run: stopping before any download."
        echo "target=$target"
        echo "package=$ONEX_PACKAGE==$ONEX_VERSION"
        echo "build=${build_args:-source-build-allowed}"
        return 0
    fi

    TMP_DIR="$(mktemp -d 2>/dev/null || mktemp -d -t omninode-onex)" ||
        fail "could not create a temporary directory." "Check that TMPDIR (or /tmp) is writable."
    trap cleanup EXIT

    local uv bin_dir
    uv="$(ensure_uv "$target")"
    bin_dir="$("$uv" tool dir --bin)"
    check_conflicts "$uv" "$bin_dir"

    set -- tool install --python "$ONEX_PYTHON"
    if [ -n "$build_args" ]; then
        set -- "$@" "$build_args"
    fi
    if [ -n "${ONEX_INSTALL_FIND_LINKS:-}" ]; then
        set -- "$@" --find-links "$ONEX_INSTALL_FIND_LINKS"
    fi
    set -- "$@" "$ONEX_PACKAGE==$ONEX_VERSION"
    if [ -n "$openssl_dir" ]; then
        OPENSSL_DIR="$openssl_dir" "$uv" "$@" ||
            fail "uv could not install $ONEX_PACKAGE $ONEX_VERSION." \
                "Check the messages above; a network error can be retried by running the installer again."
    elif ! "$uv" "$@"; then
        fail "uv could not install $ONEX_PACKAGE $ONEX_VERSION." \
            "Check the messages above; a network error can be retried by running the installer again."
    fi

    local launcher
    for launcher in $ONEX_LAUNCHERS; do
        [ -x "$bin_dir/$launcher" ] || fail "the install finished but $bin_dir/$launcher is missing."
    done
    local reported
    reported="$("$bin_dir/onex" --version 2>&1)" ||
        fail "onex was installed but 'onex --version' failed:" "$reported"
    say "Installed: $reported ($bin_dir/onex)"
    path_guidance "$bin_dir"
    say "Done. Run 'onex --help' to get started."
}

main "$@"
