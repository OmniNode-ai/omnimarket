# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""From plugin test_host_capability_parity.py; tests of plugin scripts stay with those scripts."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from omnimarket.nodes.node_host_capability_compute.handlers import (
    handler_host_capability as cap,
)

pytestmark = pytest.mark.unit

PROBE_BOUND_PERL = r"""use POSIX ();
my ($s, @c) = @ARGV;
my $p = fork();
exit 127 unless defined $p;
if (!$p) { POSIX::setpgid(0, 0); open(STDIN, "<", "/dev/null"); exec { $c[0] } @c; exit 127; }
POSIX::setpgid($p, $p);
$SIG{ALRM} = sub { kill("TERM", -$p); select(undef, undef, undef, 0.3); kill("KILL", -$p); waitpid($p, 0); exit 124; };
alarm($s);
waitpid($p, 0);
alarm(0);
my $r = $?;
kill("TERM", -$p);
exit($r & 127 ? 128 + ($r & 127) : $r >> 8);"""

PROBE_BOUND_FN = r"""probe_bound() {
  pb_s=$1; shift
  if command -v perl >/dev/null 2>&1; then perl -e "$PB_PERL" -- "$pb_s" "$@"; return $?; fi
  "$@" </dev/null & pb_p=$!; pb_i=0; pb_tick=1
  while kill -0 "$pb_p" 2>/dev/null; do
    if [ "$pb_i" -ge "$pb_s" ]; then kill -s KILL "$pb_p" 2>/dev/null; wait "$pb_p" 2>/dev/null; return 124; fi
    sleep "$pb_tick"; pb_i=$((pb_i + pb_tick))
  done
  wait "$pb_p"
}"""

PROBE_BOUND_SH = (
    "PB_PERL='" + PROBE_BOUND_PERL + "'; export PB_PERL\n" + PROBE_BOUND_FN + "\n"
)


def test_unit_capabilities_follow_the_repository_hooks_and_the_dispatcher(
    tmp_path: Path,
) -> None:
    (tmp_path / "repo-a").mkdir()
    (tmp_path / "repo-a/.pre-commit-config.yaml").write_text(
        "      - id: shell-hygiene\n        name: Shell hygiene (shellcheck, OMN-14761)\n        entry: bash x.sh\n        language: system\n"
    )
    (tmp_path / "repo-b").mkdir()
    (tmp_path / "repo-b/.pre-commit-config.yaml").write_text(
        "  - repo: https://github.com/shellcheck-py/shellcheck-py\n    hooks:\n      - id: shellcheck\n        files: x\n"
        "      - id: other\n        language: system\n"
    )
    env = {"ONEX_REGISTRY_ROOT": str(tmp_path)}
    assert cap.unit_capabilities("repo-a", env) == {"shellcheck"}
    assert cap.unit_capabilities("repo-b", env) == frozenset()
    assert cap.unit_capabilities("absent", env) == frozenset()
    named = {**env, cap.NEEDS_ENV: "git-workflow-push, lab-ssh,not-a-capability"}
    assert cap.unit_capabilities(None, named) == {"git-workflow-push", "lab-ssh"}


