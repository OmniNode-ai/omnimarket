# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""The remote execution leg must carry the same pytest EXECUTION POLICY the
local leg runs under, and it must be liveness-bounded (OMN-17564).

THE DEFECT, measured live 2026-09-02T15:19Z before any of this was written.

1. PARALLELISM AND THE PER-TEST WATCHDOG WERE DROPPED AT THE SEAM.
   ``prepush_remote_argv`` emitted only test PATHS, and the remote wrapper ran
   ``"$UV" run pytest "${ARGV[@]}" --ignore=tests/integration --tb=short`` --
   no ``-n``, no ``--dist``, no ``--timeout``. Every dispatched heavy suite ran
   SINGLE-THREADED with no per-test watchdog, while the local leg and this
   repo's own CI both run ``--dist loadgroup --timeout=60
   --timeout-method=signal`` with a worker count (``ci.yml`` test-parallel:
   ``-n 2``; ``omnibase_core``'s local heavy leg: ``-n4``).

   Measured cost: 1.3 tests/s remote vs ~6.8 tests/s locally with ``-n4`` on
   the same tree (ledger TERMINAL 2026-09-01T23:56:00Z) -- a ~4-5x slowdown.
   Because every capacity row holds an EXCLUSIVE heavy-suite slot for the
   DURATION of the run, that slowdown multiplies straight into slot occupancy:
   at 15:19Z h101 held its only slot with ONE core busy of twelve, h201 sat at
   0.22x with ~25 idle cores, and five lanes queued behind them. The lab was
   slot-starved, not CPU-starved.

2. THE EXECUTION SSH HAD NO LIVENESS BOUND. It carried ``ConnectTimeout=6``
   and nothing else, while EVERY probe ssh in the same file is wrapped in
   ``timeout 10``/``timeout 12``. ``ConnectTimeout`` governs the handshake
   only, so a host that wedges AFTER connect holds the lane forever: pid 32398
   on the pushing host sat 5h29m against an h105 that had stopped answering
   TCP:22, with zero bytes returned and no completion marker written.

What is pinned here:

* The argv file shipped to the remote host carries the execution policy --
  ``--dist=loadgroup --timeout=60 --timeout-method=signal`` and an ``-n<N>``.
* ``N`` is resolved from the SELECTED ROW's ``cores`` column, capped, never a
  hardcoded 4: a 2-core row gets ``-n2``, not four workers on two cores.
* The policy flags never make an EMPTY selection look non-empty -- the
  "no paths" refusal is decided on paths alone.
* The execution ssh carries ``ServerAliveInterval``/``ServerAliveCountMax``
  and is wrapped in the same ``timeout`` guard the probe legs already use.
* Expiry is not a new code path: a leg that returns no MARKER is classified
  NO EVIDENCE and the ranked walk ADVANCES to the next fit host, exactly as it
  did for an unreachable host before this change.

The bash is extract-and-executed against fake ``ssh``/``scp``/``timeout``
binaries that record their argv, the pattern this hook's other shell tests
already use, so the assertions run THE code that ships.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOK = REPO_ROOT / "scripts" / "hooks" / "prepush_smart_tests.sh"
LIB = REPO_ROOT / "scripts" / "hooks" / "prepush_dispatch.sh"

pytestmark = pytest.mark.unit

#: The execution policy the remote leg must carry. Held here as literals so a
#: silent drop of any one of them turns this module red rather than costing
#: another 4-5x slot-occupancy multiplier nobody measures for a week.
POLICY_FLAGS = ("--dist=loadgroup", "--timeout=60", "--timeout-method=signal")

#: The cap. It is the worker count ``omnibase_core``'s LOCAL heavy leg already
#: runs (``PREPUSH_TIMEOUT_FLAGS="-n4 --dist=loadgroup --timeout=60
#: --timeout-method=signal"``), i.e. this change is PARITY with the local leg,
#: not a new parallelism policy. It also sits between this repo's own CI value
#: (``-n 2``, deliberately reduced from ``-n auto`` after repeated xdist OOM
#: crashes -- ``.github/workflows/ci.yml``) and unbounded ``-n auto``, so a
#: host the picker admitted at only the 4096 MiB free-memory floor is never
#: oversubscribed by this leg. Raising it is a separate, measurement-backed
#: change.
XDIST_CAP = 4

_TABLE = (
    "#label\trole\thostname\tssh_target\tcores\tuv_abs_path\tuv_min_version"
    "\tworkroot\tslot_mode\tslots\trepos_denied\tmode\theavy_local\tplacement_tier\tnote\n"
    "hbig\tcapacity\thostbig\thostbig.lan\t32\t/bin/uv\t0.1.0\t/tmp/wbig"
    "\tlockdir\t1\t-\tauthorizing\tallowed\tdefault\tthirty-two cores\n"
    "hsmall\tcapacity\thostsmall\thostsmall.lan\t2\t/bin/uv\t0.1.0\t/tmp/wsmall"
    "\tlockdir\t1\t-\tauthorizing\tallowed\tdefault\ttwo cores\n"
)

_FAKE_SSH = """#!/usr/bin/env bash
printf 'SSH %s\\n' "$*" >> "$PREPUSH_TEST_LOG"
exit 0
"""

# Records its argv and copies every LOCAL source file into the capture dir, so
# the test can read the exact argv.txt that would have been shipped.
_FAKE_SCP = """#!/usr/bin/env bash
printf 'SCP %s\\n' "$*" >> "$PREPUSH_TEST_LOG"
skip_next=0
for a in "$@"; do
  if [ "$skip_next" = "1" ]; then skip_next=0; continue; fi
  case "$a" in
    -o) skip_next=1; continue ;;
    -*) continue ;;
    *:*) continue ;;
  esac
  [ -f "$a" ] && cp "$a" "$PREPUSH_TEST_CAPTURE/" 2>/dev/null
done
exit 0
"""

# Records the budget it was handed, then runs the wrapped command, so wrapping
# is proven without making every test wait on a real timeout(1).
_FAKE_TIMEOUT = """#!/usr/bin/env bash
printf 'TIMEOUT %s\\n' "$*" >> "$PREPUSH_TEST_LOG"
shift
exec "$@"
"""


def _extract_function(source: str, name: str) -> str:
    lines = source.splitlines()
    start = next(
        (i for i, line in enumerate(lines) if line.startswith(f"{name}() {{")),
        None,
    )
    assert start is not None, f"{name}() not found"
    end = next((i for i in range(start + 1, len(lines)) if lines[i] == "}"), None)
    assert end is not None, f"unterminated {name}()"
    return "\n".join(lines[start : end + 1])


def _synth_repo(tmp_path: Path, name: str = "synth") -> Path:
    """A throwaway git repo whose HEAD carries the two-row table above.

    The library reads the host table from HEAD, never the working tree, so the
    repo has to be real and committed.
    """
    repo = tmp_path / name
    (repo / "scripts" / "hooks").mkdir(parents=True)
    (repo / "scripts" / "hooks" / "prepush_hosts.tsv").write_text(
        _TABLE, encoding="utf-8"
    )
    (repo / "README.md").write_text("synthetic tree\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", "."], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "table"],
        cwd=repo,
        check=True,
    )
    return repo


