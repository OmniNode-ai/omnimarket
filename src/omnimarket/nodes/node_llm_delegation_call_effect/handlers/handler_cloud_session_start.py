# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Cloud coding-session launch scripts owned by the delegation effect.

The caller executes the rendered script on the selected account host. Keeping
the complete interactive launch here gives the delegation boundary ownership
of harness arguments, terminal setup, startup dialogue and cleanup.
"""

from __future__ import annotations

CLOUD_START_WAIT_S = 180


def cloud_start_script(
    path_export: str,
    registry_root: str,
    owner: str,
    repo: str,
    landing_branch: str,
    run_id: str,
    prompt_path: str,
) -> str:
    """Start the session from a scratch local clone of the host's canonical clone, at its ``origin/<landing>`` sha
    with the GitHub remote, under the registry root (the folder the host's client already trusts; the canonical
    clone is not touched and nothing is fetched from GitHub). ``claude --cloud`` clones that remote at the current
    branch. Starting a new cloud session is interactive only (the CLI refuses ``--cloud`` with ``--print``), so
    the command runs on a pseudo-terminal until it names the session, and the local client then ends: the session
    runs on in the cloud. The scratch clone and the prompt copy are removed on every path."""
    work = f"{registry_root}/.onex_state/cloud-start/{run_id}"
    return "\n".join(
        [
            # Keep the native login selected by the caller's cloud probe.
            path_export,
            "unset ANTHROPIC_API_KEY CLAUDE_CODE_OAUTH_TOKEN",
            f'P="{prompt_path}"',
            'O="$P.out"',
            f'W="{work}"',
            "export TERM=xterm-256color",
            'trap \'rm -rf "$W" "$P" "$O" "$P.in"\' EXIT',
            f'S="{registry_root}/{repo}"; [ -d "$S/.git" ] || S="{registry_root}/../{repo}"',
            (
                f'sha=$(git -C "$S" rev-parse --verify "origin/{landing_branch}^{{commit}}") '
                '|| { echo "CLOUD-START-NO-CLONE"; exit 66; }'
            ),
            'git clone -q --no-checkout "$S" "$W" && cd "$W"',
            f'git checkout -q -B "{landing_branch}" "$sha" && git remote set-url origin "https://github.com/{owner}/{repo}.git"',
            'mkfifo "$P.in"',
            # A macOS lab host uses BSD script, which rejects the Linux flags and FIFO.
            # Python supplies its terminal there.
            f"""C='stty cols 1000 rows 50; claude --cloud "$(cat {prompt_path})"'""",
            (
                'if [ "$(uname -s)" = Darwin ]; then set -- python3 -c '
                '\'import pty,sys; sys.exit(pty.spawn(["sh","-c",sys.argv[1]]) >> 8)\' "$C"; '
                'else set -- script -qfec "$C" /dev/null; fi'
            ),
            '"$@" < "$P.in" > "$O" 2>&1 &',
            'pid=$!; exec 3>"$P.in"',
            # Answer only complete screens drawn since the last key; first-run notes also say trust.
            # Wait for the confirmation footer and the newest selection to reach Yes.
            "E=$(printf '\\033'); M=$(printf '\\342\\235\\257'); n=0; d=; themed=; went_on=; trusted=; downs=0; hold=0",
            'seen() { tail -c +$((n + 1)) "$O" | LC_ALL=C grep -aqE "$1"; }',
            'sent() { n=$(wc -c < "$O" | tr -d " "); }',
            'picked() { tail -c +$((d + 1)) "$O" | LC_ALL=C grep -aoE "${M}(${E}\\[[0-9;]*[A-Za-z]| |[0-9]\\.)*(Yes|No)" | tail -1; }',
            f"for _ in $(seq 1 {CLOUD_START_WAIT_S}); do",
            "  grep -aqE 'claude\\.ai/code/(session|cse)_[A-Za-z0-9]{8,}' \"$O\" && break",
            '  if { [ -z "$d" ] || [ -n "$trusted" ]; } && [ -z "$themed" ] && seen \'Syntax.{0,16}theme|Choose.{0,24}text.{0,16}style\'; then',
            "    sleep 1; printf '\\r' >&3; sent; themed=yes; echo CLOUD-START-FIRST-RUN-THEME",
            '  elif { [ -z "$d" ] || [ -n "$trusted" ]; } && [ -z "$went_on" ] && seen \'Enter.{0,16}to.{0,16}continue\'; then',
            "    sleep 1; printf '\\r' >&3; sent; went_on=yes; echo CLOUD-START-FIRST-RUN-CONTINUE",
            '  elif [ -z "$trusted" ] && [ "$PWD" = "$W" ] && { [ -n "$d" ] || { seen \'trust.{0,16}this.{0,16}folder|trust.{0,16}the.{0,16}files\' \\',
            "      && seen 'Enter.{0,16}to.{0,16}confirm' && d=$n; }; }; then",
            '    if [ "$hold" -gt 0 ]; then hold=$((hold - 1))',
            '    else case "$(picked)" in',
            "      *Yes) printf '\\r' >&3; sent; trusted=yes; echo CLOUD-START-TRUSTED-SCRATCH-CLONE ;;",
            "      *) if [ \"$downs\" -lt 3 ]; then printf '\\033[B' >&3; downs=$((downs + 1)); hold=2; echo CLOUD-START-TRUST-DOWN; fi ;;",
            "    esac; fi",
            "  fi",
            '  kill -0 "$pid" 2>/dev/null || break; sleep 1',
            "done",
            'sleep 3; kill "$pid" 2>/dev/null; wait "$pid" 2>/dev/null',
            "B=$(printf '\\007')",
            (
                'LC_ALL=C sed -e "s/${E}\\[[0-9;?]*[A-Za-z]//g" -e "s/${E}][^${B}]*${B}//g" "$O" '
                "| tr -d '\\r' | tail -c 6000"
            ),
            "grep -aqE 'claude\\.ai/code/(session|cse)_[A-Za-z0-9]{8,}' \"$O\"",
        ]
    )