class Host:
    """A scratch host for the capability block: HOME, a stub bin and a PATH of only the tools the block runs."""

    TOOLS = (
        "sh",
        "sed",
        "tr",
        "printf",
        "echo",
        "head",
        "tail",
        "find",
        "mkdir",
        "rmdir",
        "mv",
        "rm",
        "cat",
        "git",
        "perl",
        "sleep",
        "dirname",
    )

    def __init__(self, root: Path) -> None:
        self.home = root / "home"
        self.bin = root / "bin"
        self.stubs = self.home / ".local/bin"
        for d in (self.home, self.bin, self.stubs):
            d.mkdir(parents=True)
        for tool in self.TOOLS:
            found = shutil.which(tool)
            if (
                found and tool == "git"
            ):  # CI's portable git is a wrapper that finds its package from $0, so a symlink breaks it
                self.stub(tool, f'exec "{found}" "$@"\n', self.bin)
            elif found:
                (self.bin / tool).symlink_to(found)
        self.venv_python = root / "venv-python"
        self.answers(
            delegation="missing llm.openrouter.api_key-is-absent-from-the-secret-store",
            scm="ok subscribed:t",
        )

    def stub(self, name: str, body: str, where: Path | None = None) -> Path:
        path = (where or self.stubs) / name
        path.write_text("#!/bin/sh\n" + body)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return path

    def answers(self, *, delegation: str, scm: str) -> None:
        self.stub(
            "venv-python",
            f'case "$3" in delegation-key) echo "{delegation}";; scm-pr-create) echo "{scm}";; esac\n',
            self.venv_python.parent,
        )

    def credential(self, password: str) -> None:
        helper = (
            f"!f() {{ echo username=x-access-token; echo password={password}; }}; f"
        )
        (self.home / ".gitconfig").write_text(f'[credential]\n\thelper = "{helper}"\n')

    def run(
        self, peers: tuple[str, ...] | list[str] = (), *, refresh: bool = True
    ) -> dict[str, tuple[str, str]]:
        script = cap.capability_probe_sh(
            peers,
            bound=PROBE_BOUND_SH,
            path_export='export PATH="$HOME/.local/bin:$PATH"',
            registry_roots=[str(self.home)],
        )
        env = {
            "HOME": str(self.home),
            "PATH": str(self.bin),
            "GIT_CONFIG_NOSYSTEM": "1",
            cap.PYTHON_ENV: str(self.venv_python),
        }
        done = subprocess.run(
            [str(self.bin / "sh"), "-c", script],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
            check=False,
        )
        assert done.returncode == 0, done.stderr
        if refresh:  # the slow checks refresh detached; read again once each has written its verdict
            cache = self.home / ".cache/omni/capabilities"
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and (
                len(list(cache.glob("*.lock")))
                or len(
                    [
                        p
                        for p in cache.iterdir()
                        if p.is_file()
                        and not p.name.startswith(".")
                        and not p.name.endswith(".tmp")
                    ]
                )
                < 4
            ):
                time.sleep(0.1)
            return self.run(peers, refresh=False)
        lines = [
            line.split(None, 3)
            for line in done.stdout.splitlines()
            if line.startswith("PARITY-CAP ")
        ]
        assert [p[1] for p in lines] == [
            "scm-pr-create",
            "git-workflow-push",
            "delegation-key",
            "lab-ssh",
            "shellcheck",
        ], done.stdout
        return {p[1]: (p[2], p[3] if len(p) > 3 else "") for p in lines}


@pytest.fixture
def box(tmp_path: Path) -> Host:
    return Host(tmp_path)


def test_probe_reads_shellcheck_on_the_lane_path(box: Host) -> None:
    assert box.run()["shellcheck"] == ("missing", "shellcheck-is-not-on-the-lane-PATH")
    shellcheck = box.stub("shellcheck", "exit 0\n")
    assert box.run()["shellcheck"] == ("ok", str(shellcheck))


def test_probe_reads_the_delegation_key_and_pr_create_through_the_dispatch_venv(
    box: Host,
) -> None:
    read = box.run()
    assert read["delegation-key"] == (
        "missing",
        "llm.openrouter.api_key-is-absent-from-the-secret-store",
    )
    assert read["scm-pr-create"] == ("ok", "subscribed:t")


def test_probe_without_a_dispatch_venv_reads_unknown(box: Host) -> None:
    box.venv_python.unlink()
    read = box.run()
    assert read["delegation-key"] == ("unknown", "no-dispatch-venv")
    assert read["scm-pr-create"] == ("unknown", "no-dispatch-venv")