def _fake_bin(tmp_path: Path) -> Path:
    binbase = tmp_path / "fakebin"
    binbase.mkdir()
    for name, body in (
        ("ssh", _FAKE_SSH),
        ("scp", _FAKE_SCP),
        ("timeout", _FAKE_TIMEOUT),
    ):
        p = binbase / name
        p.write_text(body, encoding="utf-8")
        p.chmod(0o755)
    return binbase


def _run(
    repo_root: Path,
    body: str,
    env: dict[str, str] | None = None,
    extra_prelude: str = "",
) -> subprocess.CompletedProcess[str]:
    """Run BODY with the real library sourced and the hook's own callbacks stubbed."""
    bash = shutil.which("bash")
    assert bash is not None, "bash not available"
    script = f"""
set -uo pipefail
REPO_ROOT={repo_root}
PREPUSH_LOAD_THRESHOLD=1.0
PREPUSH_MIN_FREE_MEM_MB=4096
log() {{ printf '[t] %s\\n' "$1" >&2; }}
die() {{ printf 'DIE: %s\\n' "$1" >&2; exit 1; }}
host_load_ratio() {{ printf '2.40 12 0.20 40960\\n'; }}
# The real resolver, copied from the hook that sources this library. It picks
# the fake `timeout` this harness puts first on PATH.
_prepush_timeout_cmd() {{
  if command -v timeout > /dev/null 2>&1; then printf 'timeout'
  elif command -v gtimeout > /dev/null 2>&1; then printf 'gtimeout'
  fi
}}
. {LIB}
{extra_prelude}
{body}
"""
    return subprocess.run(
        [bash, "-c", script],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
        stdin=subprocess.DEVNULL,
        env={
            **os.environ,
            "PREPUSH_LOAD_OVERRIDE_MAP": "",
            "PREPUSH_SLOT_OVERRIDE_MAP": "",
            "PREPUSH_MEM_OVERRIDE_MAP": "",
            "PREPUSH_REACH_OVERRIDE_MAP": "",
            **(env or {}),
        },
    )


def _leg_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    binbase = _fake_bin(tmp_path)
    capture = tmp_path / "capture"
    capture.mkdir()
    log = tmp_path / "calls.log"
    log.write_text("", encoding="utf-8")
    base = {
        "PATH": f"{binbase}{os.pathsep}{os.environ.get('PATH', '')}",
        "PREPUSH_TEST_LOG": str(log),
        "PREPUSH_TEST_CAPTURE": str(capture),
        "PREPUSH_LOAD_OVERRIDE_MAP": "hbig=0.20,hsmall=0.20",
        "PREPUSH_MEM_OVERRIDE_MAP": "hbig=40000,hsmall=40000",
        "PREPUSH_UV_OVERRIDE_MAP": "hbig=9.9.9,hsmall=9.9.9",
        "PREPUSH_SLOT_OVERRIDE_MAP": "hbig=free,hsmall=free",
    }
    base.update(extra)
    return base


# =============================================================================
# 1. Worker count is resolved from the selected row, capped, never hardcoded
# =============================================================================


@pytest.mark.parametrize(
    ("cores", "expected"),
    [
        ("2", 2),  # a small row is NEVER oversubscribed to the cap
        ("4", 4),
        ("10", XDIST_CAP),
        ("12", XDIST_CAP),
        ("32", XDIST_CAP),
        ("", 1),  # unresolvable -> today's single-worker behavior, never the cap
        ("-", 1),
        ("many", 1),
        ("0", 1),
    ],
)
def test_worker_count_is_min_of_the_rows_cores_and_the_cap(
    tmp_path: Path, cores: str, expected: int
) -> None:
    """``-n`` follows the TARGET host, not the pushing host.

    A fixed ``-n4`` would under-use a 32-core row and oversubscribe a 2-core
    one; an unresolvable ``cores`` field degrades to one worker (exactly what
    shipped before this change) rather than guessing headroom -- the same
    fail-closed posture the load and memory probes already carry.
    """
    repo = _synth_repo(tmp_path)
    out = _run(
        repo,
        f'PREPUSH_PICK_CORES="{cores}"\nprepush_remote_xdist_workers',
    )
    assert out.returncode == 0, out.stdout + out.stderr
    assert out.stdout.strip() == str(expected), out.stdout + out.stderr


def test_the_flag_block_carries_the_whole_execution_policy(tmp_path: Path) -> None:
    repo = _synth_repo(tmp_path)
    out = _run(repo, 'PREPUSH_PICK_CORES="32"\nprepush_remote_pytest_flags')
    assert out.returncode == 0, out.stdout + out.stderr
    emitted = [line for line in out.stdout.splitlines() if line]
    for flag in POLICY_FLAGS:
        assert flag in emitted, f"{flag} missing from {emitted}"
    assert f"-n{XDIST_CAP}" in emitted, emitted


def test_the_picker_publishes_the_selected_rows_core_count(tmp_path: Path) -> None:
    """``PREPUSH_PICK_CORES`` is what the worker count is resolved from, so the
    picker has to publish it alongside every other ``PREPUSH_PICK_*`` field."""
    repo = _synth_repo(tmp_path)
    out = _run(
        repo,
        "pick_capacity_host somewhereelse synth authorizing\n"
        'echo "PICK=${PREPUSH_PICK_LABEL} CORES=${PREPUSH_PICK_CORES}"',
        env=_leg_env(tmp_path),
    )
    assert re.search(r"PICK=(hbig|hsmall)", out.stdout), out.stdout + out.stderr
    label = re.search(r"PICK=(\w+)", out.stdout).group(1)  # type: ignore[union-attr]
    expected = "32" if label == "hbig" else "2"
    assert f"CORES={expected}" in out.stdout, out.stdout + out.stderr