def test_probe_reads_the_workflow_scope_of_the_git_credential_and_never_puts_it_on_argv(
    box: Host,
) -> None:
    assert box.run()["git-workflow-push"] == ("unknown", "no-github-credential-for-git")
    seen = box.home / "curl-seen"
    box.stub(
        "curl",
        f'printf "%s\\n" "$*" > "{seen}"; cat >> "{seen}"\n'
        'printf "HTTP/2 200\\r\\nx-oauth-scopes: repo, read:org\\r\\n\\r\\n"\n',
    )
    box.credential("fixture-not-a-token")
    shutil.rmtree(box.home / ".cache")  # the first read is cached; this one reads again
    assert box.run()["git-workflow-push"] == ("missing", "scopes=repo,read:org")
    argv, *stdin = seen.read_text().splitlines()
    assert "fixture-not-a-token" not in argv
    assert "Authorization: token fixture-not-a-token" in stdin
    box.stub(
        "curl",
        'cat >/dev/null; printf "HTTP/2 200\\r\\nX-OAuth-Scopes: repo, workflow\\r\\n\\r\\n"\n',
    )
    shutil.rmtree(box.home / ".cache")
    assert box.run()["git-workflow-push"] == ("ok", "scopes=repo,workflow")
    box.stub("curl", 'cat >/dev/null; printf "HTTP/2 401\\r\\n\\r\\n"\n')
    shutil.rmtree(box.home / ".cache")
    assert box.run()["git-workflow-push"][0] == "missing"


def test_probe_reads_an_ssh_route_to_each_peer(box: Host) -> None:
    assert box.run()["lab-ssh"] == ("unknown", "no-peers-named")
    box.stub("ssh", 'case "$*" in *good-peer*) exit 0;; *) exit 255;; esac\n')
    assert box.run(["good-peer", "bad-peer", "not a host"])["lab-ssh"] == (
        "missing",
        "no-ssh-route-to:bad-peer",
    )
    assert box.run(["good-peer"])["lab-ssh"] == ("ok", "reached:good-peer")
    assert box.run(["good-peer", "user@good-peer-2"])["lab-ssh"] == (
        "ok",
        "reached:good-peer,user@good-peer-2",
    )


def test_probe_prints_unread_before_the_first_refresh_and_never_waits_on_it(
    box: Host,
) -> None:
    box.stub("ssh", "sleep 5\n")
    started = time.monotonic()
    read = box.run(["slow-peer"], refresh=False)
    assert time.monotonic() - started < 4
    assert read["lab-ssh"] == ("unknown", "not-read")
    assert read["shellcheck"][0] == "missing"


def test_probe_caches_the_slow_checks_and_reads_shellcheck_every_time(
    box: Host,
) -> None:
    box.run()
    box.answers(delegation="ok llm.openrouter.api_key-resolves", scm="missing bus:x")
    box.stub("shellcheck", "exit 0\n")
    read = box.run()
    assert read["delegation-key"][0] == "missing"
    assert read["scm-pr-create"][0] == "ok"
    assert read["shellcheck"][0] == "ok"
    shutil.rmtree(box.home / ".cache")
    read = box.run()
    assert read["delegation-key"][0] == "ok"
    assert read["scm-pr-create"] == (
        "missing",
        "bus:x",
    )


def test_the_capability_program_is_one_quoted_word_and_answers_one_verdict_line() -> (
    None
):
    assert "'" not in cap.CAPABILITY_PY
    for args in (
        ["delegation-key", cap.DELEGATION_SECRET_REF],
        ["scm-pr-create", cap.SCM_BUS_LANE, "1"],
        ["nothing"],
    ):
        done = subprocess.run(
            [sys.executable, "-I", "-c", cap.CAPABILITY_PY, *args],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
            env={
                k: v for k, v in os.environ.items() if k in ("PATH", "LD_LIBRARY_PATH")
            },
        )
        lines = done.stdout.splitlines()
        assert done.returncode == 0, (args, done.stdout, done.stderr)
        assert len(lines) == 1, (args, done.stdout, done.stderr)
        assert lines[0].split()[0] in cap.VERDICTS, (args, done.stdout, done.stderr)