# =============================================================================
# 2. The argv FILE that is shipped carries the policy
# =============================================================================


def _dispatch_one_leg(
    tmp_path: Path, cores: str = "32", body_extra: str = ""
) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
    """Drive the real ``prepush_remote_run`` against fake ssh/scp/timeout.

    Nothing writes a MARKER, so the leg lands on the NO-EVIDENCE branch -- the
    branch an expired or wedged host also lands on.
    """
    repo = _synth_repo(tmp_path)
    env = _leg_env(tmp_path)
    out = _run(
        repo,
        "IS_FULL=True\n"
        'FULL_SUITE_TARGET="tests/"\n'
        "RUNNABLE_INTEGRATION_PATHS=()\n"
        "PATHS=()\n"
        'PREPUSH_PICK_LABEL="hbig"\n'
        'PREPUSH_PICK_HOSTNAME="hostbig"\n'
        'PREPUSH_PICK_SSH="hostbig.lan"\n'
        'PREPUSH_PICK_UV="/bin/uv"\n'
        'PREPUSH_PICK_WORKROOT="/tmp/wbig"\n'
        'PREPUSH_PICK_SLOTMODE="lockdir"\n'
        'PREPUSH_PICK_MODE="authorizing"\n'
        'PREPUSH_PICK_SLOT="1"\n'
        'PREPUSH_PICK_RATIO="0.20"\n'
        'PREPUSH_PROBE_LOG="hbig=fit"\n'
        f'PREPUSH_PICK_CORES="{cores}"\n'
        f"{body_extra}"
        'rc=0; prepush_remote_run "full-suite escalation" || rc=$?\n'
        'echo "RC=${rc}"',
        env=env,
    )
    return out, Path(env["PREPUSH_TEST_CAPTURE"]), Path(env["PREPUSH_TEST_LOG"])


def test_the_shipped_argv_file_carries_paths_then_the_execution_policy(
    tmp_path: Path,
) -> None:
    out, capture, _ = _dispatch_one_leg(tmp_path, cores="32")
    argv = (capture / "argv.txt").read_text(encoding="utf-8").splitlines()
    assert argv, out.stdout + out.stderr
    # The selection still leads, byte-identical to what it always was: the
    # policy is APPENDED, so it can never displace or reorder a test path.
    assert argv[0] == "tests/", argv
    for flag in POLICY_FLAGS:
        assert flag in argv, f"{flag} missing from shipped argv {argv}"
    assert f"-n{XDIST_CAP}" in argv, argv


def test_the_selection_paths_receipt_field_stays_paths_only(tmp_path: Path) -> None:
    """``prepush_remote_argv`` is the SELECTION, and the receipt records it under
    ``selection_paths``. Folding execution flags into it would make an audit of
    "what did that host actually run" read flags as coverage."""
    repo = _synth_repo(tmp_path)
    out = _run(
        repo,
        "IS_FULL=True\n"
        'FULL_SUITE_TARGET="tests/"\n'
        "RUNNABLE_INTEGRATION_PATHS=()\n"
        "PATHS=()\n"
        'PREPUSH_PICK_CORES="32"\n'
        "prepush_remote_argv",
    )
    assert out.stdout.splitlines() == ["tests/"], out.stdout + out.stderr


def test_the_policy_flags_cannot_make_an_empty_selection_look_runnable(
    tmp_path: Path,
) -> None:
    """The "nothing selected" refusal is decided on PATHS alone.

    If the flags were written before the emptiness check, a selection that
    resolved to zero paths would ship an argv file of pure flags -- pytest
    would then collect ``testpaths`` from the transplanted tree's own config,
    silently running something nobody selected.
    """
    out, capture, log = _dispatch_one_leg(
        tmp_path,
        cores="32",
        body_extra='IS_FULL=False\nPATHS=()\nFULL_SUITE_TARGET="tests/"\n',
    )
    assert "RC=1" in out.stdout, out.stdout + out.stderr
    assert not (capture / "argv.txt").exists(), "an empty selection was shipped"
    assert "SCP" not in log.read_text(encoding="utf-8")


# =============================================================================
# 3. The execution ssh is liveness-bounded
# =============================================================================


def test_the_execution_ssh_carries_keepalives_and_a_timeout_guard(
    tmp_path: Path,
) -> None:
    """``ConnectTimeout`` governs the handshake only.

    The leg that sat 5h29m on a wedged h105 had connected successfully; what it
    lacked was any bound on SILENCE after that. Keepalives bound the transport
    and the ``timeout`` wrapper bounds the whole run, mirroring the probe legs
    in this same file which are all ``timeout``-wrapped already.
    """
    out, _, log = _dispatch_one_leg(tmp_path)
    calls = log.read_text(encoding="utf-8").splitlines()
    exec_calls = [
        c for c in calls if "prepush_smart_tests.sh" in c and c.startswith("SSH")
    ]
    assert exec_calls, f"no execution ssh recorded: {calls}\n{out.stderr}"
    execline = exec_calls[0]
    assert "ServerAliveInterval=" in execline, execline
    assert "ServerAliveCountMax=" in execline, execline

    wrapped = [c for c in calls if c.startswith("TIMEOUT") and "ssh" in c]
    assert wrapped, f"the execution ssh was not wrapped in the timeout guard: {calls}"
    budget = wrapped[0].split()[1]
    # Split rather than `and`-ed: this repo's ruff enforces PT018, and a
    # compound assertion here would not say WHICH half failed.
    assert budget.isdigit(), wrapped[0]
    assert int(budget) > 0, wrapped[0]


def test_the_execution_ssh_runs_unwrapped_when_no_timeout_binary_exists(
    tmp_path: Path,
) -> None:
    """``timeout(1)`` ships on neither Mac in the lab by default.

    Its absence must degrade to the keepalive bound alone -- which still closes
    the wedged-host case -- rather than refusing to dispatch at all.
    """
    repo = _synth_repo(tmp_path)
    env = _leg_env(tmp_path)
    out = _run(
        repo,
        "IS_FULL=True\n"
        'FULL_SUITE_TARGET="tests/"\n'
        "RUNNABLE_INTEGRATION_PATHS=()\n"
        "PATHS=()\n"
        'PREPUSH_PICK_LABEL="hbig"\n'
        'PREPUSH_PICK_HOSTNAME="hostbig"\n'
        'PREPUSH_PICK_SSH="hostbig.lan"\n'
        'PREPUSH_PICK_UV="/bin/uv"\n'
        'PREPUSH_PICK_WORKROOT="/tmp/wbig"\n'
        'PREPUSH_PICK_SLOTMODE="lockdir"\n'
        'PREPUSH_PICK_MODE="authorizing"\n'
        'PREPUSH_PICK_SLOT="1"\n'
        'PREPUSH_PICK_RATIO="0.20"\n'
        'PREPUSH_PROBE_LOG="hbig=fit"\n'
        'PREPUSH_PICK_CORES="32"\n'
        'rc=0; prepush_remote_run "full-suite escalation" || rc=$?\n'
        'echo "RC=${rc}"',
        env=env,
        extra_prelude="_prepush_timeout_cmd() { printf ''; }\n",
    )
    calls = Path(env["PREPUSH_TEST_LOG"]).read_text(encoding="utf-8").splitlines()
    exec_calls = [
        c for c in calls if "prepush_smart_tests.sh" in c and c.startswith("SSH")
    ]
    assert exec_calls, f"no execution ssh recorded: {calls}\n{out.stderr}"
    assert "ServerAliveInterval=" in exec_calls[0], exec_calls[0]
    assert not [c for c in calls if c.startswith("TIMEOUT")], calls


# =============================================================================
# 4. Expiry falls through to the EXISTING no-evidence classification
# =============================================================================


def test_a_leg_that_writes_no_marker_is_no_evidence_not_a_verdict(
    tmp_path: Path,
) -> None:
    out, _, _ = _dispatch_one_leg(tmp_path)
    assert "RC=1" in out.stdout, out.stdout + out.stderr
    assert "NO completion marker" in out.stderr, out.stderr
    assert "not a pass, not a failure" in out.stderr, out.stderr


def test_the_ranked_walk_advances_past_a_leg_that_produced_no_marker(
    tmp_path: Path,
) -> None:
    """Timing a host out must cost the NEXT host, not the whole escalation.

    This is the property that makes the ``timeout`` wrapper safe to add: it
    creates no new classification. An expired leg returns exactly what an
    unreachable-on-arrival leg has always returned, and ``dispatch_to_lab_host``
    already treats that as a PLACEMENT miss.
    """
    repo = _synth_repo(tmp_path)
    env = _leg_env(tmp_path)
    out = _run(
        repo,
        "IS_FULL=True\n"
        'FULL_SUITE_TARGET="tests/"\n'
        "RUNNABLE_INTEGRATION_PATHS=()\n"
        "PATHS=()\n"
        'PREPUSH_LC_HOST="somewhereelse"\n'
        'rc=0; dispatch_to_lab_host "full-suite escalation" || rc=$?\n'
        'echo "RC=${rc}"',
        env=env,
        extra_prelude=_extract_function(
            HOOK.read_text(encoding="utf-8"), "dispatch_to_lab_host"
        ),
    )
    assert "RC=1" in out.stdout, out.stdout + out.stderr
    # BOTH fit candidates were tried -- the walk did not stop at the first
    # silent host, and it did not forge a verdict from either.
    assert "hbig" in out.stderr, out.stderr
    assert "hsmall" in out.stderr, out.stderr
    assert "no fit lab host produced a verdict" in out.stderr, out.stderr
    assert "REMOTE LAB RUN PASS" not in out.stderr, out.stderr


# =============================================================================
# 5. The policy must be HONOURABLE on the transplanted tree
# =============================================================================


def test_the_shipped_flags_are_backed_by_declared_test_dependencies() -> None:
    """A flag the remote environment cannot honour is a FALSE RED, and a false
    red on this leg HARD-BLOCKS the push.

    The remote wrapper materializes the tree with ``uv sync --all-extras``,
    which installs this project's default dependency groups. ``-n``/``--dist``
    come from ``pytest-xdist`` and ``--timeout``/``--timeout-method`` from
    ``pytest-timeout``; if either is dropped from the declared groups, pytest
    exits on "unrecognized arguments" on EVERY dispatched suite and every
    escalation becomes a refusal. Pin the coupling rather than discover it on a
    host at 02:00.
    """
    import tomllib

    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dev = data["dependency-groups"]["dev"]
    names = {re.split(r"[<>=!~\[ ]", spec, maxsplit=1)[0] for spec in dev}
    assert "pytest-xdist" in names, "-n/--dist are shipped to the remote leg"
    assert "pytest-timeout" in names, "--timeout/--timeout-method are shipped"


def test_the_remote_pytest_invocation_is_otherwise_unchanged() -> None:
    """The policy is ADDITIVE. The remote wrapper still ignores
    ``tests/integration`` (the leg deliberately excludes service-dependent
    suites) and still shortens tracebacks; nothing about the invocation moved
    except the argv it is handed."""
    lib = LIB.read_text(encoding="utf-8")
    assert (
        '"$UV" run pytest "${ARGV[@]}" --ignore=tests/integration --tb=short' in lib
    ), "the remote pytest invocation changed shape"


def test_the_policy_is_constants_not_env_indirection() -> None:
    """``PREPUSH_*`` env overrides are forbidden by the hook (OMN-16480), and
    the off-box wait budget is already a bare constant for the same reason: an
    env indirection here would let ``PREPUSH_REMOTE_XDIST_WORKER_CAP=1`` restore
    the single-threaded defect in one word, silently."""
    lib = LIB.read_text(encoding="utf-8")
    for const in (
        "PREPUSH_REMOTE_POLICY_FLAGS",
        "PREPUSH_REMOTE_XDIST_WORKER_CAP",
        "PREPUSH_REMOTE_SSH_ALIVE_INTERVAL_SECONDS",
        "PREPUSH_REMOTE_SSH_ALIVE_COUNT_MAX",
        "PREPUSH_REMOTE_EXEC_TIMEOUT_SECONDS",
    ):
        assert re.search(rf"^{const}=[^$]", lib, re.MULTILINE), (
            f"{const} must be a bare constant, not a ${{VAR:-...}} indirection"
        )
