# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Guards for lab-wide pre-push distribution (OMN-16991).

Three things are pinned here, because all three were structural defects rather
than bugs in a computation:

1. **The host table is the identity authority, read from the COMMITTED tree.**
   The guard used to test two hard-coded hostnames -- a literal ``||`` that was
   the entire reason ``.101``/``.105`` could not be used. The full table
   contents are asserted, so adding or promoting a host requires a reviewed
   commit *and* a deliberate edit here.

2. **Placement reads SLOT state before load.** Measured 2026-08-30: ``.201``
   showed the fittest load ratio in the lab (14.08/32 = 0.44x) while running
   three concurrent pre-push suites behind a 10-deep queue. A load-only picker
   routes a fourth run onto the most jammed host in the fleet.

3. **Nothing here may make the gate accept less work.** The precedence tests
   pin the GitHub-hosted sha-pinned run ahead of the lab leg, and pin that a
   remote RED refuses instead of falling through to the override grant.

The bash helpers are extract-and-executed (the pattern already used for this
hook's other pure shell functions) so the assertions run THE code that ships,
never a Python re-implementation that could pass while the shipped picker is
broken.
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
TABLE = REPO_ROOT / "scripts" / "hooks" / "prepush_hosts.tsv"

pytestmark = pytest.mark.unit


# =============================================================================
# The table itself
# =============================================================================


def _rows() -> list[list[str]]:
    rows = []
    for line in TABLE.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0]
        if not line.strip():
            continue
        rows.append(line.split("\t"))
    return rows


def test_table_exists_and_every_row_has_the_full_column_set() -> None:
    assert TABLE.is_file(), f"expected the host table at {TABLE}"
    rows = _rows()
    assert rows, "expected at least one data row"
    for row in rows:
        assert len(row) == 15, (
            f"row {row[0] if row else row!r} has {len(row)} columns, expected 15 "
            "(label role hostname ssh_target cores uv_abs_path uv_min_version "
            "workroot slot_mode slots repos_denied mode heavy_local "
            "placement_tier note)"
        )


def test_table_contents_are_pinned() -> None:
    """The exact designated set, asserted.

    This is the point of the file: the table decides which machines may
    authorize a heavy gate run, so a row addition or a `mode` promotion must be
    a reviewed, deliberate change and not a quiet edit.
    """
    got = {r[0]: (r[1], r[2], r[11]) for r in _rows()}
    assert got == {
        "h200": ("capacity", "stickybeatz-studio", "authorizing"),
        "h201": ("capacity", "omninode-pc", "authorizing"),
        "h201c": ("identity", "gate-runner-201", "authorizing"),
        "h101": ("capacity", "stickybeatz", "authorizing"),
        "h105": ("capacity", "omnibook", "authorizing"),
    }


def test_the_shipped_heavy_local_policy_is_pinned() -> None:
    """The interactive hosts route their own heavy work off-box; the pure lab
    hosts keep the pre-OMN-17392 behavior.

    `h200` (OMN-17392, operator-directed) and the two `.201` identities
    (OMN-17485: `h201` the host, `h201c` the gate-runner container -- the dev
    runtime lane's evidence surface and an interactive collaborator workspace)
    are `prefer_remote`; `h101`/`h105` stay `allowed`. Every prefer_remote row
    is still `authorizing` -- the policy governs where a row's OWN escalations
    go, never whether the row may satisfy anyone else's.
    """
    policy = {r[0]: r[12] for r in _rows()}
    assert policy == {
        "h200": "prefer_remote",
        "h201": "prefer_remote",
        "h201c": "prefer_remote",
        "h101": "allowed",
        "h105": "allowed",
    }
    modes = {r[0]: r[11] for r in _rows()}
    for label in ("h200", "h201", "h201c"):
        assert modes[label] == "authorizing", (
            f"prefer_remote must not be a back-door de-designation: {label} still "
            "has to be able to authorize an escalation for every OTHER host"
        )


def test_prefer_remote_rows_stay_a_strict_subset_of_capacity_rows() -> None:
    """A structural invariant, not a restatement of the pin above: whatever the
    table grows to, the set of rows that route work away from themselves must
    stay a strict subset of the capacity rows, or an escalation has nowhere to
    go and every push falls back to the box it was routed off.

    Since OMN-17485 the subset is two (h200, h201) rather than one; the
    remainder must still contain at least one `allowed`, default-tier capacity
    row, or the "somewhere else" every prefer_remote host routes to would be
    nothing but demoted or self-deflecting hosts."""
    capacity = [r for r in _rows() if r[1] == "capacity"]
    prefer_remote = [r for r in capacity if r[12] == "prefer_remote"]
    assert prefer_remote, "expected at least one prefer_remote row (h200)"
    assert len(prefer_remote) < len(capacity), (
        "every capacity row is prefer_remote -- there is no host left to route "
        "an escalation TO, so the bounded off-box wait can only ever time out"
    )
    allowed_default = [
        r for r in capacity if r[12] == "allowed" and r[13] != "last_resort"
    ]
    assert allowed_default, (
        "no allowed, default-tier capacity row remains -- heavy escalations "
        "have no first-choice destination anywhere in the lab"
    )


def test_placement_tier_is_pinned() -> None:
    """`.201` (h201) is the ONLY `last_resort` row: it hosts the dev runtime
    lane -- the live evidence surface the OMN-16963 AC5 terminalization
    measurement reads from -- and the interactive collaborator lane, so the
    placement engine may take it only when no default-tier host is fit
    (OMN-17485). Promotion back to `default` is this reviewed two-step, not a
    quiet edit. `h201c` is identity-only and never a placement target."""
    tiers = {r[0]: r[13] for r in _rows()}
    assert tiers == {
        "h200": "default",
        "h201": "last_resort",
        "h201c": "-",
        "h101": "default",
        "h105": "default",
    }


def test_201_host_is_designated_by_its_real_hostname() -> None:
    """`.201`'s real `hostname -s` is `omninode-pc`; `gate-runner-201` is only
    the CONTAINER's. Before OMN-16991 only the container name was designated,
    so every push on the host itself needed an env override that the pytest
    child's env scrub then stripped."""
    hosts = {r[0]: r[2] for r in _rows()}
    assert hosts["h201"] == "omninode-pc"
    assert hosts["h201c"] == "gate-runner-201"


def test_the_shipped_repo_denials_are_pinned() -> None:
    """`h101` and `h105` DENY this repo; `h200`/`h201`/`h201c` deny nothing.

    Denial is per-repo capacity policy, so both directions are a reviewed table
    edit plus a deliberate edit here -- the same two-step that guards a mode
    promotion.

    WHY THE TWO MACS ARE DENIED, measured not precautionary: a real dispatch of
    this repo's full suite to `h101` on 2026-09-01 ran 17,883 tests in 13m58s
    and returned 12 HOST-COUPLED failures -- all 12 pass locally -- from ONE
    root cause. `$OMNI_HOME` does not cross the ssh boundary, and two call sites
    silently default to `Path.home()/"Code"/"omni_home"`: EVIDENCE_BASE_DIR in
    handler_post_merge_sync.py, and the env `_smoke_aislop_sweep` passes in
    market_skill_baseline.py. That path EXISTS on the lab Macs and is TCC-denied
    to sshd, so 11 knowledge-sync tests raise PermissionError on `mkdir` and
    test_market_skill_smokes gets non-JSON from a sweep that could not read a
    workspace.

    `prepush_dispatch.sh` treats a remote red as a genuine red and HARD-BLOCKS
    the push -- correctly, since a red must never fall through to an override
    grant. So an undenied row here would make this gate strictly WORSE than the
    two-hostname literal it replaces: a guaranteed false red is worse than an
    honest refusal. The capacity itself is proven (see the row notes); only this
    repo's workspace coupling is not.

    WHY `repos_denied` AND NOT `mode=disabled`: the denial is about THIS REPO,
    not about the host's standing. `disabled` would also strip the row's
    identity -- a valid identity -- and would stop it authorizing anything else.
    This is the same column, and the same lift-when-proven path, OMN-16989 used
    for `h201` in omnibase_infra.

    `h201` denies nothing: OMN-16989's 15 "host-coupled" failures were all
    measured in the `.201` gate-runner CONTAINER, a different environment from
    the `.201` HOST this table routes to, and the denial was lifted after a
    green run over the real remote leg.
    """
    denied = {r[0]: r[10] for r in _rows()}
    assert denied == {
        "h200": "-",
        "h201": "-",
        "h201c": "-",
        "h101": "omnimarket",
        "h105": "omnimarket",
    }


def test_a_denied_row_is_skipped_for_this_repo_but_kept_for_others(
    table_repo: Path,
) -> None:
    """The denial has to bite on the REPO ARGUMENT, not on the row itself.

    Two assertions in opposite directions, because a denial that skipped the row
    unconditionally would be `disabled` wearing another column's name, and a
    denial that never skipped anything would be a comment.
    """
    fit = "h200=2.09,h201=2.09,h101=0.20,h105=0.10"
    mem = "h200=65536,h201=65536,h101=65536,h105=65536"
    denied_out = _pick(
        table_repo,
        load=fit,
        slot=_ALL_FREE,
        uv=_GOOD_UV,
        mem=mem,
        repo_name="omnimarket",
    )
    assert "h101=repo-denied" in denied_out, denied_out
    assert "h105=repo-denied" in denied_out, denied_out
    assert "PICK=h101" not in denied_out, denied_out
    assert "PICK=h105" not in denied_out, denied_out

    other_out = _pick(
        table_repo,
        load=fit,
        slot=_ALL_FREE,
        uv=_GOOD_UV,
        mem=mem,
        repo_name="some-other-repo",
    )
    assert "PICK=h105" in other_out, other_out


def test_the_two_denied_macs_keep_their_proven_capacity_facts() -> None:
    """Denying a host must not delete the evidence that the host WORKS.

    A row turned off with no record of why is indistinguishable from a row that
    was never proven, and the next person re-runs every probe from scratch. The
    note has to carry BOTH halves: the measured capacity and the measured
    blocker. (Why they are denied at all is pinned in
    test_the_shipped_repo_denials_are_pinned.)
    """
    notes = {r[0]: r[14] for r in _rows()}
    modes = {r[0]: r[11] for r in _rows()}
    for label in ("h101", "h105"):
        assert modes[label] == "authorizing", (
            f"{label} must keep its identity: the denial is per-repo capacity "
            "policy, not a de-designation"
        )
        assert "PROVEN FOR OMNIMARKET" in notes[label], (
            f"{label} lost the record of the capacity proof when it was denied"
        )
        assert "17,747" in notes[label], (
            f"{label}'s note no longer carries the measured collection count"
        )
        assert "REPOS_DENIED=omnimarket" in notes[label], (
            f"{label} is denied with no stated reason in its note"
        )
        assert "TCC-denied" in notes[label], (
            f"{label}'s note does not name the measured root cause"
        )


def test_h101_hostname_is_what_hostname_s_actually_prints() -> None:
    """`ssh` to h101 and `hostname -s` prints `Stickybeatz`, not
    `stickybeatz.local`. The old value could never have matched an identity
    check, so the row would have failed silently the moment it was promoted."""
    hosts = {r[0]: r[2] for r in _rows()}
    assert hosts["h101"] == "stickybeatz"
    assert "." not in hosts["h101"], (
        "the column holds `hostname -s` output, which is never dotted"
    )


def test_every_capacity_row_carries_an_absolute_uv_path_and_a_floor() -> None:
    """uv is on no host's non-interactive PATH, and the live fleet spread is
    0.8.3 -> 0.11.32 against a lockfile at revision 3. Presence is not enough;
    the version floor is what makes a stale host skip rather than fail weirdly
    mid-`uv sync`."""
    for row in _rows():
        if row[1] != "capacity":
            continue
        assert row[5].startswith("/"), (
            f"{row[0]}: uv path must be absolute, got {row[5]!r}"
        )
        assert row[6][0].isdigit(), (
            f"{row[0]}: expected a uv_min_version, got {row[6]!r}"
        )


def test_101_workroot_avoids_the_tcc_protected_tree() -> None:
    """`ssh jonah@.101 'ls ~/Code'` returns `Operation not permitted`, so the
    workroot must live outside it -- the bundle design never needs `~/Code` on
    a remote host, which is what removes the out-of-band GUI grant step."""
    workroots = {r[0]: r[7] for r in _rows()}
    # Both literals carry TWO annotations, in this order, because two
    # independent gates scan the line and their grammars are incompatible:
    #   * scripts/validation/check_leaked_literals.sh matches
    #     `# onex-allow-local-path OMN-[0-9]+ reason="..."` anchored at the `#`,
    #     so its marker must come FIRST;
    #   * tests/unit/structure/test_no_hardcoded_literals.py matches
    #     `#\s*(onex-allow-internal-ip|test-literal-ok)` -- it does not accept
    #     `onex-allow-local-path` at all -- so it needs its own trailing `#`.
    # Each literal is bound to a NAME on its own line so `ruff format` cannot
    # move it away from the annotation that exempts it. That is not
    # hypothetical: the first version of this test annotated an expression that
    # the formatter then split, which passed the leak gate locally and went red
    # only on the lab host, in the full suite.
    tcc_denied_prefix = "/Users/jonah/Code"  # onex-allow-local-path OMN-17435 reason="the TCC-denied prefix this test asserts the workroot is NOT under; the literal is the thing being excluded, never a default"  # test-literal-ok: same, for the structural gate
    shared_workroot = "/Users/Shared/onex-prepush"  # onex-allow-local-path OMN-17435 reason="the shared, TCC-free remote workroot the committed host table pins for this row"  # test-literal-ok: same, for the structural gate
    assert not workroots["h101"].startswith(tcc_denied_prefix)
    assert workroots["h101"] == shared_workroot


# =============================================================================
# Extract-and-execute harness
# =============================================================================


def _run_driver(repo_root: Path, body: str) -> subprocess.CompletedProcess[str]:
    """Run BODY with the real library sourced and the hook's own dependencies
    stubbed, against a throwaway git repo whose HEAD carries the real table.

    ``stdin`` is /dev/null on purpose. The row-scan defect these tests pin is
    "a probe ate the loop's stdin", and the tests that reproduce it stub a probe
    that DRAINS stdin; inheriting this pytest process's stdin would make such a
    stub block forever instead of returning at EOF.
    """
    script = f"""
set -uo pipefail
REPO_ROOT={repo_root}
PREPUSH_LOAD_THRESHOLD=1.0
log() {{ printf '[t] %s\\n' "$1" >&2; }}
die() {{ printf 'DIE: %s\\n' "$1" >&2; exit 1; }}
_prepush_timeout_cmd() {{ printf ''; }}
host_load_ratio() {{ return 1; }}
. {LIB}
{body}
"""
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        stdin=subprocess.DEVNULL,
        env={
            **os.environ,
            "PREPUSH_LOAD_OVERRIDE_MAP": "",
            "PREPUSH_SLOT_OVERRIDE_MAP": "",
            "PREPUSH_MEM_OVERRIDE_MAP": "",
        },
    )


def _driver(repo_root: Path, body: str) -> str:
    return _run_driver(repo_root, body).stdout


def _driver_both(repo_root: Path, body: str) -> str:
    completed = _run_driver(repo_root, body)
    return completed.stdout + completed.stderr


#: A table whose rows exist only to exercise the RULES, independent of whichever
#: machines the lab happens to hold today. Two authorizing rows plus a shadow
#: row is the exact shape the placement bug needed: the shadow host is the
#: idlest, so a load-only picker chooses it and then throws its verdict away.
_SYNTHETIC_TABLE = (
    "#label\trole\thostname\tssh_target\tcores\tuv_abs_path\tuv_min_version"
    "\tworkroot\tslot_mode\tslots\trepos_denied\tmode\theavy_local"
    "\tplacement_tier\tnote\n"
    "ha\tcapacity\thosta\tjonah@hosta\t24\t/bin/uv\t0.1.0\t/tmp/wa\tlockdir\t1\t-\tauthorizing\tallowed\tdefault\tbusier\n"
    "hb\tcapacity\thostb\tjonah@hostb\t24\t/bin/uv\t0.1.0\t/tmp/wb\tlockdir\t1\t-\tauthorizing\tallowed\tdefault\tidler\n"
    "hs\tcapacity\thosts\tjonah@hosts\t24\t/bin/uv\t0.1.0\t/tmp/ws\tlockdir\t1\t-\tshadow\tallowed\tdefault\tidlest of all\n"
)

#: A single disabled row, so the shipped table's promotion of h101 (its last
#: disabled row, OMN-17161) does not strand the "a disabled host is never
#: probed" rule without a fixture to exercise it.
_SYNTHETIC_TABLE_DISABLED_ONLY = (
    "#label\trole\thostname\tssh_target\tcores\tuv_abs_path\tuv_min_version"
    "\tworkroot\tslot_mode\tslots\trepos_denied\tmode\theavy_local"
    "\tplacement_tier\tnote\n"
    "hd\tcapacity\thostd\tjonah@hostd\t24\t/bin/uv\t0.1.0\t/tmp/wd\tlockdir\t1\t-\tdisabled\tallowed\tdefault\tstill unfit\n"
)


def _repo_with_table(tmp_path: Path, table_text: str, name: str = "synth") -> Path:
    """A throwaway git repo whose HEAD carries TABLE_TEXT as the host table."""
    repo = tmp_path / name
    (repo / "scripts" / "hooks").mkdir(parents=True)
    (repo / "scripts" / "hooks" / "prepush_hosts.tsv").write_text(
        table_text, encoding="utf-8"
    )
    subprocess.run(["git", "init", "-q", "."], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "table"],
        cwd=repo,
        check=True,
    )
    return repo


@pytest.fixture
def table_repo(tmp_path: Path) -> Path:
    """A throwaway repo whose HEAD carries the real table, so the tests
    exercise the real `git show HEAD:` read path rather than a stub."""
    repo = tmp_path / "repo"
    (repo / "scripts" / "hooks").mkdir(parents=True)
    (repo / "scripts" / "hooks" / "prepush_hosts.tsv").write_text(
        TABLE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    subprocess.run(["git", "init", "-q", "."], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "table"],
        cwd=repo,
        check=True,
    )
    return repo


# =============================================================================
# Identity
# =============================================================================


def test_identity_accepts_the_real_201_hostname(table_repo: Path) -> None:
    out = _driver(table_repo, "prepush_identity_label omninode-pc || echo NONE")
    assert out.strip() == "h201"


def test_identity_accepts_the_201_container_hostname(table_repo: Path) -> None:
    out = _driver(table_repo, "prepush_identity_label gate-runner-201 || echo NONE")
    assert out.strip() == "h201c"


def test_a_shadow_host_is_not_a_designated_identity(tmp_path: Path) -> None:
    """A shadow host is a placement target whose verdict may not satisfy the
    escalation, so it must not confer identity either -- otherwise the identity
    guard would start PASSING on a host still in shadow, inverting the guard.

    Driven off a synthetic table because the shipped one no longer carries a
    shadow row (h105 was promoted); the RULE still has to hold for the next row
    that starts in shadow."""
    repo = _repo_with_table(tmp_path, _SYNTHETIC_TABLE)
    out = _driver(repo, "prepush_identity_label hosts || echo NONE")
    assert out.strip() == "NONE"


def test_a_disabled_host_is_not_a_designated_identity(table_repo: Path) -> None:
    out = _driver(table_repo, "prepush_identity_label stickybeatz.local || echo NONE")
    assert out.strip() == "NONE"


def test_an_override_replaces_its_row_rather_than_adding_a_name(
    table_repo: Path,
) -> None:
    """OMN-15059's guard is proven by forcing a nonsense `PREPUSH_200_HOSTNAME`
    and asserting refusal. That only holds while the override REPLACES the .200
    row: an override that merely appended a name could no longer de-designate
    this machine, silently inverting the guard."""
    out = _driver(
        table_repo,
        "PREPUSH_200_HOSTNAME=nope prepush_identity_label stickybeatz-studio || echo NONE",
    )
    assert out.strip() == "NONE"


def test_the_per_row_override_can_de_designate_any_row(table_repo: Path) -> None:
    out = _driver(
        table_repo,
        "PREPUSH_HOST_OVERRIDE_H201=nope prepush_identity_label omninode-pc || echo NONE",
    )
    assert out.strip() == "NONE"


def test_an_uncommitted_table_edit_cannot_designate_a_host(table_repo: Path) -> None:
    """The table is read from HEAD and the working copy must agree. Otherwise a
    one-line uncommitted edit naming your laptop would self-authorize a heavy
    gate run with no review and no receipt -- the forgeable-artifact surface
    OMN-16688 deliberately avoided."""
    tsv = table_repo / "scripts" / "hooks" / "prepush_hosts.tsv"
    tsv.write_text(
        tsv.read_text(encoding="utf-8")
        + "hevil\tcapacity\tmy-laptop\t-\t8\t/bin/uv\t0.1.0\t/tmp/w\tlockdir\t1\t-\tauthorizing\tallowed\tdefault\tforged\n",
        encoding="utf-8",
    )
    out = _driver(table_repo, "prepush_identity_label my-laptop || echo NONE")
    assert out.strip() == "NONE"


# =============================================================================
# The picker
# =============================================================================

_ALL_FREE = "h200=free,h201=free,h101=free,h105=free"


def _hook_func(name: str) -> str:
    """The named function's REAL source, lifted out of the hook.

    The driver deliberately stubs `host_load_ratio` (it must not ssh anywhere),
    so a test that wants to exercise the shipped fitness logic has to
    re-materialize it over that stub. Extracting it beats copying it: a copy
    would keep passing after the hook's own version changed.
    """
    text = HOOK.read_text(encoding="utf-8")
    start = text.index(f"\n{name}() {{\n")
    end = text.index("\n}\n", start)
    return text[start + 1 : end + 3]


def _with_real_load() -> str:
    """Prelude that restores the hook's real load/fitness implementation (and
    the memory floor it reads) on top of the driver's network-free stub.

    Built lazily rather than at import: a module-level build turns any missing
    piece into a collection ERROR that takes the whole file down with one
    unreadable traceback, instead of failing the handful of tests that actually
    depend on it.
    """
    text = HOOK.read_text(encoding="utf-8")
    floor = re.search(r"^PREPUSH_MIN_FREE_MEM_MB=\d+$", text, re.M)
    assert floor is not None, "the hook no longer declares a memory floor constant"
    return (
        "reap_spin_loop_orphans() { return 0; }\n"
        + _hook_func("host_load_ratio")
        + floor.group(0)
        + "\n"
        + _hook_func("host_is_fit")
    )


def _pick(
    repo: Path,
    *,
    load: str,
    slot: str,
    uv: str,
    mem: str = "",
    # A repo name NO row denies, so the picker-mechanics tests below measure
    # load/slot/uv/memory ranking and nothing else. This repo's own denial
    # policy is exercised deliberately, by the two tests that pass
    # `repo_name="omnimarket"`, rather than leaking into every ranking test.
    repo_name: str = "picker-fixture-repo",
) -> str:
    body = (
        f'export PREPUSH_LOAD_OVERRIDE_MAP="{load}"\n'
        f'export PREPUSH_SLOT_OVERRIDE_MAP="{slot}"\n'
        f'export PREPUSH_MEM_OVERRIDE_MAP="{mem}"\n'
        f'export PREPUSH_UV_OVERRIDE_MAP="{uv}"\n'
        f"if pick_capacity_host stickybeatz-studio {repo_name}; then\n"
        '  echo "PICK=$PREPUSH_PICK_LABEL"\n'
        "else\n"
        '  echo "PICK=none"\n'
        "fi\n"
        'echo "PROBE=$PREPUSH_PROBE_LOG"\n'
    )
    return _driver(repo, body)


_GOOD_UV = "h200=0.11.32,h201=0.11.5,h101=0.8.3,h105=0.11.8"


def test_picker_chooses_the_least_loaded_fit_host(table_repo: Path) -> None:
    out = _pick(
        table_repo,
        load="h200=0.90,h201=0.44,h105=0.21",
        slot=_ALL_FREE,
        uv=_GOOD_UV,
    )
    assert "PICK=h105" in out


def test_a_busy_host_is_unfit_even_when_it_is_the_least_loaded(
    table_repo: Path,
) -> None:
    """The measured case, not a hypothetical: `.201` read 0.44x -- the fittest
    ratio in the lab -- while running three concurrent pre-push suites behind a
    10-deep queue. load1 is a CPU-time proxy; the scarce resource is an
    exclusive heavy-suite slot."""
    out = _pick(
        table_repo,
        load="h200=0.90,h201=0.10,h105=0.80",
        slot="h200=free,h201=busy,h105=free",
        uv=_GOOD_UV,
    )
    assert "PICK=h105" in out, out
    assert "h201=busy" in out


def test_an_unreachable_host_is_skipped_never_assumed_free(
    table_repo: Path,
) -> None:
    """Silence is not headroom. A host we cannot read is skipped exactly like
    one we measured as over capacity -- the fail-closed posture the load probe
    already had."""
    out = _pick(
        table_repo,
        load="h200=0.90,h105=0.21",
        slot=_ALL_FREE,
        uv=_GOOD_UV,
    )
    assert "PICK=h105" in out
    assert "h201=unreachable" in out


def test_a_host_whose_slot_state_is_unknown_is_skipped(table_repo: Path) -> None:
    out = _pick(
        table_repo,
        load="h200=0.90,h201=0.10,h105=0.21",
        slot="h200=free,h201=unknown,h105=free",
        uv=_GOOD_UV,
    )
    assert "h201=slot-unknown" in out
    assert "PICK=h105" in out


def test_a_host_below_the_uv_floor_is_skipped(table_repo: Path) -> None:
    out = _pick(
        table_repo,
        load="h200=2.09,h201=2.0,h105=0.21",
        slot=_ALL_FREE,
        uv="h200=0.11.32,h201=0.11.5,h105=0.8.3",
    )
    assert "PICK=none" in out
    assert "h105=uv-unfit(0.8.3<0.11.0)" in out


def test_a_repo_denied_host_is_never_chosen(tmp_path: Path) -> None:
    """Driven off a synthetic table because the shipped one no longer denies any
    repo on any row (OMN-16989 lifted h201's `omnibase_infra` denial after the
    full `tests/unit/` suite ran green on that host over the real remote leg).

    The RULE still has to hold for the next row that needs a denial, and pinning
    it to whichever repo the lab happens to deny today made a capacity-policy
    edit look like a mechanism regression -- exactly the failure this run hit."""
    denied_table = _SYNTHETIC_TABLE.replace(
        "ha\tcapacity\thosta\tjonah@hosta\t24\t/bin/uv\t0.1.0\t/tmp/wa\tlockdir\t1\t-\t",
        "ha\tcapacity\thosta\tjonah@hosta\t24\t/bin/uv\t0.1.0\t/tmp/wa\tlockdir\t1\tsomerepo\t",
    )
    assert "\tsomerepo\t" in denied_table, "fixture edit did not take"
    repo = _repo_with_table(tmp_path, denied_table, name="denied")
    out = _pick(
        repo,
        load="ha=0.10,hb=0.21",
        slot="ha=free,hb=free,hs=free",
        uv="ha=9.9.9,hb=9.9.9,hs=9.9.9",
        repo_name="somerepo",
    )
    assert "ha=repo-denied" in out, out
    assert "PICK=hb" in out, out


def test_the_synthetic_denial_fixture_is_still_the_right_shape() -> None:
    """Guards the fixture choice above, INVERTED from its upstream form.

    In omnibase_infra no shipped row denies a repo, so that assertion reads
    "if a real row ever denies again, this fixture can be retired". Here two
    rows DO deny (h101/h105 -- see test_the_shipped_repo_denials_are_pinned),
    so the useful guard is the opposite one: the synthetic fixture must keep
    testing the rule against a table this repo does NOT ship, so the rule stays
    proven if and when the live denials are lifted.
    """
    denied = {r[0]: r[10] for r in _rows()}
    live_denials = {k: v for k, v in denied.items() if v != "-"}
    assert live_denials, (
        "no shipped row denies a repo any more -- the live denials were lifted, "
        "so update test_the_shipped_repo_denials_are_pinned and consider "
        "whether the synthetic fixture is still the clearer proof"
    )
    assert "\tsomerepo\t" not in TABLE.read_text(encoding="utf-8"), (
        "the synthetic fixture's sentinel repo name leaked into the shipped "
        "table -- the two must stay independent"
    )


def test_a_disabled_host_is_never_probed(tmp_path: Path) -> None:
    """Driven off a synthetic table because the shipped one no longer carries
    a disabled row (h101 was promoted, OMN-17161); the RULE still has to hold
    for the next row that starts disabled. The only row is disabled, so a fit
    pick is impossible if -- and only if -- it was actually skipped rather
    than probed."""
    repo = _repo_with_table(tmp_path, _SYNTHETIC_TABLE_DISABLED_ONLY)
    out = _pick(repo, load="hd=0.01", slot="hd=free", uv="hd=9.9.9")
    assert "hd=disabled" in out
    assert "PICK=none" in out


def test_picker_returns_no_host_when_nothing_is_fit(table_repo: Path) -> None:
    """The fallback path. When no host is fit the picker must fail rather than
    return a least-bad guess -- the caller then falls through to the existing
    precedence (GitHub-hosted verify -> grant -> die), which is unchanged."""
    out = _pick(
        table_repo,
        load="h200=2.09,h201=3.10,h105=1.90",
        slot=_ALL_FREE,
        uv=_GOOD_UV,
    )
    assert "PICK=none" in out


def test_every_probed_host_is_recorded_for_the_receipt(table_repo: Path) -> None:
    """A refusal has to be auditable rather than believed, so every probed host
    lands in the trail that the receipt and the die() message both carry."""
    out = _pick(
        table_repo,
        load="h200=2.09,h201=3.10,h105=1.90",
        slot=_ALL_FREE,
        uv=_GOOD_UV,
    )
    for label in ("h200", "h201", "h101", "h105"):
        assert label in out


# =============================================================================
# Per-host slot CAPACITY (OMN-17269): a row may declare slots > 1
# =============================================================================
#
# OMN-16991 gave every capacity row exactly one exclusive slot. Operator
# direction 2026-08-30 ("it looks like .105 can take more load") plus the same
# day's live evidence (h105: load1 2.74/10 = 0.27x, slot FREE) showed the
# binding constraint was the one-slot-per-host model, not host fitness. A row
# with `slots=N` is N independently placeable candidates -- slot 1 keeps the
# bare LABEL (byte-identical to every pre-OMN-17269 row), slot k>=2 is
# `LABEL.k`, its own override-map key -- each re-qualified on LIVE state at
# pick time, never assumed fit because a sibling slot on the same row is free.

_SYNTHETIC_TABLE_MULTISLOT = (
    "#label\trole\thostname\tssh_target\tcores\tuv_abs_path\tuv_min_version"
    "\tworkroot\tslot_mode\tslots\trepos_denied\tmode\theavy_local"
    "\tplacement_tier\tnote\n"
    "hm\tcapacity\thostm\tjonah@hostm\t10\t/bin/uv\t0.1.0\t/tmp/wm\tlockdir\t2\t-\tauthorizing\tallowed\tdefault\ttwo-slot test host\n"
)


def test_the_shipped_slots_column_is_pinned(table_repo: Path) -> None:
    """EVERY row on THIS repo's table declares slots=1 -- deliberately, and
    deliberately UNLIKE omnibase_infra's table, which declares slots=2 for h105.

    The lockdir namespace (``<workroot>/LOCK``, ``LOCK.<k>``) is shared across
    repos, so a per-repo slots value is a self-limit, not a host-wide one:
    omnimarket declaring 1 does not stop an omnibase_infra lane from taking
    LOCK.2 on the same machine. omnibase_infra measured 2 against ITS suite;
    this repo's escalation target is all ~17,700 tests of ``tests/``, and two
    concurrent runs of that on a 10-core M4 was NOT measured by OMN-17435.

    Widening a row's capacity is exactly the kind of change this file exists to
    force through a reviewed, deliberate test edit (same reasoning as the
    mode-promotion pins above) -- and here it additionally requires the
    measurement that has not been taken."""
    slots = {r[0]: r[9] for r in _rows()}
    assert slots == {
        "h200": "1",
        "h201": "1",
        "h201c": "1",
        "h101": "1",
        "h105": "1",
    }


def test_slot_one_keeps_the_bare_label_not_a_dot_one_suffix(
    table_repo: Path,
) -> None:
    """Slot 1 of every row must place under the pre-existing bare LABEL, so a
    slots=1 row is byte-identical in placement to a table with no slots column
    at all. (The ``.<k>`` suffix only ever appears for k>=2; this repo ships no
    such row today -- see test_the_shipped_slots_column_is_pinned -- but the
    picker is shared, so the rule is pinned here regardless.)"""
    out = _pick(
        table_repo,
        load="h200=0.90,h201=0.44,h105=0.21",
        slot=_ALL_FREE,
        uv=_GOOD_UV,
    )
    assert "PICK=h105" in out, out
    assert "PICK=h105.1" not in out, out


def test_both_slots_busy_is_a_placement_miss(tmp_path: Path) -> None:
    """A two-slot row with both slots held offers no placement at all."""
    repo = _repo_with_table(tmp_path, _SYNTHETIC_TABLE_MULTISLOT, name="multislot-a")
    out = _pick(
        repo,
        load="hm=0.10",
        slot="hm=busy,hm.2=busy",
        uv="hm=9.9.9",
        repo_name="omnibase_core",
    )
    assert "PICK=none" in out, out
    assert "hm=busy" in out, out
    assert "hm.2=busy" in out, out


def test_a_second_slot_is_accepted_when_it_re_qualifies_on_measured_load(
    tmp_path: Path,
) -> None:
    """Slot 1 held does not disqualify slot 2 -- slot 2 is probed on ITS OWN
    live state and, measured under threshold, is placeable."""
    repo = _repo_with_table(tmp_path, _SYNTHETIC_TABLE_MULTISLOT, name="multislot-b")
    out = _pick(
        repo,
        load="hm.2=0.30",
        slot="hm=busy,hm.2=free",
        uv="hm.2=9.9.9",
        repo_name="omnibase_core",
    )
    assert "PICK=hm.2" in out, out
    assert "hm=busy" in out, out


def test_a_second_slot_is_refused_when_measured_load_is_high(
    tmp_path: Path,
) -> None:
    """Free slot is necessary but not sufficient -- a free second slot on a
    host whose LIVE load is already over threshold must still refuse. Fitness
    is re-measured at pick time, never assumed from slot availability alone."""
    repo = _repo_with_table(tmp_path, _SYNTHETIC_TABLE_MULTISLOT, name="multislot-c")
    out = _pick(
        repo,
        load="hm.2=2.50",
        slot="hm=busy,hm.2=free",
        uv="hm.2=9.9.9",
        repo_name="omnibase_core",
    )
    assert "PICK=none" in out, out
    assert "hm.2=over(2.50)" in out, out


def test_prepush_select_candidate_exposes_the_slot_index(tmp_path: Path) -> None:
    """The slot a candidate was ranked into must be readable by the caller so
    the remote leg can lock the right LOCK.<k> and the receipt can record it
    (OMN-17269 DoD: receipts record which slot a run held)."""
    repo = _repo_with_table(tmp_path, _SYNTHETIC_TABLE_MULTISLOT, name="multislot-d")
    out = _driver(
        repo,
        'export PREPUSH_LOAD_OVERRIDE_MAP="hm.2=0.10"\n'
        'export PREPUSH_SLOT_OVERRIDE_MAP="hm=busy,hm.2=free"\n'
        'export PREPUSH_UV_OVERRIDE_MAP="hm.2=9.9.9"\n'
        "pick_capacity_host somewhere-else omnibase_core > /dev/null 2>&1\n"
        'echo "LABEL=$PREPUSH_PICK_LABEL SLOT=$PREPUSH_PICK_SLOT"\n',
    )
    assert "LABEL=hm.2 SLOT=2" in out, out


def test_prepush_select_candidate_defaults_slot_to_one(table_repo: Path) -> None:
    """A slot-1 candidate (every pre-OMN-17269 row) reports SLOT=1 explicitly,
    not an empty/unset value that a caller might mishandle."""
    out = _driver(
        table_repo,
        'export PREPUSH_LOAD_OVERRIDE_MAP="h105=0.21"\n'
        f'export PREPUSH_SLOT_OVERRIDE_MAP="{_ALL_FREE}"\n'
        f'export PREPUSH_UV_OVERRIDE_MAP="{_GOOD_UV}"\n'
        "pick_capacity_host somewhere-else omnibase_core > /dev/null 2>&1\n"
        'echo "LABEL=$PREPUSH_PICK_LABEL SLOT=$PREPUSH_PICK_SLOT"\n',
    )
    assert "LABEL=h105 SLOT=1" in out, out


# =============================================================================
# The slot PROBE itself (OMN-17606): why slots>1 was unreachable in practice
# =============================================================================
#
# OMN-17269 shipped the slot MECHANISM upstream in omnibase_infra, and this
# repo vendors that library byte-for-byte (OMN-17435). Every row in THIS
# repo's table is `slots=1`, so the multi-slot arithmetic had never run here
# at all -- and upstream, where h101/h105 do carry `slots=2`, it had never
# actually placed anything either. Measured read-only 2026-09-02T19:11-21:1xZ
# across the fleet: `LOCK.2` had never been created ONCE anywhere, on any
# host, in three days -- h105 121 run dirs, h101 73, a bare `LOCK` only and no
# `slots/` directory. Three defects in `_PREPUSH_SLOT_PROBE_SH` explain it.
#
# Two of the three are not "capacity" defects at all and bite THIS repo
# directly, which is why the fix is ported here rather than waited on:
#
#   * the leg double-count (2) inflates `heavy_pids` on every host in every
#     repo, `slots=1` included;
#   * the empty-QUEUE field shift (3) is live on `h201`, the one `slot_mode=
#     queue` row in this repo's table, whose `~/push-lanes/QUEUE` exists and
#     is empty -- and the shift is FAIL-OPEN as well as false-busy (see
#     test_a_probe_with_the_wrong_field_count_is_unknown_not_shifted).
#
# Every fix makes the predicate LESS strict, so each is pinned by the exact
# arithmetic it restores rather than by "it now returns free": an untracked
# heavy process with no lock to explain it must still read BUSY, and
# test_an_unexplained_heavy_process_is_still_busy asserts exactly that.
#
# WHY THIS WAS NOT CAUGHT BEFORE: `_PREPUSH_SLOT_PROBE_SH` had never been
# EXECUTED by a test in any of the three repos that vendor it. The only
# reference was a string grep, and every slot test routes through
# `PREPUSH_SLOT_OVERRIDE_MAP`, which short-circuits the probe entirely. The
# tests below run the shipped probe body itself.


def _probe_line(out: str) -> list[str]:
    """The probe's last stdout line, split into fields."""
    lines = [ln for ln in out.strip().splitlines() if ln.strip()]
    assert lines, f"probe produced no output: {out!r}"
    return lines[-1].split()


def _fake_ps(bin_dir: Path, *lines: str) -> None:
    """A `ps` stub on PATH that prints LINES for any argv.

    The real signal cannot be produced from a test -- it needs a live remote
    leg -- so the stub reproduces the exact two argv lines a single leg puts
    in `ps`, captured verbatim from h105 at 2026-09-02T21:1xZ.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = "#!/bin/sh\n" + "".join(f"printf '%s\\n' {line!r}\n" for line in lines)
    ps = bin_dir / "ps"
    ps.write_text(script, encoding="utf-8")
    ps.chmod(0o755)


#: The two argv lines ONE remote leg contributes to `ps ax -o args=`, copied
#: from h105 (run omnibase_core-e69568dd5e02-80404, PIDs 86954 + 86956). The
#: first is the ssh wrapper shell, the second is the leg. A count that returns
#: 2 here is counting the shell that spawned the leg as a second leg.
_ONE_LEG_PS = (
    "zsh -c cd '/Users/Shared/onex-prepush/runs/omnibase_core-e69568dd5e02-80404'"  # onex-allow-local-path OMN-17606 reason="verbatim argv of a real remote leg, captured off h105; a normalized path stops reproducing the signal"  # test-literal-ok: same, for the structural gate
    " || exit 96; chmod +x prepush_smart_tests.sh || exit 97;"
    " ./prepush_smart_tests.sh '/Users/Shared/onex-prepush/runs/x' '/uv' 'sha' '1'",  # onex-allow-local-path OMN-17606 reason="verbatim argv of a real remote leg, captured off h105; a normalized path stops reproducing the signal"  # test-literal-ok: same, for the structural gate
    "bash ./prepush_smart_tests.sh /Users/Shared/onex-prepush/runs/x /uv sha 1",  # onex-allow-local-path OMN-17606 reason="verbatim argv of a real remote leg, captured off h105; a normalized path stops reproducing the signal"  # test-literal-ok: same, for the structural gate
)


def test_the_slot_probe_counts_held_locks_under_a_shell_that_rejects_globs(
    table_repo: Path, tmp_path: Path
) -> None:
    """`held` must not depend on the shell expanding an unmatched glob.

    The probe string is handed to `ssh`, which runs it under the TARGET's
    login shell, and the lab Macs' login shell is zsh (measured 2026-09-02:
    `ssh <h101|h105> 'echo $SHELL'` -> /bin/zsh, ZSH_VERSION=5.9). zsh's
    default `nomatch` makes an unmatched glob a FATAL error that aborts the
    command line before it runs -- so the old `ls -d "$W"/LOCK "$W"/LOCK.*`
    printed nothing, and its `2>/dev/null` could not suppress the message
    because the redirection belonged to a command that never executed. `held`
    therefore read 0 on every remote Mac probe in exactly the state slot 2
    exists for: slot 1 locked, slot 2 free. Reproduced portably with bash's
    `failglob`, which has the same semantics; a real-zsh twin runs below
    where zsh exists."""
    wr = tmp_path / "wr"
    (wr / "LOCK").mkdir(parents=True)
    home = tmp_path / "home"
    home.mkdir()
    out = _driver(
        table_repo,
        f'export HOME="{home}"\n'
        f'export PREPUSH_WORKROOT="{wr}"\n'
        "export PREPUSH_SLOT_INDEX=2\n"
        'bash -O failglob -c "$_PREPUSH_SLOT_PROBE_SH"\n',
    )
    fields = _probe_line(out)
    assert len(fields) == 4, fields
    assert fields[2] == "0", f"LOCK.2 does not exist, so l must be 0: {fields}"
    assert fields[3] == "1", f"one held lock dir (LOCK) must be counted: {fields}"


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh not installed")
def test_the_slot_probe_counts_held_locks_under_real_zsh(
    table_repo: Path, tmp_path: Path
) -> None:
    """The same property against the ACTUAL shell the lab Macs run, so the
    portable `failglob` stand-in above can never drift away from the thing it
    stands in for."""
    wr = tmp_path / "wr"
    (wr / "LOCK").mkdir(parents=True)
    home = tmp_path / "home"
    home.mkdir()
    out = _driver(
        table_repo,
        f'export HOME="{home}"\n'
        f'export PREPUSH_WORKROOT="{wr}"\n'
        "export PREPUSH_SLOT_INDEX=2\n"
        'zsh -c "$_PREPUSH_SLOT_PROBE_SH"\n',
    )
    fields = _probe_line(out)
    assert len(fields) == 4, fields
    assert fields[3] == "1", f"one held lock dir (LOCK) must be counted: {fields}"


def test_the_slot_probe_counts_one_process_per_leg_not_the_ssh_wrapper(
    table_repo: Path, tmp_path: Path
) -> None:
    """A single remote leg must count as ONE heavy process, not two.

    The leg is launched as `zsh -c '...; ./prepush_smart_tests.sh ...'`, so
    the wrapper shell AND the script both carry the script name in their
    argv. Measured on h101, h105 and h201 at 2026-09-02T21:1xZ: exactly one
    leg was running on h105 and the old count returned 2. That doubling is
    what defeats `p <= self + held` -- one lock can never explain two
    processes, so a correctly-locked host reads BUSY on every slot, in every
    repo, `slots=1` rows included."""
    wr = tmp_path / "wr"
    wr.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    _fake_ps(tmp_path / "bin", *_ONE_LEG_PS)
    out = _driver(
        table_repo,
        f'export HOME="{home}"\n'
        f'export PATH="{tmp_path / "bin"}:$PATH"\n'
        f'export PREPUSH_WORKROOT="{wr}"\n'
        'sh -c "$_PREPUSH_SLOT_PROBE_SH"\n',
    )
    fields = _probe_line(out)
    assert fields[1] == "1", f"one leg must count once, got p={fields[1]}: {fields}"


def test_the_slot_probe_emits_four_fields_when_the_queue_file_is_empty(
    table_repo: Path, tmp_path: Path
) -> None:
    """`grep -c .` on an EXISTING BUT EMPTY file prints 0 and exits 1, so the
    old `|| echo 0` fired as well and `q` became two lines. Every later field
    then shifted left by one and `l` was read out of `p`.

    This is live on h201 -- the only `slot_mode=queue` row in THIS repo's
    table -- whose `~/push-lanes/QUEUE` exists and is empty (0 bytes, mtime
    2026-09-02T13:20). The 2026-09-02T20:07Z refusal trail printed
    `h201=busy(queue=0 heavy_pids=0 lock=2 held=1)`, and `l` is assigned only
    0 or 1, so `lock=2` is a value the code cannot produce except by
    shifting."""
    wr = tmp_path / "wr"
    wr.mkdir()
    home = tmp_path / "home"
    (home / "push-lanes").mkdir(parents=True)
    (home / "push-lanes" / "QUEUE").write_text("", encoding="utf-8")
    out = _driver(
        table_repo,
        f'export HOME="{home}"\n'
        f'export PREPUSH_WORKROOT="{wr}"\n'
        'sh -c "$_PREPUSH_SLOT_PROBE_SH"\n',
    )
    # The WHOLE probe output, not just its last line: the defect emitted the
    # extra `0` on a line of its own, which a last-line read would hide while
    # `set -- $raw` in the caller still word-splits across the newline and
    # shifts every field.
    words = out.split()
    assert len(words) == 4, f"an empty QUEUE must not add a field: {out!r}"
    assert words[0] == "0", words


def test_a_probe_with_the_wrong_field_count_is_unknown_not_shifted(
    table_repo: Path,
) -> None:
    """Fail closed on a malformed probe instead of reading it shifted.

    The old parse took `${1..4}` positionally with defaults, so the five-word
    output above was accepted and silently misread rather than rejected. That
    is not merely a false BUSY: with a real `p=0` and a real `l=1` -- the
    window between `_lock_acquire`'s mkdir and pytest spawning, which spans
    the whole `uv sync` -- the shifted read is `q=0 p=0 l=0 held=1`, every
    guard passes and `0 <= 0+1` returns FREE on a host whose LOCK is HELD.
    Unknown is skipped exactly like unreachable, which is the rule the whole
    probe is built on, so a future field change degrades to a placement miss
    and never to a wrong verdict."""
    out = _driver(
        table_repo,
        'PREPUSH_SLOT_OVERRIDE="0 0 0 0 1" prepush_slot_state "" /nonexistent 0 1\n'
        'echo "RC=$?"\n',
    )
    assert "RC=2" in out, out
    busy_open = _driver(
        table_repo,
        # The exact five fields h201 returned live, with lock HELD.
        "PREPUSH_SLOT_OVERRIDE=\"$(printf '0\\n0 0 1 1')\""
        ' prepush_slot_state "" /nonexistent 0 1\n'
        'echo "RC=$?"\n'
        'echo "DETAIL=${PREPUSH_SLOT_DETAIL:-unset}"\n',
    )
    assert "RC=2" in busy_open, busy_open
    assert "lock=0" not in busy_open, busy_open


def test_a_second_slot_is_free_when_one_locked_leg_explains_the_heavy_process(
    table_repo: Path, tmp_path: Path
) -> None:
    """The whole point, end to end, against the real probe.

    State: slot 1 locked (`LOCK` present), slot 2 unlocked (no `LOCK.2`), and
    exactly one live leg -- the state every lab Mac was in for hours on
    2026-09-02 while six lanes queued for a placement target. Slot 1 must read
    BUSY and slot 2 must read FREE. Before OMN-17606 both read BUSY. No row in
    THIS repo's table declares `slots=2` yet, so this pins the library's
    contract rather than a live placement here -- and it is the contract the
    upstream table depends on."""
    wr = tmp_path / "wr"
    (wr / "LOCK").mkdir(parents=True)
    home = tmp_path / "home"
    home.mkdir()
    _fake_ps(tmp_path / "bin", *_ONE_LEG_PS)
    out = _driver(
        table_repo,
        f'export HOME="{home}"\n'
        f'export PATH="{tmp_path / "bin"}:$PATH"\n'
        f'prepush_slot_state "" "{wr}" 0 1; echo "SLOT1=$?"\n'
        f'prepush_slot_state "" "{wr}" 0 2; echo "SLOT2=$?"\n',
    )
    assert "SLOT1=3" in out, out
    assert "SLOT2=0" in out, out


def test_an_unexplained_heavy_process_is_still_busy(
    table_repo: Path, tmp_path: Path
) -> None:
    """The fixes must not turn the probe permissive.

    Two independent legs (two wrapper+script pairs, so p=2) with only ONE
    held lock is a host running an untracked heavy process this table cannot
    account for. That must stay BUSY on the free slot -- the `p <= self +
    held` predicate is what makes the probe fail closed, and OMN-17606 only
    restored its inputs, it did not relax it. This guard-rail passes BOTH
    before and after the fix, which is what proves the change restores the
    documented input rather than weakening the predicate."""
    wr = tmp_path / "wr"
    (wr / "LOCK").mkdir(parents=True)
    home = tmp_path / "home"
    home.mkdir()
    _fake_ps(tmp_path / "bin", *(_ONE_LEG_PS + _ONE_LEG_PS))
    out = _driver(
        table_repo,
        f'export HOME="{home}"\n'
        f'export PATH="{tmp_path / "bin"}:$PATH"\n'
        f'prepush_slot_state "" "{wr}" 0 2; echo "SLOT2=$?"\n',
    )
    assert "SLOT2=3" in out, out


# =============================================================================
# The lock
# =============================================================================


def test_lock_is_exclusive(table_repo: Path, tmp_path: Path) -> None:
    wr = tmp_path / "wr"
    out = _driver(
        table_repo,
        f"prepush_lock_acquire {wr} && echo FIRST=ok\n"
        f'( PREPUSH_HELD_LOCK=""; prepush_lock_acquire {wr} && echo SECOND=ok || echo SECOND=blocked )\n',
    )
    assert "FIRST=ok" in out
    assert "SECOND=blocked" in out


def test_lock_is_reusable_after_release(table_repo: Path, tmp_path: Path) -> None:
    wr = tmp_path / "wr"
    out = _driver(
        table_repo,
        f"prepush_lock_acquire {wr} && echo FIRST=ok\n"
        "prepush_lock_release\n"
        f'( PREPUSH_HELD_LOCK=""; prepush_lock_acquire {wr} && echo SECOND=ok || echo SECOND=blocked )\n',
    )
    assert "FIRST=ok" in out
    assert "SECOND=ok" in out


def test_a_lock_whose_holder_is_dead_on_this_machine_is_reclaimed(
    table_repo: Path, tmp_path: Path
) -> None:
    """mkdir(2) is the lock primitive because flock(1) is absent on both Macs
    and its fd idiom needs `exec {fd}<>`, which bash 3.2 cannot parse. What
    mkdir lacks is auto-release on death, so a lock whose holder is provably
    gone is reclaimed -- without this one externally-SIGTERMed run (OMN-16713)
    wedges a host permanently."""
    wr = tmp_path / "wr"
    lockdir = wr / "LOCK"
    lockdir.mkdir(parents=True)
    host = subprocess.run(
        ["hostname", "-s"], capture_output=True, text=True, check=False
    ).stdout.strip()
    # pid 2^22 is above every default pid_max and is reliably absent.
    (lockdir / "holder").write_text(f"4194303 {host} 2026-01-01T00:00:00Z\n")
    out = _driver(
        table_repo,
        f"prepush_lock_acquire {wr} && echo RECLAIM=ok || echo RECLAIM=blocked",
    )
    assert "RECLAIM=ok" in out


def test_a_lock_held_by_a_live_process_is_not_reclaimed(
    table_repo: Path, tmp_path: Path
) -> None:
    wr = tmp_path / "wr"
    lockdir = wr / "LOCK"
    lockdir.mkdir(parents=True)
    host = subprocess.run(
        ["hostname", "-s"], capture_output=True, text=True, check=False
    ).stdout.strip()
    (lockdir / "holder").write_text(f"{os.getpid()} {host} 2026-01-01T00:00:00Z\n")
    out = _driver(
        table_repo,
        f"prepush_lock_acquire {wr} && echo RECLAIM=ok || echo RECLAIM=blocked",
    )
    assert "RECLAIM=blocked" in out


def test_a_lock_held_by_another_machine_is_never_reclaimed(
    table_repo: Path, tmp_path: Path
) -> None:
    """A pid from another host says nothing about whether a process here is
    alive, so a foreign holder is never reaped on a liveness check."""
    wr = tmp_path / "wr"
    lockdir = wr / "LOCK"
    lockdir.mkdir(parents=True)
    (lockdir / "holder").write_text("4194303 some-other-host 2026-01-01T00:00:00Z\n")
    out = _driver(
        table_repo,
        f"prepush_lock_acquire {wr} && echo RECLAIM=ok || echo RECLAIM=blocked",
    )
    assert "RECLAIM=blocked" in out


# =============================================================================
# Precedence and non-bypass invariants (static wiring)
# =============================================================================


def test_the_lab_leg_is_tried_before_the_degraded_capacity_grant() -> None:
    """Precedence by EVIDENCE STRENGTH, on BOTH the designated and the
    undesignated path.

    A designated lab host runs the real suite against this exact tree and binds
    the verdict to the sha with a completion marker. The grant runs a CONTENDED
    suite on the host that was just measured unfit and says so. Ordering the
    grant first would spend the weakest evidence while a fit lab host sat idle
    -- which is the throughput failure OMN-17435 was filed over, inverted.

    omnibase_infra additionally has a sha-pinned GitHub-hosted full-suite rung
    (OMN-16688, ``remote_full_suite_verified``) ABOVE the lab leg. That leg is
    NOT ported to this repo (nor to omnibase_core by OMN-17159): it needs this
    repo's own CI shape wired into ``prepush_remote_verify.py``. Its ABSENCE can
    only remove a PASS path, never add one, so this repo's ladder is strictly
    narrower than infra's, not weaker. This test asserts the rungs that exist
    here are in the right order, and the next test asserts the missing rung has
    not been faked."""
    text = HOOK.read_text(encoding="utf-8")
    start = text.index("guard_full_suite_host() {")
    guard = text[start:]
    for path_name, segment in (
        ("designated-host", guard[: guard.index("# Not a designated host")]),
        ("undesignated-host", guard[guard.index("# Not a designated host") :]),
    ):
        i_lab = segment.index("dispatch_to_lab_host")
        i_grant = segment.index("consume_override_grant")
        # rindex, not index: the designated-host segment opens with two
        # UNRELATED fail-closed die()s (unresolvable hostname, unreadable
        # table) that precede the ladder entirely. The ladder's terminal
        # refusal is the LAST die in the segment.
        i_die = segment.rindex("die ")
        assert i_lab < i_grant < i_die, (
            f"{path_name} path: expected lab leg -> grant -> die, got a different order"
        )


def test_the_unported_github_verify_rung_is_absent_not_stubbed() -> None:
    """The OMN-16688 rung must be genuinely ABSENT, not present as a stub.

    A stub that returns 0 would be a new unconditional PASS path on a
    fail-closed gate -- strictly worse than the honest gap. So the hook must
    not CALL ``remote_full_suite_verified`` anywhere, and this repo must not
    ship a ``prepush_remote_verify.py`` that the hook could start trusting
    without a reviewed change."""
    text = HOOK.read_text(encoding="utf-8")
    called = [
        line
        for line in text.splitlines()
        if "remote_full_suite_verified" in line and not line.lstrip().startswith("#")
    ]
    assert not called, (
        "the hook references remote_full_suite_verified outside a comment; "
        f"the OMN-16688 rung is not ported here: {called}"
    )
    assert not (
        REPO_ROOT / "scripts" / "hooks" / "prepush_remote_verify.py"
    ).exists(), (
        "prepush_remote_verify.py appeared without the hook rung that consumes "
        "it -- port both or neither"
    )


def test_a_remote_red_refuses_and_never_falls_through_to_a_grant() -> None:
    """A suite that genuinely failed on a designated host is a red gate, not a
    capacity problem. Letting it fall through to `consume_override_grant` would
    be a bypass wearing the word "fallback"."""
    text = HOOK.read_text(encoding="utf-8")
    start = text.index("dispatch_to_lab_host() {")
    body = text[start : text.index("guard_full_suite_host() {")]
    assert "3)" in body, "expected an rc=3 (remote RED) branch in dispatch_to_lab_host"
    assert "die " in body, (
        "expected the rc=3 (remote RED) branch of dispatch_to_lab_host to die"
    )
    red_branch = body[body.index("    3)") :]
    assert "die " in red_branch.split("esac")[0], (
        "the remote-RED branch must refuse, not return and fall through"
    )


def test_the_hook_introduces_no_new_bypass_env_knob() -> None:
    """Every knob added by OMN-16991 either routes work or makes the gate run
    MORE of it. None can make it accept less: the entry rejection of
    PREPUSH_ALLOW_* and the recursion sentinel are untouched."""
    text = HOOK.read_text(encoding="utf-8")
    assert "reject_inherited_env_overrides" in text
    assert 'if [ -n "${ONEX_PREPUSH_HOOK_ACTIVE:-}" ]; then' in text
    lib = LIB.read_text(encoding="utf-8")
    assert "PREPUSH_ALLOW" not in lib, (
        "the distribution library must not read any PREPUSH_ALLOW_* variable"
    )


def test_the_remote_command_rearms_both_guards() -> None:
    """ssh forwards neither the recursion sentinel nor the env scrub. Without
    re-arming, the remote repo's own suite -- which subprocesses this hook --
    takes FIRST-entry behavior there, resolves the selector, picks a host and
    ships another bundle: an unbounded DISTRIBUTED variant of the
    OMN-16425/OMN-16489 F-01 recursion (~9h03m, 44,064 tests)."""
    lib = LIB.read_text(encoding="utf-8")
    remote = lib[lib.index("cat > \"$runner\" <<'REMOTE'") : lib.index("\nREMOTE\n")]
    assert "export ONEX_PREPUSH_HOOK_ACTIVE=" in remote
    assert "PREPUSH_[A-Za-z0-9_]*" in remote, (
        "expected every PREPUSH_* name to be unset"
    )
    assert "unset ENABLE_SMART_TESTS" in remote


def test_the_verdict_is_read_from_a_marker_not_the_ssh_exit_code() -> None:
    """ssh returns 255 on transport failure (indistinguishable from a test
    failure) and any backgrounding wrapper returns 0 with nothing having run --
    a fail-OPEN shape. The marker binds the verdict to this tree and this argv;
    absence or mismatch is NO evidence."""
    lib = LIB.read_text(encoding="utf-8")
    assert 'readback="$(ssh' in lib, (
        "the verdict must be READ BACK from the target host, not inferred here"
    )
    assert 'marker="$(printf \'%s\\n\' "$readback"' in lib
    assert '"$m_head" != "$head_sha"' in lib
    assert '"$m_argv" != "$argv_sha"' in lib
    assert "NO EVIDENCE" in lib
    # The streaming pipeline's status belongs to sed(1), and `|| true` follows
    # it, so nothing about the verdict can come from that command's exit code.
    #
    # OMN-17564 lifted the wrapper invocation into `$remote_cmd` (it is now
    # issued twice -- once timeout-wrapped, once not, depending on whether
    # timeout(1) exists on the pusher), so the invocation and the pipeline that
    # streams it are no longer adjacent and a fixed window after the invocation
    # would pin nothing. Assert the property directly instead, on EVERY
    # streaming branch: a branch added later without the discard would
    # reintroduce exactly the fail-open shape this test exists for.
    assert "./prepush_smart_tests.sh '${rundir}'" in lib, (
        "the remote command no longer invokes the wrapper"
    )
    streams = [m.start() for m in re.finditer(r'"\$remote_cmd" 2>&1 \|', lib)]
    assert streams, "no streaming invocation of $remote_cmd found"
    for idx in streams:
        window = lib[idx : idx + 200]
        assert 'sed "s/^/[${label}] /" >&2 || true' in window, window


def test_a_shadow_host_verdict_never_authorizes() -> None:
    lib = LIB.read_text(encoding="utf-8")
    idx = lib.index('if [ "$PREPUSH_PICK_MODE" = "shadow" ]')
    branch = lib[idx : idx + 500]
    assert "return 1" in branch, (
        "a shadow host must fall through to the normal precedence, never authorize"
    )


def test_the_remote_wrapper_is_visible_to_the_201_queue_gate() -> None:
    """`.201`'s queue runner gates every lane on
    `ps ax | grep prepush_smart_tests.sh` ("covers foreign runs not launched
    through this queue"). Naming the remote wrapper to match makes a
    distributed run share that one mutex instead of becoming another foreign
    detached run -- the defect class OMN-16968 is open against."""
    lib = LIB.read_text(encoding="utf-8")
    assert 'runner="${localdir}/prepush_smart_tests.sh"' in lib
    assert "prepush_smart_tests.sh" in lib[lib.index("_PREPUSH_SLOT_PROBE_SH") :][:600]


def test_the_local_heavy_path_takes_the_host_lock() -> None:
    """OMN-16174: the local path took no lock of any kind, which is why five
    concurrent full suites once ran on one host with one taking 97+ minutes. It
    was the busiest path in the hook and the only unserialized one.

    OMN-17392 moved this block into `prepush_try_local_heavy_slot` so the
    `allowed` path and the post-off-box-wait fallback share ONE definition of
    "may run here". That is exactly why the assertion follows it rather than
    being relaxed: two call sites now depend on this lock, so losing it would
    be twice as bad as when the test was written.
    """
    text = HOOK.read_text(encoding="utf-8")
    start = text.index("prepush_try_local_heavy_slot() {")
    body = text[start:]
    body = body[: body.index("\n}\n")]
    assert 'host_is_fit ""' in body
    assert "prepush_lock_acquire" in body
    assert "prepush_local_workroot" in body


def test_the_escalation_argv_stays_a_superset_of_the_narrow_selection() -> None:
    """OMN-16825: the heavy call site ships $FULL_SUITE_TARGET **plus** the
    allowlisted service-free integration paths, so a remote escalation can
    never run FEWER of the impacted tests than the narrowing it replaces."""
    lib = LIB.read_text(encoding="utf-8")
    argv = lib[lib.index("prepush_remote_argv() {") :]
    argv = argv[: argv.index("\n}\n")]
    assert "FULL_SUITE_TARGET" in argv
    assert "RUNNABLE_INTEGRATION_PATHS" in argv
    assert "PATHS" in argv


def test_the_hook_declares_the_integration_addendum_array_even_when_empty() -> None:
    """``RUNNABLE_INTEGRATION_PATHS`` must be DECLARED by the caller, not left
    unset, and it must be declared BEFORE the heavy call site can reach the
    picker that reads it.

    macOS ships bash 3.2, which runs this hook on every lab Mac, and under
    ``set -u`` it raises "unbound variable" for ``${#NAME[@]}`` on a
    never-declared array -- where newer bash quietly answers 0.
    ``prepush_remote_argv`` in the byte-identical picker reads that name
    unconditionally, so leaving it undeclared aborts the remote leg on exactly
    the hosts this port exists to reach. Empty is correct HERE (this repo's
    escalation target is all of ``tests/``, already a superset of every
    selectable path); undeclared is not."""
    text = HOOK.read_text(encoding="utf-8")
    decl = [
        line
        for line in text.splitlines()
        if line.startswith("RUNNABLE_INTEGRATION_PATHS=")
    ]
    assert decl == ["RUNNABLE_INTEGRATION_PATHS=()"], (
        "expected exactly one top-level `RUNNABLE_INTEGRATION_PATHS=()` "
        f"declaration in the hook, found {decl!r}"
    )
    assert text.index("RUNNABLE_INTEGRATION_PATHS=()") < text.index(
        'if [ "$IS_FULL" = "True" ]'
    ), "the array must be declared before the heavy call site reads it"


def test_the_hook_cites_no_document_this_repo_does_not_have() -> None:
    """A refusal that names a missing runbook is a dead end at the exact moment
    someone is blocked.

    The pre-OMN-17435 hook cited ``docs/runbooks/200-build-lane-execution-pattern.md``
    in three refusal paths; that file has never existed in this repo. The
    replacement is the host table's own header, which ships in the same commit
    as the rows it documents and therefore cannot go missing separately.

    SCOPE: this asserts over the file THIS repo authors (the hook). The two
    vendored files are byte-identical copies whose doc pointers resolve in
    omnibase_infra, their home -- see
    test_every_vendored_file_matches_its_recorded_digest. Editing a pointer
    inside a vendored copy would fork it, which is a strictly worse outcome
    than a pointer that resolves one repo over.
    """
    text = HOOK.read_text(encoding="utf-8")
    assert "200-build-lane-execution-pattern" not in text, (
        "the hook still cites a runbook that does not exist"
    )
    for ref in sorted(set(re.findall(r"docs/[A-Za-z0-9_./-]+\.md", text))):
        assert (REPO_ROOT / ref).is_file(), (
            f"the hook cites {ref}, which does not exist in this repo. Point at "
            "the host table's own header instead, or ship the document"
        )
    header = TABLE.read_text(encoding="utf-8").split("#label")[0]
    for column in ("authorizing", "repos_denied"):
        assert column in header, (
            f"the table header must document {column!r} -- a reader has to set "
            "it to add or re-enable a host, and the refusals now point here"
        )


def test_an_unusable_workroot_is_reported_as_infrastructural_not_contention(
    table_repo: Path, tmp_path: Path
) -> None:
    """rc 2 (workroot unusable) must stay distinguishable from rc 1
    (contended). Conflating them would make a permissions problem look like a
    busy host and start refusing heavy pushes that passed before this lock
    existed -- inventing a refusal out of an infrastructural failure."""
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("i am a file")
    out = _driver(
        table_repo,
        f'rc=0; prepush_lock_acquire {blocker}/wr || rc=$?; echo "RC=$rc"',
    )
    assert "RC=2" in out


def test_the_local_fit_path_proceeds_when_the_workroot_is_unusable() -> None:
    """An unusable workroot says nothing about capacity, so the hook must fall
    back to its pre-OMN-16991 behavior rather than refuse."""
    text = HOOK.read_text(encoding="utf-8")
    start = text.index("prepush_try_local_heavy_slot() {")
    fit = text[start:]
    fit = fit[: fit.index("\n}\n")]
    assert '[ "$lock_rc" -eq 2 ]' in fit
    assert "running unserialized on this host" in fit


# =============================================================================
# The row scan must reach every host (OMN-16991 verify finding 1)
# =============================================================================


def test_the_picker_scans_every_row_even_when_a_probe_consumes_stdin(
    table_repo: Path,
) -> None:
    """The whole lab must be evaluated, not just whichever row sorts first.

    The picker's loop body invokes ssh(1) three times per row, and ssh reads
    its parent's stdin unless given ``-n``. While the row list WAS the loop's
    stdin, the first probe swallowed every remaining row: the real picker on
    the real network emitted ``PROBE=[h200=fit(0.9,authorizing)]`` and never
    evaluated h201/h101/h105, so a lab with three idle hosts refused the push
    and the feature added exactly zero capacity.

    Reproduced here without a network by stubbing the three probes to DRAIN
    stdin, which is precisely what ssh does. Under the old here-doc-fed loop
    this test sees one label; under the array scan it sees all four.
    """
    body = (
        'host_load_ratio() { while IFS= read -r _junk; do :; done; printf "1.0 10 0.10\\n"; }\n'
        "prepush_slot_state() { while IFS= read -r _junk; do :; done; PREPUSH_SLOT_DETAIL=stub; return 0; }\n"
        "prepush_uv_version_ok() { while IFS= read -r _junk; do :; done; PREPUSH_UV_VERSION_SEEN=9.9.9; return 0; }\n"
        "pick_capacity_host stickybeatz-studio omnibase_core > /dev/null 2>&1 || true\n"
        'echo "PROBE=$PREPUSH_PROBE_LOG"\n'
    )
    out = _driver(table_repo, body)
    for label in ("h200", "h201", "h101", "h105"):
        assert label in out, (
            f"{label} was never evaluated -- the row scan was truncated: {out!r}"
        )


def test_every_ssh_invocation_carries_dash_n(table_repo: Path) -> None:
    """Belt and braces for the same defect, from the other side.

    The array scan alone would fix it, but a stdin-eating probe inside ANY
    future loop reintroduces it silently -- a truncated scan looks exactly like
    a small lab. ``-n`` makes ssh structurally incapable of it.
    """
    invocation = re.compile(r"(?<![\w./-])ssh\s+(-\S+)")
    for path in (LIB, HOOK):
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if line.lstrip().startswith("#"):
                continue
            for match in invocation.finditer(line):
                assert match.group(1) == "-n", (
                    f"{path.name}:{lineno} invokes ssh without -n; inside a row "
                    f"loop that eats the remaining rows: {line.strip()!r}"
                )


# =============================================================================
# A shadow host must never win placement (OMN-16991 verify finding 3)
# =============================================================================


def test_a_shadow_row_never_wins_placement_over_an_authorizing_host(
    tmp_path: Path,
) -> None:
    """Ranking on load alone let the idlest host win regardless of its mode.

    Live dry-run against the shipped picker before this fix:
    ``h200=fit(0.90,authorizing) h201=fit(0.30,authorizing)
    h105=fit(0.20,shadow) -> PICK=h105``. A shadow verdict cannot satisfy the
    escalation, so the run was dispatched, a bundle + scp + `uv sync` + a full
    suite were paid for, and the answer was then discarded -- while the
    authorizing host that could have answered was passed over. Mode is now an
    eligibility filter applied BEFORE the probe, not a post-hoc veto.
    """
    repo = _repo_with_table(tmp_path, _SYNTHETIC_TABLE)
    out = _driver(
        repo,
        'export PREPUSH_LOAD_OVERRIDE_MAP="ha=0.90,hb=0.30,hs=0.05"\n'
        'export PREPUSH_SLOT_OVERRIDE_MAP="ha=free,hb=free,hs=free"\n'
        'export PREPUSH_UV_OVERRIDE_MAP="ha=1.0.0,hb=1.0.0,hs=1.0.0"\n'
        "if pick_capacity_host somewhere-else omnibase_core; then\n"
        '  echo "PICK=$PREPUSH_PICK_LABEL"\n'
        "else\n"
        '  echo "PICK=none"\n'
        "fi\n"
        'echo "PROBE=$PREPUSH_PROBE_LOG"\n',
    )
    assert "PICK=hb" in out, out
    assert "hs=mode-shadow-not-eligible" in out, out
    assert "hs=fit" not in out, "a shadow row must not even be probed for placement"


def test_the_eligible_mode_is_a_parameter_not_a_hardcoded_authorizing(
    tmp_path: Path,
) -> None:
    """Shadow is still a supported mode -- it is just not a candidate for a
    verdict-bearing run. Pinning the parameter keeps a future shadow-day tool
    from having to re-implement the picker to get at those rows."""
    repo = _repo_with_table(tmp_path, _SYNTHETIC_TABLE)
    out = _driver(
        repo,
        'export PREPUSH_LOAD_OVERRIDE_MAP="ha=0.90,hb=0.30,hs=0.05"\n'
        'export PREPUSH_SLOT_OVERRIDE_MAP="ha=free,hb=free,hs=free"\n'
        'export PREPUSH_UV_OVERRIDE_MAP="ha=1.0.0,hb=1.0.0,hs=1.0.0"\n'
        "pick_capacity_host somewhere-else omnibase_core shadow > /dev/null 2>&1\n"
        'echo "PICK=$PREPUSH_PICK_LABEL"\n',
    )
    assert "PICK=hs" in out, out


def test_the_picker_ranks_every_fit_host_not_just_the_winner(
    tmp_path: Path,
) -> None:
    """Placement is a ranked list so a candidate that fails to answer costs the
    next-best host, not the whole escalation."""
    repo = _repo_with_table(tmp_path, _SYNTHETIC_TABLE)
    out = _driver(
        repo,
        'export PREPUSH_LOAD_OVERRIDE_MAP="ha=0.90,hb=0.30,hs=0.05"\n'
        'export PREPUSH_SLOT_OVERRIDE_MAP="ha=free,hb=free,hs=free"\n'
        'export PREPUSH_UV_OVERRIDE_MAP="ha=1.0.0,hb=1.0.0,hs=1.0.0"\n'
        "pick_capacity_host somewhere-else omnibase_core > /dev/null 2>&1\n"
        'echo "COUNT=$(prepush_candidate_count)"\n'
        'prepush_select_candidate 1 && echo "FIRST=$PREPUSH_PICK_LABEL"\n'
        'prepush_select_candidate 2 && echo "SECOND=$PREPUSH_PICK_LABEL"\n'
        'prepush_select_candidate 3 || echo "THIRD=none"\n',
    )
    assert "COUNT=2" in out, out
    assert "FIRST=hb" in out, out
    assert "SECOND=ha" in out, out
    assert "THIRD=none" in out, out


# =============================================================================
# A failed pick must try the next fit host (OMN-16991 verify finding 3)
# =============================================================================


def _extract_shell_function(path: Path, name: str) -> str:
    """The SHIPPED text of one shell function, so these assertions drive the
    code that runs on a push rather than a Python restatement of it."""
    text = path.read_text(encoding="utf-8")
    start = text.index(f"{name}() {{")
    end = text.index("\n}\n", start) + len("\n}\n")
    return text[start:end]


def _dispatch_driver(repo: Path, remote_run_stub: str) -> str:
    body = (
        'export PREPUSH_LOAD_OVERRIDE_MAP="ha=0.90,hb=0.30,hs=0.05"\n'
        'export PREPUSH_SLOT_OVERRIDE_MAP="ha=free,hb=free,hs=free"\n'
        'export PREPUSH_UV_OVERRIDE_MAP="ha=1.0.0,hb=1.0.0,hs=1.0.0"\n'
        "PREPUSH_LC_HOST=somewhere-else\n"
        "REMOTE_LAB_RUN_VERDICT=0\n"
        + _extract_shell_function(HOOK, "dispatch_to_lab_host")
        + remote_run_stub
        + 'if dispatch_to_lab_host "heavy thing"; then\n'
        '  echo "RESULT=satisfied verdict=$REMOTE_LAB_RUN_VERDICT host=$PREPUSH_PICK_LABEL"\n'
        "else\n"
        '  echo "RESULT=no-evidence"\n'
        "fi\n"
    )
    return _driver_both(repo, body)


def test_dispatch_tries_the_next_ranked_host_when_the_first_yields_no_evidence(
    tmp_path: Path,
) -> None:
    """ "No completion marker" says nothing about the tree -- it is a placement
    miss. Before this fix the whole escalation was staked on one host: a single
    unreachable-on-arrival candidate refused a push that the second-ranked
    host, idle and reachable, would have cleared."""
    repo = _repo_with_table(tmp_path, _SYNTHETIC_TABLE)
    out = _dispatch_driver(
        repo,
        'prepush_remote_run() { echo "TRIED=$PREPUSH_PICK_LABEL";'
        ' [ "$PREPUSH_PICK_LABEL" = "hb" ] && return 1; return 0; }\n',
    )
    assert "TRIED=hb" in out, out
    assert "TRIED=ha" in out, out
    assert "RESULT=satisfied verdict=1 host=ha" in out, out


def test_dispatch_tries_the_next_ranked_host_when_the_slot_is_taken_on_arrival(
    tmp_path: Path,
) -> None:
    """rc 4 = the target's heavy-suite slot was held when the wrapper landed,
    so NO suite ran there. That is a placement miss too, and refusing on it
    would turn a race with another dispatcher into a failed push."""
    repo = _repo_with_table(tmp_path, _SYNTHETIC_TABLE)
    out = _dispatch_driver(
        repo,
        'prepush_remote_run() { echo "TRIED=$PREPUSH_PICK_LABEL";'
        ' [ "$PREPUSH_PICK_LABEL" = "hb" ] && return 4; return 0; }\n',
    )
    assert "TRIED=hb" in out
    assert "TRIED=ha" in out
    assert "RESULT=satisfied verdict=1 host=ha" in out, out


def test_dispatch_refuses_on_a_remote_red_without_shopping_for_a_greener_host(
    tmp_path: Path,
) -> None:
    """The retry loop must not become verdict shopping. A RED is a verdict --
    the suite genuinely failed on a host we designated -- so it refuses right
    there and never asks a second host for a nicer answer."""
    repo = _repo_with_table(tmp_path, _SYNTHETIC_TABLE)
    out = _dispatch_driver(
        repo,
        'prepush_remote_run() { echo "TRIED=$PREPUSH_PICK_LABEL";'
        ' [ "$PREPUSH_PICK_LABEL" = "hb" ] && return 3; return 0; }\n',
    )
    assert "TRIED=hb" in out
    assert "TRIED=ha" not in out, "a remote RED must not fall through to another host"
    assert "DIE:" in out, out
    assert "RESULT=" not in out


def test_dispatch_reports_no_evidence_when_no_ranked_host_answers(
    tmp_path: Path,
) -> None:
    repo = _repo_with_table(tmp_path, _SYNTHETIC_TABLE)
    out = _dispatch_driver(repo, "prepush_remote_run() { return 1; }\n")
    assert "RESULT=no-evidence" in out, out


def test_dispatch_asks_the_picker_for_authorizing_rows_explicitly() -> None:
    """The verdict-bearing path names the mode it needs at the call site, so a
    later default change cannot quietly make shadow hosts placeable again."""
    body = _extract_shell_function(HOOK, "dispatch_to_lab_host")
    assert 'pick_capacity_host "$PREPUSH_LC_HOST" "$repo" authorizing' in body


# =============================================================================
# The remote leg must take the TARGET host's slot (OMN-16991 verify finding 2)
# =============================================================================


def _remote_wrapper_text() -> str:
    """The wrapper exactly as it is shipped to the target host."""
    lib = LIB.read_text(encoding="utf-8")
    opener = "cat > \"$runner\" <<'REMOTE'\n"
    start = lib.index(opener) + len(opener)
    return lib[start : lib.index("\nREMOTE\n", start)] + "\n"


def _self_hostname() -> str:
    return subprocess.run(
        ["hostname", "-s"], capture_output=True, text=True, check=False
    ).stdout.strip()


@pytest.fixture
def remote_run_env(tmp_path: Path) -> dict[str, Path]:
    """A materialized remote-side run: workroot, rundir, a real git bundle, an
    argv file, the shipped wrapper, and a fake `uv` that records whether the
    host lock was held WHILE the suite ran."""
    src = tmp_path / "src"
    (src / "tests").mkdir(parents=True)
    (src / "tests" / "test_a.py").write_text("def test_a():\n    assert True\n")
    subprocess.run(["git", "init", "-q", "."], cwd=src, check=True)
    subprocess.run(["git", "add", "-A"], cwd=src, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "t"],
        cwd=src,
        check=True,
    )
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=src,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    workroot = tmp_path / "workroot"
    rundir = workroot / "runs" / "r1"
    rundir.mkdir(parents=True)
    subprocess.run(
        ["git", "bundle", "create", str(rundir / "tree.bundle"), "HEAD"],
        cwd=src,
        check=True,
        capture_output=True,
    )
    (rundir / "argv.txt").write_text("tests\n")

    wrapper = rundir / "prepush_smart_tests.sh"
    wrapper.write_text(_remote_wrapper_text())
    wrapper.chmod(0o755)

    witness = tmp_path / "lock_witness"
    fake_uv = tmp_path / "uv"
    fake_uv.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "sync" ]; then exit 0; fi\n'
        # Proof that the target-host slot is held for the DURATION of the run,
        # not merely acquired and dropped before the expensive part.
        'if [ -d "$LOCK_PROBE" ]; then echo held > "$LOCK_WITNESS"; '
        'else echo free > "$LOCK_WITNESS"; fi\n'
        'echo "collected 3 items"\n'
        'exit "${FAKE_UV_EXIT:-0}"\n'
    )
    fake_uv.chmod(0o755)

    return {
        "workroot": workroot,
        "rundir": rundir,
        "uv": fake_uv,
        "witness": witness,
        "head": head,  # type: ignore[dict-item]
    }


def _run_wrapper(
    env_info: dict[str, Path],
    *,
    extra_env: dict[str, str] | None = None,
    extra_argv: list[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "LOCK_PROBE": str(env_info["workroot"] / "LOCK"),
        "LOCK_WITNESS": str(env_info["witness"]),
    }
    env.update(extra_env or {})
    return subprocess.run(
        [
            "bash",
            str(env_info["rundir"] / "prepush_smart_tests.sh"),
            str(env_info["rundir"]),
            str(env_info["uv"]),
            str(env_info["head"]),
            "argvsha",
            "origin-host:1",
            str(env_info["workroot"]),
            *(extra_argv or []),
        ],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
        stdin=subprocess.DEVNULL,
        env=env,
    )


def test_the_remote_leg_holds_the_target_hosts_lock_for_the_whole_run(
    remote_run_env: dict[str, Path],
) -> None:
    """The remote leg took NO lock on the target before this fix.

    Polled live during a real 25s dispatch to omnibook: ``LOCK=no`` throughout,
    and afterwards the workroot held only ``runs/`` -- after two real
    dispatches. So the local heavy path (which DOES take the lock) and a
    transplanted suite could run on the same host at the same time: OMN-16174's
    overlap, reopened across the local/remote boundary. Remote exclusion rested
    entirely on ``ps ax | grep prepush_smart_tests.sh``, which has a
    probe -> scp -> exec race window.
    """
    result = _run_wrapper(remote_run_env)
    assert result.returncode == 0, result.stderr
    assert remote_run_env["witness"].read_text().strip() == "held", (
        "the target host's LOCK was not held while the suite was executing"
    )
    marker = (remote_run_env["rundir"] / "MARKER").read_text()
    assert "exit=0" in marker
    assert "collected=3" in marker
    assert not (remote_run_env["workroot"] / "LOCK").exists(), (
        "the lock must be released when the wrapper exits"
    )


def test_the_remote_leg_releases_the_lock_even_when_the_suite_fails(
    remote_run_env: dict[str, Path],
) -> None:
    """A red suite must not wedge the host. Release is an EXIT trap, not a
    line after the happy path."""
    result = _run_wrapper(remote_run_env, extra_env={"FAKE_UV_EXIT": "1"})
    assert result.returncode == 1
    assert "exit=1" in (remote_run_env["rundir"] / "MARKER").read_text()
    assert not (remote_run_env["workroot"] / "LOCK").exists()


def test_the_remote_wrapper_locks_a_numbered_lockdir_for_slot_two(
    remote_run_env: dict[str, Path],
) -> None:
    """OMN-17269: SLOT_INDEX (positional arg 9) selects WHICH lockdir this
    dispatch holds. Slot 1 (the default, exercised by every other test in this
    file) keeps the bare `LOCK` path; slot 2 must hold `LOCK.2` instead -- a
    DIFFERENT directory, not merely a different witness of the same one, so a
    second concurrent lane can hold its own exclusive lock on the same host
    without contending slot 1's."""
    workroot = remote_run_env["workroot"]
    slot2_probe = workroot / "LOCK.2"
    env = {
        **os.environ,
        "LOCK_PROBE": str(slot2_probe),
        "LOCK_WITNESS": str(remote_run_env["witness"]),
    }
    result = subprocess.run(
        [
            "bash",
            str(remote_run_env["rundir"] / "prepush_smart_tests.sh"),
            str(remote_run_env["rundir"]),
            str(remote_run_env["uv"]),
            str(remote_run_env["head"]),
            "argvsha",
            "origin-host:1",
            str(workroot),
            "",
            "",
            "2",
        ],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
        stdin=subprocess.DEVNULL,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    assert remote_run_env["witness"].read_text().strip() == "held", (
        "slot 2 must hold LOCK.2 while the suite runs, not the bare LOCK dir "
        "slot 1 uses"
    )
    assert not slot2_probe.exists(), "LOCK.2 must be released when the wrapper exits"
    assert not (workroot / "LOCK").exists(), (
        "a slot-2 dispatch must never touch slot 1's bare LOCK dir"
    )


def test_the_remote_leg_refuses_when_the_target_slot_is_already_held(
    remote_run_env: dict[str, Path],
) -> None:
    """Exit 94 is "no suite ran here", which the caller turns into "try the
    next ranked host" -- never into a verdict about the tree."""
    lockdir = remote_run_env["workroot"] / "LOCK"
    lockdir.mkdir(parents=True)
    (lockdir / "holder").write_text(
        f"{os.getpid()} {_self_hostname()} 2026-01-01T00:00:00Z\n"
    )
    result = _run_wrapper(remote_run_env)
    assert result.returncode == 94, result.stderr
    assert "REMOTE_LOCK_CONTENDED" in result.stderr
    assert not (remote_run_env["rundir"] / "MARKER").exists(), (
        "a contended slot must produce no marker -- a marker is a verdict"
    )
    assert lockdir.exists(), "the live holder's lock must survive the refusal"


def test_the_remote_leg_reclaims_a_lock_whose_holder_died_on_that_host(
    remote_run_env: dict[str, Path],
) -> None:
    """mkdir(2) does not auto-release on death, so one externally-SIGTERMed run
    (OMN-16713) would wedge the host forever without this."""
    lockdir = remote_run_env["workroot"] / "LOCK"
    lockdir.mkdir(parents=True)
    (lockdir / "holder").write_text(
        f"4194303 {_self_hostname()} 2026-01-01T00:00:00Z\n"
    )
    result = _run_wrapper(remote_run_env)
    assert result.returncode == 0, result.stderr
    assert remote_run_env["witness"].read_text().strip() == "held"


def test_the_remote_leg_never_reclaims_a_lock_held_from_another_machine(
    remote_run_env: dict[str, Path],
) -> None:
    """A pid from another host says nothing about whether a process HERE is
    alive, so a foreign holder is never reaped on a liveness check."""
    lockdir = remote_run_env["workroot"] / "LOCK"
    lockdir.mkdir(parents=True)
    (lockdir / "holder").write_text("4194303 some-other-host 2026-01-01T00:00:00Z\n")
    result = _run_wrapper(remote_run_env)
    assert result.returncode == 94, result.stderr


def test_the_remote_command_carries_no_set_e_that_would_eat_the_wrapper_exit() -> None:
    """Under ``set -e`` a failing (or slot-contended, exit 94) wrapper aborts
    the remote shell BEFORE ``rc=$?`` runs, so the one fact this leg needs --
    why the wrapper stopped -- is the fact that never gets written."""
    lib = LIB.read_text(encoding="utf-8")
    cmd = lib[lib.index("./prepush_smart_tests.sh '${rundir}'") - 400 :][:900]
    assert "set -e;" not in cmd, cmd
    assert "WRAPPER_EXIT" in cmd
    assert 'wrapper_exit:-}" = "94"' in lib, (
        "the contended-slot code must be routed to a try-the-next-host result"
    )


# =============================================================================
# Housekeeping invariants
# =============================================================================


def test_the_lock_release_and_the_tempfile_cleanup_share_one_exit_trap() -> None:
    """bash keeps exactly ONE EXIT trap per shell. The guard used to install
    ``trap prepush_lock_release EXIT`` after the hook had already installed the
    mktemp cleanup, silently replacing it and leaking three temp files on every
    heavy run that took the host slot."""
    text = HOOK.read_text(encoding="utf-8")
    traps = re.findall(r"^\s*trap\s+\S+\s+EXIT", text, flags=re.MULTILINE)
    assert len(traps) == 1, f"expected exactly one EXIT trap, found {traps}"
    cleanup = _extract_shell_function(HOOK, "prepush_hook_cleanup")
    assert "CHANGED_FILE" in cleanup
    assert "prepush_lock_release" in cleanup


def test_the_remote_leg_reclaims_the_transplanted_tree() -> None:
    """A clone plus ``uv sync --all-extras`` is ~0.5 GB per run and nothing
    pruned it: two dispatches left 1.0 GB on omnibook, the host the picker
    prefers, which fills a laptop disk in a few hundred pushes and then fails
    runs for a reason that looks nothing like its cause."""
    lib = LIB.read_text(encoding="utf-8")
    gc = _extract_shell_function(LIB, "prepush_remote_gc")
    assert "rm -rf '${2}/tree'" in gc
    assert "-mtime +3" in gc
    run = lib[lib.index("prepush_remote_run() {") :]
    assert run.count("prepush_remote_gc ") >= 4, (
        "every terminal path of the remote leg must reclaim the tree"
    )


def test_a_remote_red_fetches_the_suite_log_it_tells_you_to_read() -> None:
    """The refusal instructs the developer to read the streamed output, but the
    wrapper redirects pytest into ``$RUNDIR/suite.log`` on the REMOTE host --
    so before this there was nothing above to read and a remote RED, which
    hard-blocks the push, was undiagnosable without a manual ssh."""
    lib = LIB.read_text(encoding="utf-8")
    assert "tail -n 200 '${rundir}/suite.log'" in lib
    red = lib[lib.index('if [ "$m_exit" -ne 0 ]; then') :][:900]
    assert "suite.log" in red


# =============================================================================
# The pytest-side guard reads the SAME table (OMN-16991 verify finding 4)
# =============================================================================
#
# This is the coupling that made the shadow mode useless. A dispatched run is
# executed by a TRANSPLANTED copy of this repo, and that copy carries this
# repo's own conftest.py -> scripts/hooks/pytest_full_suite_host_guard.enforce,
# which refuses a full-suite target on any host outside the authorizing set.
# So while omnibook was `shadow`, every heavy dispatch to it exited nonzero at
# pytest_configure and wrote a receipt whose pytest_exit != 0 is
# indistinguishable from a genuine red. The "shadow day, then promote" plan was
# unreachable by construction: the shadow host could never record a green.

_GIT_SCOPING_ENV_VARS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_COMMON_DIR",
    "GIT_PREFIX",
)


def _designated_from(repo: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, ...]:
    """`designated_hostnames()` resolved against REPO's committed table.

    A live `git push` exports GIT_DIR/GIT_WORK_TREE into hook children and they
    override both `-C` and cwd for every descendant git call, so they are
    cleared here -- otherwise this would silently read THIS worktree.
    """
    from scripts.hooks.pytest_full_suite_host_guard import designated_hostnames

    for var in _GIT_SCOPING_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(repo)
    return designated_hostnames(env={})


def test_the_conftest_guard_reads_the_same_committed_table_as_the_bash_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo_with_table(tmp_path, TABLE.read_text(encoding="utf-8"), name="shipped")
    assert _designated_from(repo, monkeypatch) == (
        "stickybeatz-studio",
        "omninode-pc",
        "gate-runner-201",
        "stickybeatz",
        "omnibook",
    )


def test_omnibook_can_now_produce_a_green_full_suite_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The end of finding 4, asserted on the exact decision function that
    refused: with h105 authorizing, a full-suite target transplanted to
    omnibook is no longer rejected at pytest_configure, so a dispatch there can
    return a verdict that means something."""
    from scripts.hooks.pytest_full_suite_host_guard import (
        full_suite_host_violation_message,
    )

    repo = _repo_with_table(
        tmp_path, TABLE.read_text(encoding="utf-8"), name="shipped2"
    )
    names = _designated_from(repo, monkeypatch)
    assert (
        full_suite_host_violation_message(
            host="omnibook",
            target_hostname=names[0],
            additional_target_hostnames=names[1:],
            override_authorized=False,
        )
        is None
    )


def test_a_shadow_row_is_still_refused_by_the_conftest_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Promotion is what changed for h105 -- not the rule. A row in `shadow`
    confers no identity on either guard, which is exactly why a shadow host can
    never self-certify its way to `authorizing`."""
    from scripts.hooks.pytest_full_suite_host_guard import (
        full_suite_host_violation_message,
    )

    repo = _repo_with_table(tmp_path, _SYNTHETIC_TABLE, name="synthguard")
    names = _designated_from(repo, monkeypatch)
    assert names == ("hosta", "hostb")
    message = full_suite_host_violation_message(
        host="hosts",
        target_hostname=names[0],
        additional_target_hostnames=names[1:],
        override_authorized=False,
    )
    assert message is not None
    assert "hosts" in message


def test_the_remote_wrapper_restores_a_developer_shell_path() -> None:
    """A non-interactive ssh session gets a minimal PATH -- measured on omnibook
    it is literally ``/usr/bin:/bin:/usr/sbin:/sbin``, with neither the Homebrew
    prefix nor ``~/.local/bin`` on it. The suite shells out to tools by BARE
    NAME (``uv``, ``shellcheck``), so the first full-suite dispatch there
    returned 8 reds, every one a FileNotFoundError for a tool that WAS installed
    on the host. A remote red hard-blocks the push, so PATH parity is what makes
    the verdict mean anything."""
    remote = _remote_wrapper_text()
    assert 'PATH="$(dirname "$UV")' in remote
    assert "/opt/homebrew/bin" in remote
    assert "/usr/local/bin" in remote
    assert "export PATH" in remote
    argv_line = remote.index('"$UV" run pytest')
    assert remote.index("export PATH") < argv_line, (
        "PATH must be set before the suite runs, not after"
    )


def test_the_remote_wrapper_path_covers_linux_hosts_too() -> None:
    """The shipped prefix was macOS-only by construction (OMN-16989):
    ``/opt/homebrew/bin`` has no meaning on a Linux row, and h201 is the fleet's
    only Linux capacity row. Its measured non-interactive PATH omits
    ``~/.local/bin``, where BOTH ``uv`` and ``shellcheck`` live there.

    The Linux analogues are appended AFTER every measured entry, so they can
    only add resolution -- a tool that already resolves keeps resolving to the
    same binary."""
    remote = _remote_wrapper_text()
    line = next(
        ln for ln in remote.splitlines() if ln.startswith('PATH="$(dirname "$UV")')
    )
    # Generic distro prefixes, not anyone's home directory -- but the leaked-
    # literals gate matches `/home/<user>/...`-shaped strings, so they are
    # annotated rather than smuggled past it.
    linuxbrew = "/home/linuxbrew/.linuxbrew/bin"  # onex-allow-local-path OMN-17435 reason="distro-wide Linuxbrew prefix asserted on the remote PATH; not a personal home path"
    for entry in (linuxbrew, "/snap/bin"):
        assert entry in line, f"expected {entry} on the remote PATH"
    assert line.index("${HOME:-}/.local/bin") < line.index(linuxbrew), (
        "the measured entries must keep precedence over the added ones"
    )


def test_the_remote_leg_ships_the_base_ref_so_the_transplant_can_resolve_it() -> None:
    """``git bundle create <b> HEAD`` carries one ref, so the transplanted clone
    has no ``origin/dev`` -- and this suite SUBPROCESSES the hook, which
    resolves ``${PREPUSH_BASE_REF:-origin/dev}`` before anything else. Measured
    on h201 (OMN-16989): the whole
    ``tests/ci/test_prepush_hook_host_identity_guard.py`` behavioral proof
    reduced to ``base ref 'origin/dev' could not be resolved`` -- a red about the
    transplant, not about the tree under test."""
    lib = LIB.read_text(encoding="utf-8")
    cmd = lib[lib.index("./prepush_smart_tests.sh '${rundir}'") :][:400]
    assert "'${base_ref}' '${base_sha}'" in cmd, (
        "the remote command must hand the wrapper the base ref and its sha"
    )
    assert 'base_ref="${BASE_REF:-}"' in lib
    assert 'base_sha="${BASE_SHA:-}"' in lib


def test_the_wrapper_materializes_the_base_ref_in_the_transplanted_tree(
    remote_run_env: dict[str, Path],
) -> None:
    """Behavioral: run the shipped wrapper with a base ref and assert the clone
    resolves it afterwards. BASE_SHA is a merge-base on the origin side, so it
    is always an ancestor of HEAD and its objects are already in the bundle --
    only the ref is missing, and creating it is a local ``update-ref``."""
    head = str(remote_run_env["head"])
    result = _run_wrapper(remote_run_env, extra_argv=["origin/dev", head])
    assert result.returncode == 0, result.stderr
    tree = remote_run_env["rundir"] / "tree"
    resolved = subprocess.run(
        ["git", "rev-parse", "origin/dev"],
        cwd=tree,
        capture_output=True,
        text=True,
        check=False,
    )
    assert resolved.returncode == 0, (
        f"the transplanted tree must resolve origin/dev; got {resolved.stderr!r}"
    )
    assert resolved.stdout.strip() == head


def test_the_wrapper_runs_normally_when_no_base_ref_is_supplied(
    remote_run_env: dict[str, Path],
) -> None:
    """Absent or unresolvable, the base ref is skipped SILENTLY. It may only add
    resolution -- it must never be able to refuse a run, which would turn a
    convenience into a new way to hard-block a push."""
    result = _run_wrapper(remote_run_env, extra_argv=["origin/dev", "0" * 40])
    assert result.returncode == 0, result.stderr
    assert (remote_run_env["rundir"] / "MARKER").is_file()


# =============================================================================
# Off-box by default (OMN-17392)
# =============================================================================
#
# Operator directive 2026-08-31, verbatim: "we should move prepush off this box
# if possible". "This box" is `h200`. Before this change the guard ran the heavy
# suite LOCALLY the moment the local host was a designated capacity row whose
# load probe read under threshold, so the lab was consulted only once this
# machine was already too loaded to be worth consulting it about -- measured
# that day at load1 96.58/24 = 4.02x while h105 sat at 0.12x and h201 at 0.10x.

_TABLE_PREFER_REMOTE = (
    "#label\trole\thostname\tssh_target\tcores\tuv_abs_path\tuv_min_version"
    "\tworkroot\tslot_mode\tslots\trepos_denied\tmode\theavy_local"
    "\tplacement_tier\tnote\n"
    "hp\tcapacity\thostp\tjonah@hostp\t24\t/bin/uv\t0.1.0\t/tmp/wp\tlockdir\t1\t-"
    "\tauthorizing\tprefer_remote\tdefault\tthe box we route off\n"
    "hl\tcapacity\thostl\tjonah@hostl\t24\t/bin/uv\t0.1.0\t/tmp/wl\tlockdir\t1\t-"
    "\tauthorizing\tallowed\tdefault\ta lab host\n"
)


def test_the_local_box_reports_prefer_remote(tmp_path: Path) -> None:
    repo = _repo_with_table(tmp_path, _TABLE_PREFER_REMOTE, name="pr1")
    assert _driver(repo, "prepush_heavy_local_policy hostp").strip() == "prefer_remote"


def test_a_lab_host_reports_allowed(tmp_path: Path) -> None:
    repo = _repo_with_table(tmp_path, _TABLE_PREFER_REMOTE, name="pr2")
    assert _driver(repo, "prepush_heavy_local_policy hostl").strip() == "allowed"


def test_an_unknown_host_has_no_policy_at_all(tmp_path: Path) -> None:
    """Absence must be distinguishable from `allowed`: a host that is in the
    table on no row at all (capacity or identity, OMN-17485) is not "allowed to
    run heavy work here", it is not a designated host, and the caller's own
    not-a-designated-host branch owns it."""
    repo = _repo_with_table(tmp_path, _TABLE_PREFER_REMOTE, name="pr3")
    out = _driver(repo, "prepush_heavy_local_policy nosuchhost || echo NONE")
    assert out.strip() == "NONE"


def test_a_row_predating_the_column_reads_as_allowed(tmp_path: Path) -> None:
    """The column is additive. A 13-column row (the pre-OMN-17392 schema, whose
    field 13 is the free-text note) must read as `allowed` -- the old behavior --
    and must never be parsed into `prefer_remote` by accident."""
    legacy = (
        "#label\trole\thostname\tssh_target\tcores\tuv_abs_path\tuv_min_version"
        "\tworkroot\tslot_mode\tslots\trepos_denied\tmode\tnote\n"
        "hz\tcapacity\thostz\tjonah@hostz\t24\t/bin/uv\t0.1.0\t/tmp/wz\tlockdir\t1\t-"
        "\tauthorizing\tsome free text that is not a policy\n"
    )
    repo = _repo_with_table(tmp_path, legacy, name="pr4")
    assert _driver(repo, "prepush_heavy_local_policy hostz").strip() == "allowed"


def test_prefer_remote_host_is_still_a_placement_target_for_others(
    tmp_path: Path,
) -> None:
    """The half of this change that is easiest to get wrong. `prefer_remote`
    governs only what happens when the row IS the pushing host; it must not
    remove the row from anyone else's candidate list, or flipping the busiest,
    beefiest machine in the lab to prefer_remote would delete it as capacity."""
    repo = _repo_with_table(tmp_path, _TABLE_PREFER_REMOTE, name="pr5")
    body = (
        'export PREPUSH_LOAD_OVERRIDE_MAP="hp=0.10,hl=0.90"\n'
        'export PREPUSH_SLOT_OVERRIDE_MAP="hp=free,hl=free"\n'
        'export PREPUSH_UV_OVERRIDE_MAP="hp=0.9.9,hl=0.9.9"\n'
        "if pick_capacity_host hostl synth; then\n"
        '  echo "PICK=$PREPUSH_PICK_LABEL"\n'
        "else\n"
        '  echo "PICK=none"\n'
        "fi\n"
    )
    assert "PICK=hp" in _driver(repo, body)


_TABLE_IDENTITY_POLICY = (
    "#label\trole\thostname\tssh_target\tcores\tuv_abs_path\tuv_min_version"
    "\tworkroot\tslot_mode\tslots\trepos_denied\tmode\theavy_local"
    "\tplacement_tier\tnote\n"
    "hc\tidentity\tcontainerhost\t-\t32\t-\t-\t-\tnone\t1\t-\tauthorizing"
    "\tprefer_remote\t-\tan executing container identity\n"
    "hl\tcapacity\thostl\tjonah@hostl\t24\t/bin/uv\t0.1.0\t/tmp/wl\tlockdir\t1\t-"
    "\tauthorizing\tallowed\tdefault\ta lab host\n"
)


def test_an_identity_row_carries_a_heavy_local_policy_too(tmp_path: Path) -> None:
    """The gate-runner container (h201c) is identity-only as a placement
    TARGET, but it is the LOCAL host of every in-container push (OMN-17485).
    A policy function that reads capacity rows only would silently hand every
    in-container escalation the `allowed` default -- exactly the origination
    surface the .201 demotion exists to close."""
    repo = _repo_with_table(tmp_path, _TABLE_IDENTITY_POLICY, name="idp1")
    out = _driver(repo, "prepush_heavy_local_policy containerhost")
    assert out.strip() == "prefer_remote"


# =============================================================================
# placement_tier: a last_resort host can never outrank a fit default host
# (OMN-17485)
# =============================================================================
# `.201` hosts the dev runtime lane -- the live evidence surface the OMN-16963
# AC5 terminalization measurement reads from -- and the interactive
# collaborator lane. Measured 2026-09-01: its gate-runner slot ran heavy
# suites back-to-back 08:31Z-12:02Z while a collaborator's own governed full
# core suite (44464 tests, 2h58m) ran host-side concurrently. The demotion is
# a RANKING rule, deliberately not an exclusion: a heavy escalation with
# nowhere else to go still lands there rather than dying, and the fit record
# says so out loud.

_TABLE_TIERED = (
    "#label\trole\thostname\tssh_target\tcores\tuv_abs_path\tuv_min_version"
    "\tworkroot\tslot_mode\tslots\trepos_denied\tmode\theavy_local"
    "\tplacement_tier\tnote\n"
    "hd1\tcapacity\thostd1\tjonah@hostd1\t24\t/bin/uv\t0.1.0\t/tmp/w1\tlockdir\t1\t-"
    "\tauthorizing\tallowed\tdefault\tbusier default host\n"
    "hd2\tcapacity\thostd2\tjonah@hostd2\t24\t/bin/uv\t0.1.0\t/tmp/w2\tlockdir\t1\t-"
    "\tauthorizing\tallowed\tdefault\tidler default host\n"
    "hlr\tcapacity\thostlr\tjonah@hostlr\t32\t/bin/uv\t0.1.0\t/tmp/w3\tlockdir\t1\t-"
    "\tauthorizing\tprefer_remote\tlast_resort\tidlest host in the lab, demoted\n"
)

_TIER_ENV = (
    'export PREPUSH_SLOT_OVERRIDE_MAP="hd1=free,hd2=free,hlr=free"\n'
    'export PREPUSH_UV_OVERRIDE_MAP="hd1=1.0.0,hd2=1.0.0,hlr=1.0.0"\n'
)


def test_a_fit_default_host_outranks_an_idler_last_resort_host(
    tmp_path: Path,
) -> None:
    """The load-only ranking this replaces would pick hlr at 0.05x every time.
    Tier is the major key: however idle the demoted host is, a fit default
    host wins."""
    repo = _repo_with_table(tmp_path, _TABLE_TIERED, name="tier1")
    out = _driver(
        repo,
        'export PREPUSH_LOAD_OVERRIDE_MAP="hd1=0.90,hd2=0.60,hlr=0.05"\n'
        + _TIER_ENV
        + "if pick_capacity_host somewhere-else omnibase_core; then\n"
        '  echo "PICK=$PREPUSH_PICK_LABEL"\n'
        "else\n"
        '  echo "PICK=none"\n'
        "fi\n"
        'echo "PROBE=$PREPUSH_PROBE_LOG"\n',
    )
    assert "PICK=hd2" in out, out
    assert "tier=last_resort" in out, (
        "the demoted host's fit record must carry its tier so the pass-over "
        f"is auditable, got: {out}"
    )


def test_the_last_resort_host_is_still_reachable_when_nothing_else_is_fit(
    tmp_path: Path,
) -> None:
    """Demotion is a ranking rule, not an exclusion. When every default-tier
    slot is held, the escalation lands on the demoted host rather than
    refusing a push another authorizing host could have cleared."""
    repo = _repo_with_table(tmp_path, _TABLE_TIERED, name="tier2")
    out = _driver(
        repo,
        'export PREPUSH_LOAD_OVERRIDE_MAP="hd1=0.90,hd2=0.60,hlr=0.05"\n'
        'export PREPUSH_SLOT_OVERRIDE_MAP="hd1=busy,hd2=busy,hlr=free"\n'
        'export PREPUSH_UV_OVERRIDE_MAP="hd1=1.0.0,hd2=1.0.0,hlr=1.0.0"\n'
        "if pick_capacity_host somewhere-else omnibase_core; then\n"
        '  echo "PICK=$PREPUSH_PICK_LABEL"\n'
        "else\n"
        '  echo "PICK=none"\n'
        "fi\n",
    )
    assert "PICK=hlr" in out, out


def test_the_ranked_list_puts_every_default_host_before_the_last_resort_host(
    tmp_path: Path,
) -> None:
    """Not just the winner: the walk order itself is tier-major, so a default
    host that fails to answer costs the OTHER default host next, and the
    demoted host only after both."""
    repo = _repo_with_table(tmp_path, _TABLE_TIERED, name="tier3")
    out = _driver(
        repo,
        'export PREPUSH_LOAD_OVERRIDE_MAP="hd1=0.90,hd2=0.60,hlr=0.05"\n'
        + _TIER_ENV
        + "pick_capacity_host somewhere-else omnibase_core > /dev/null 2>&1\n"
        'echo "COUNT=$(prepush_candidate_count)"\n'
        'prepush_select_candidate 1 && echo "FIRST=$PREPUSH_PICK_LABEL"\n'
        'prepush_select_candidate 2 && echo "SECOND=$PREPUSH_PICK_LABEL"\n'
        'prepush_select_candidate 3 && echo "THIRD=$PREPUSH_PICK_LABEL"\n',
    )
    assert "COUNT=3" in out, out
    assert "FIRST=hd2" in out, out
    assert "SECOND=hd1" in out, out
    assert "THIRD=hlr" in out, out


def test_a_row_predating_the_tier_column_ranks_as_default(tmp_path: Path) -> None:
    """The column is additive, in both directions of trouble: a 14-column row
    (the pre-OMN-17485 schema, whose field 14 is the free-text note) must rank
    as `default` -- never be demoted by its own note text -- and must still
    outrank an explicit `last_resort` row."""
    mixed = (
        "#label\trole\thostname\tssh_target\tcores\tuv_abs_path\tuv_min_version"
        "\tworkroot\tslot_mode\tslots\trepos_denied\tmode\theavy_local\tnote\n"
        "hy\tcapacity\thosty\tjonah@hosty\t24\t/bin/uv\t0.1.0\t/tmp/wy\tlockdir\t1\t-"
        "\tauthorizing\tallowed\tlegacy row, note in field 14\n"
        "hlr\tcapacity\thostlr\tjonah@hostlr\t32\t/bin/uv\t0.1.0\t/tmp/w3\tlockdir\t1\t-"
        "\tauthorizing\tprefer_remote\tlast_resort\tidlest, demoted\n"
    )
    repo = _repo_with_table(tmp_path, mixed, name="tier4")
    out = _driver(
        repo,
        'export PREPUSH_LOAD_OVERRIDE_MAP="hy=0.90,hlr=0.05"\n'
        'export PREPUSH_SLOT_OVERRIDE_MAP="hy=free,hlr=free"\n'
        'export PREPUSH_UV_OVERRIDE_MAP="hy=1.0.0,hlr=1.0.0"\n'
        "if pick_capacity_host somewhere-else omnibase_core; then\n"
        '  echo "PICK=$PREPUSH_PICK_LABEL"\n'
        "else\n"
        '  echo "PICK=none"\n'
        "fi\n",
    )
    assert "PICK=hy" in out, out


def test_the_guard_consults_the_policy_before_running_locally() -> None:
    """The behavioral core, asserted against the hook's real control flow: the
    designated-host branch must read the policy and, on `prefer_remote`, reach
    the lab legs BEFORE `prepush_try_local_heavy_slot`.

    Ordering is the whole ticket. A version that consulted the policy but still
    tried the local slot first would pass a naive "is prefer_remote mentioned"
    check while changing nothing at all.
    """
    text = HOOK.read_text(encoding="utf-8")
    guard = text[text.index("guard_full_suite_host() {") :]
    guard = guard[: guard.index("\n  # Not a designated host.")]
    assert "prepush_heavy_local_policy" in guard
    branch = guard[guard.index('if [ "$policy" = "prefer_remote" ]; then') :]
    branch = branch[: branch.index("\n    else\n")]
    for expected in (
        # omnibase_infra's branch also reaches `remote_full_suite_verified`
        # first (OMN-16688). That rung is not ported to this repo -- see
        # test_the_unported_github_verify_rung_is_absent_not_stubbed -- so it is
        # deliberately absent from this list rather than silently expected.
        "prepush_wait_for_lab_capacity",
        "prepush_try_local_heavy_slot",
    ):
        assert expected in branch, f"prefer_remote branch never reaches {expected}"
    assert branch.index("prepush_wait_for_lab_capacity") < branch.index(
        "prepush_try_local_heavy_slot"
    ), "the off-box wait must be spent BEFORE the local slot is even attempted"


def test_the_off_box_budget_is_a_constant_not_an_env_override() -> None:
    """The directive is explicit that PREPUSH_* overrides stay forbidden, and a
    `${PREPUSH_OFFBOX_WAIT_BUDGET_SECONDS:-900}` would be a one-word bypass of
    this entire policy: setting it to 0 collapses the wait and lands every push
    straight on the local fallback."""
    text = HOOK.read_text(encoding="utf-8")
    for name in (
        "PREPUSH_OFFBOX_WAIT_BUDGET_SECONDS",
        "PREPUSH_OFFBOX_WAIT_INTERVAL_SECONDS",
        "PREPUSH_MIN_FREE_MEM_MB",
    ):
        assert f"{name}=${{{name}:-" not in text, (
            f"{name} is env-overridable, which makes the gate it guards optional"
        )
        assert re.search(rf"^{name}=[0-9]+$", text, re.M), (
            f"{name} must be a literal constant assignment"
        )


def test_the_local_fallback_still_requires_measured_capacity() -> None:
    """The fallback must not be a way to run a heavy suite on a box that is over
    threshold. It calls the SAME `prepush_try_local_heavy_slot` the `allowed`
    path calls, so an unfit host refuses exactly as it did before this change --
    the new path is strictly narrower than the one it replaces, never wider."""
    text = HOOK.read_text(encoding="utf-8")
    guard = text[text.index("guard_full_suite_host() {") :]
    branch = guard[guard.index('if [ "$policy" = "prefer_remote" ]; then') :]
    branch = branch[: branch.index("\n    else\n")]
    assert "if prepush_try_local_heavy_slot; then" in branch
    assert "PREPUSH_ALLOW_LOCAL_FULL_SUITE" not in branch
    assert "consume_override_grant" not in branch, (
        "the prefer_remote branch must fall through to the shared refusal ladder "
        "rather than minting its own grant"
    )


def test_the_local_fallback_is_loud() -> None:
    """'never silently' is the operator's word. The fallback has to say it is
    running on the box, that off-box was tried first, and what it probed."""
    text = HOOK.read_text(encoding="utf-8")
    guard = text[text.index("guard_full_suite_host() {") :]
    branch = guard[guard.index('if [ "$policy" = "prefer_remote" ]; then') :]
    branch = branch[: branch.index("\n    else\n")]
    assert "LOCAL FALLBACK IN EFFECT" in branch
    assert "PREPUSH_PROBE_LOG" in branch
    assert "NOT a bypass" in branch


def test_the_bounded_wait_retries_then_gives_up(table_repo: Path) -> None:
    """Executed, not grepped: the wait loop must re-attempt placement and then
    return non-zero when the budget is spent, rather than looping forever (a
    hung pre-push is indistinguishable from a broken one)."""
    body = (
        "attempts=0\n"
        "dispatch_to_lab_host() { attempts=$((attempts + 1));"
        ' PREPUSH_PROBE_LOG="h201=busy(queue=0 heavy_pids=2)"; return 1; }\n'
        "sleep() { :; }\n"
        "rc=0\n"
        'prepush_wait_for_lab_capacity "heavy thing" 3 1 || rc=$?\n'
        'echo "RC=$rc ATTEMPTS=$attempts"\n'
    )
    out = _driver_both(
        table_repo,
        _hook_func("prepush_lab_has_transient_capacity")
        + _hook_func("prepush_wait_for_lab_capacity")
        + body,
    )
    assert "RC=1" in out, out
    # budget 3 / interval 1 -> attempts at waited=0,1,2,3 then break.
    assert "ATTEMPTS=4" in out, out
    assert "OFF-BOX QUEUE-AND-WAIT" in out
    assert "budget exhausted" in out


def test_the_bounded_wait_returns_the_moment_a_host_takes_it(
    table_repo: Path,
) -> None:
    """A lab host freeing up mid-wait must end the wait immediately -- the point
    of re-probing is to catch exactly that."""
    body = (
        "attempts=0\n"
        "dispatch_to_lab_host() { attempts=$((attempts + 1));"
        ' PREPUSH_PROBE_LOG="h201=busy(queue=0 heavy_pids=2)";'
        ' [ "$attempts" -ge 2 ] && return 0; return 1; }\n'
        "sleep() { :; }\n"
        "rc=0\n"
        'prepush_wait_for_lab_capacity "heavy thing" 600 1 || rc=$?\n'
        'echo "RC=$rc ATTEMPTS=$attempts"\n'
    )
    out = _driver_both(
        table_repo,
        _hook_func("prepush_lab_has_transient_capacity")
        + _hook_func("prepush_wait_for_lab_capacity")
        + body,
    )
    assert "RC=0 ATTEMPTS=2" in out, out


# =============================================================================
# Memory-aware placement (OMN-17392 / the OMN-17271 memory dimension)
# =============================================================================
#
# Measured live on 2026-08-31, one second apart, and the reason this dimension
# exists: the `.201` HOST and the gate-runner CONTAINER running on it both
# reported load 3.27/32 = 0.10x -- the fittest ratio in the lab -- while their
# available memory differed 19-fold (49771 MiB vs 2562 MiB, the container
# sitting at 5.9 GiB of an 8 GiB cgroup cap). A CPU-only picker cannot tell
# those apart, which is how it kept ranking a saturated, OOM-killing target
# first (OMN-17247, OMN-17316).


def test_a_memory_starved_host_is_unfit_even_at_zero_load(table_repo: Path) -> None:
    """The exact shape of the measured defect: idlest box in the lab, no memory."""
    out = _pick(
        table_repo,
        load="h200=0.90,h201=0.10,h105=0.80",
        slot=_ALL_FREE,
        uv=_GOOD_UV,
        mem="h200=40000,h201=2562,h105=14664",
    )
    assert "PICK=h105" in out, out
    assert "h201=mem-over(2562MiB<4096)" in out, out


def test_a_host_whose_memory_cannot_be_read_is_skipped_not_assumed_ample(
    table_repo: Path,
) -> None:
    """Silence is not headroom -- the same fail-closed rule `unreachable` and
    `slot-unknown` already carry. `-1` is what the probe emits when neither
    /proc/meminfo nor vm_stat could be read."""
    out = _pick(
        table_repo,
        load="h200=0.90,h201=0.10,h105=0.80",
        slot=_ALL_FREE,
        uv=_GOOD_UV,
        mem="h200=40000,h201=-1,h105=14664",
    )
    assert "PICK=h105" in out, out
    assert "h201=mem-unknown" in out, out


def test_memory_is_ranked_after_load_but_admits_independently(
    table_repo: Path,
) -> None:
    """load1 RANKS; memory ADMITS. The least-loaded host still wins -- unless it
    cannot prove memory, in which case the next one does, rather than the pick
    failing outright."""
    out = _pick(
        table_repo,
        load="h200=0.20,h201=0.10,h105=0.30",
        slot=_ALL_FREE,
        uv=_GOOD_UV,
        mem="h200=1000,h201=1000,h105=14664",
    )
    assert "PICK=h105" in out, out


def test_every_candidate_failing_memory_yields_no_pick(table_repo: Path) -> None:
    """Fail closed: if nothing in the lab can prove headroom, the picker returns
    nothing and the caller refuses -- it never falls back to the least-bad."""
    out = _pick(
        table_repo,
        load="h200=0.10,h201=0.10,h101=0.10,h105=0.10",
        slot=_ALL_FREE,
        uv=_GOOD_UV,
        mem="h200=100,h201=100,h101=100,h105=100",
    )
    assert "PICK=none" in out, out


def test_the_fit_record_carries_the_memory_it_decided_on(table_repo: Path) -> None:
    """OMN-17271 item 4, evidence-carrying routing: the probe trail that lands in
    the receipt and the refusal message records the MEASUREMENT, not just the
    verdict, so a placement can be audited rather than believed."""
    out = _pick(
        table_repo,
        load="h200=0.90,h201=0.44,h105=0.21",
        slot=_ALL_FREE,
        uv=_GOOD_UV,
        mem="h200=40000,h201=49771,h105=14664",
    )
    assert "h105=fit(0.21,authorizing,14664MiB)" in out, out


def test_the_probe_snippet_reads_cgroup_limits_not_just_machine_memory() -> None:
    """The gate-runner OOM (OMN-17247) is invisible to a MemAvailable read: the
    HOST had 49 GiB free while the capped container it would have run in had
    2.5 GiB of its 8 GiB cap. Both cgroup generations are read."""
    text = HOOK.read_text(encoding="utf-8")
    probe = text[text.index("_PREPUSH_LOAD_PROBE_SH='") :]
    probe = probe[: probe.index("'\n")]
    assert "/sys/fs/cgroup/memory.max" in probe
    assert "/sys/fs/cgroup/memory.current" in probe
    assert "/sys/fs/cgroup/memory/memory.limit_in_bytes" in probe
    assert "MemAvailable" in probe
    assert "vm_stat" in probe, "the macOS lab hosts need a memory path too"


def test_the_probe_snippet_carries_no_single_quotes() -> None:
    """Load-bearing, not style: the snippet is itself a single-quoted assignment
    and is handed to ssh(1) for a remote login shell to execute. One single
    quote inside it truncates the assignment and every remote probe silently
    stops working."""
    text = HOOK.read_text(encoding="utf-8")
    probe = text[
        text.index("_PREPUSH_LOAD_PROBE_SH='") + len("_PREPUSH_LOAD_PROBE_SH='") :
    ]
    probe = probe[: probe.index("'")]
    assert "MemAvailable" in probe, "sanity: the whole snippet must be in view"
    assert "'" not in probe


def test_host_is_fit_refuses_a_host_that_is_idle_but_out_of_memory(
    table_repo: Path,
) -> None:
    """host_is_fit is what the LOCAL branch calls, so the memory floor has to
    apply to this box too -- otherwise the machine we are routing work off is
    the one machine exempt from the check."""
    body = (
        'export PREPUSH_LOAD_OVERRIDE_LOCAL="0.10 24 1000"\n'
        "rc=0\n"
        'host_is_fit "" || rc=$?\n'
        'echo "RC=$rc DETAIL=$PREPUSH_LAST_FIT_DETAIL"\n'
    )
    out = _driver_both(table_repo, _with_real_load() + body)
    assert "RC=1" in out, out
    assert "mem 1000MiB < 4096MiB" in out, out


def test_host_is_fit_accepts_a_host_that_proves_both_dimensions(
    table_repo: Path,
) -> None:
    body = (
        'export PREPUSH_LOAD_OVERRIDE_LOCAL="0.10 24 40000"\n'
        "rc=0\n"
        'host_is_fit "" || rc=$?\n'
        'echo "RC=$rc DETAIL=$PREPUSH_LAST_FIT_DETAIL"\n'
    )
    out = _driver_both(table_repo, _with_real_load() + body)
    assert "RC=0" in out, out
    assert "mem 40000MiB" in out, out


def test_host_is_fit_reports_unreadable_memory_as_could_not_check(
    table_repo: Path,
) -> None:
    """rc 2 must stay distinguishable from rc 1: "could not check" and "over
    capacity" lead to different operator actions, and the callers are documented
    as never conflating them."""
    body = (
        'export PREPUSH_LOAD_OVERRIDE_LOCAL="0.10 24 -1"\n'
        "rc=0\n"
        'host_is_fit "" || rc=$?\n'
        'echo "RC=$rc DETAIL=$PREPUSH_LAST_FIT_DETAIL"\n'
    )
    out = _driver_both(table_repo, _with_real_load() + body)
    assert "RC=2" in out, out
    assert "memory unreadable" in out, out


# =============================================================================
# The wait budget is spent only on refusals that can resolve themselves
# =============================================================================
#
# The bounded wait exists to catch a lab slot freeing up. That premise holds
# for a host that is BUSY, over on load, or over on memory -- all three drain
# on their own. It does NOT hold for a host that is unreachable, repo-denied,
# disabled, or below the uv floor: none of those change because we waited, so
# spending the budget on them buys nothing and costs the pusher the full
# 900s before the local fallback it was always going to reach.
#
# The case that makes this concrete is a Mac off the lab LAN: every remote row
# probes `unreachable`, and without this gate EVERY heavy push pays 15 minutes
# of silence before running locally anyway. That is a daily-friction
# regression, not a safety property -- skipping a wait that cannot succeed
# never runs a suite that would otherwise have been refused, because the local
# fallback still has to prove measured capacity AND an exclusive slot.


def _wait_driver(probe_log: str, budget: str = "3", interval: str = "1") -> str:
    """Drive the real wait loop with a stubbed dispatch that always misses and
    reports PROBE_LOG, so only the transient/structural decision is under test."""
    return (
        "attempts=0\n"
        "dispatch_to_lab_host() { attempts=$((attempts + 1));"
        f' PREPUSH_PROBE_LOG="{probe_log}"; return 1; }}\n'
        "sleep() { :; }\n"
        "rc=0\n"
        f'prepush_wait_for_lab_capacity "heavy thing" {budget} {interval} || rc=$?\n'
        'echo "RC=$rc ATTEMPTS=$attempts"\n'
    )


def test_the_wait_is_skipped_when_every_host_is_structurally_unavailable(
    table_repo: Path,
) -> None:
    """All four rows unreachable -- the lab is gone, not busy. One attempt, no
    wait, straight to the caller's fallback ladder."""
    out = _driver_both(
        table_repo,
        _hook_func("prepush_lab_has_transient_capacity")
        + _hook_func("prepush_wait_for_lab_capacity")
        + _wait_driver("h200=unreachable h201=unreachable h105=unreachable"),
    )
    assert "RC=1" in out, out
    assert "ATTEMPTS=1" in out, out
    assert "budget exhausted" not in out, out
    assert "cannot resolve on its own" in out, out


def test_the_wait_is_spent_when_a_host_is_merely_busy(table_repo: Path) -> None:
    """A held slot is exactly what the wait is for: it drains. Budget 3 /
    interval 1 -> attempts at waited=0,1,2,3."""
    out = _driver_both(
        table_repo,
        _hook_func("prepush_lab_has_transient_capacity")
        + _hook_func("prepush_wait_for_lab_capacity")
        + _wait_driver(
            "h200=unreachable h201=busy(queue=0 heavy_pids=2) h105=unreachable"
        ),
    )
    assert "RC=1" in out, out
    assert "ATTEMPTS=4" in out, out
    assert "budget exhausted" in out, out


def test_a_memory_starved_host_is_worth_waiting_for(table_repo: Path) -> None:
    """`mem-over` is the OMN-17247 container mid-suite. It drains when the suite
    holding the memory finishes, so it earns the wait exactly like `busy`."""
    out = _driver_both(
        table_repo,
        _hook_func("prepush_lab_has_transient_capacity")
        + _hook_func("prepush_wait_for_lab_capacity")
        + _wait_driver("h201=mem-over(2562MiB<4096) h105=unreachable"),
    )
    assert "ATTEMPTS=4" in out, out


def test_an_overloaded_host_is_worth_waiting_for(table_repo: Path) -> None:
    """Load drains too -- it is the original reason the lab is ever refused."""
    out = _driver_both(
        table_repo,
        _hook_func("prepush_lab_has_transient_capacity")
        + _hook_func("prepush_wait_for_lab_capacity")
        + _wait_driver("h201=over(2.400) h105=uv-unfit(0.8.3<0.11.0)"),
    )
    assert "ATTEMPTS=4" in out, out


def test_a_uv_unfit_or_repo_denied_lab_is_not_worth_waiting_for(
    table_repo: Path,
) -> None:
    """Neither an old uv nor a repo denial changes while a pusher waits. The
    negative control for the two tests above: same loop, same stub, only the
    probe-log reason differs, and that alone must decide."""
    out = _driver_both(
        table_repo,
        _hook_func("prepush_lab_has_transient_capacity")
        + _hook_func("prepush_wait_for_lab_capacity")
        + _wait_driver("h201=repo-denied h105=uv-unfit(0.8.3<0.11.0) h101=disabled"),
    )
    assert "ATTEMPTS=1" in out, out
    assert "cannot resolve on its own" in out, out


def test_skipping_the_wait_still_returns_no_placement(table_repo: Path) -> None:
    """The skip must return 1 (no evidence), never 0. Returning 0 would tell the
    caller a lab host had run the suite when none did -- the one way this
    optimisation could become a bypass."""
    out = _driver_both(
        table_repo,
        _hook_func("prepush_lab_has_transient_capacity")
        + _hook_func("prepush_wait_for_lab_capacity")
        + _wait_driver("h200=unreachable"),
    )
    assert "RC=1" in out, out


# =============================================================================
# The local refusal names the dimension that actually refused
# =============================================================================


def test_the_allowed_path_refusal_names_the_measured_reason() -> None:
    """Before OMN-17392 this log line sat inside `if host_is_fit ""`, so "this
    host is fit but its slot is held" was always true when it printed. The
    refactor to prepush_try_local_heavy_slot moved it out of that branch, where
    it now also fires for an over-loaded or memory-starved host and asserts
    something measurably false. A refusal that misnames its own cause sends the
    reader hunting for a held lock that does not exist."""
    text = HOOK.read_text(encoding="utf-8")
    start = text.index("guard_full_suite_host() {")
    guard = text[start:]
    assert "this host is fit but its heavy-suite slot is already held" not in guard, (
        "the local-refusal log still hardcodes 'fit but slot held', which is "
        "false whenever the host was refused for load or memory"
    )
    assert "PREPUSH_LOCAL_HEAVY_REASON" in guard, (
        "expected the refusal to report the reason prepush_try_local_heavy_slot "
        "actually measured"
    )


def test_try_local_heavy_slot_records_which_dimension_refused() -> None:
    """The reason must come from the measurement, not from the call site's
    guess about why it failed."""
    text = HOOK.read_text(encoding="utf-8")
    start = text.index("prepush_try_local_heavy_slot() {")
    body = text[start : text.index("\n}\n", start)]
    assert "PREPUSH_LOCAL_HEAVY_REASON" in body
    assert "PREPUSH_LAST_FIT_DETAIL" in body, (
        "the unfit branch must carry the load/memory detail host_is_fit measured"
    )


# =============================================================================
# Vendored-copy integrity and provenance (OMN-17435)
# =============================================================================
#
# WHY VENDORED AT ALL. OMN-17435 recommends "one shared surface" over a third
# hand-copied clone, and the recommendation is right about the risk. It is not
# right about the remedy that is available here: every shared-surface mechanism
# makes this FAIL-CLOSED gate depend on something an isolated clone may not
# have. A sibling-clone `source` needs omnibase_infra checked out next to this
# repo; a submodule needs `--recurse-submodules`; a pip-installed picker needs a
# synced `.venv` the hook runs BEFORE. In every case a missing dependency turns
# a placement optimisation into a hard push failure. So the copy stays local and
# the DRIFT is what gets mechanically controlled.
#
# WHAT IS CONTROLLED, and why a digest alone was not enough. omnibase_core's
# OMN-17159 pins only a sha256, which detects a LOCAL edit but says nothing
# about whether the copy is STALE. Measured 2026-09-01, that gap is live: core's
# pin (067706bb...c096) is omnibase_infra commit 81955c0d0 (OMN-17269), while
# omnibase_infra dev already shipped 3e1c975b0 (OMN-17392: memory-aware
# admission, prefer_remote, bounded off-box wait). Core's vendored picker was a
# whole feature behind before its own PR merged. This repo therefore records the
# upstream COMMIT alongside the digest, in scripts/hooks/prepush_vendored.tsv,
# so "has upstream moved?" is answerable by anyone with network access without
# this repo needing that access at push time.

VENDORED = REPO_ROOT / "scripts" / "hooks" / "prepush_vendored.tsv"


def _vendored_rows() -> list[list[str]]:
    rows = []
    for line in VENDORED.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0]
        if not line.strip():
            continue
        rows.append(line.split("\t"))
    return rows


def test_every_vendored_file_matches_its_recorded_digest() -> None:
    """The shipped bytes and the provenance record cannot disagree.

    Updating a vendored file is a legitimate, expected operation -- re-copy from
    the named upstream path, update BOTH the sha256 and the upstream_commit in
    the same commit, and name the upstream revision in the PR body. Editing the
    file WITHOUT touching the record is the thing that cannot happen quietly.
    """
    import hashlib

    rows = _vendored_rows()
    assert rows, f"expected at least one provenance row in {VENDORED}"
    for row in rows:
        assert len(row) == 7, f"malformed provenance row: {row!r}"
        rel, _repo, _upath, commit, _branch, digest, _copied = row
        target = REPO_ROOT / rel
        assert target.is_file(), f"provenance names a missing file: {rel}"
        actual = hashlib.sha256(target.read_bytes()).hexdigest()
        assert actual == digest, (
            f"{rel} has diverged from its recorded upstream copy "
            f"({_repo}@{commit}:{_upath}). If the change is intentional, update "
            f"the sha256 in {VENDORED.name} in the same commit and say which "
            "upstream revision it now tracks; if it is not, restore the file"
        )
        assert len(commit) == 40, (
            f"upstream_commit for {rel} is not a full 40-char sha: {commit!r}"
        )
        assert all(c in "0123456789abcdef" for c in commit), (
            f"upstream_commit for {rel} is not lowercase hex: {commit!r}"
        )


def test_the_picker_library_is_covered_by_the_provenance_record() -> None:
    """The picker specifically -- not merely "some file" -- must be pinned.

    A provenance file that happened to list only the two Python helpers would
    leave the fail-closed placement logic itself unguarded, which is the one
    file whose silent fork actually changes where a suite runs.
    """
    covered = {row[0] for row in _vendored_rows()}
    assert "scripts/hooks/prepush_dispatch.sh" in covered
    assert "scripts/hooks/prepush_override_grant.py" in covered
    assert "scripts/hooks/pytest_full_suite_host_guard.py" in covered


def test_the_picker_library_is_not_edited_into_a_repo_specific_fork() -> None:
    """The copied library must carry no repo-local branching.

    A conditional on the repo name inside the shared picker is how "one
    mechanism, three repos" becomes three mechanisms wearing one filename. The
    supported seam for per-repo behavior is the host TABLE (repos_denied, mode,
    slots, heavy_local) and the CALLER's argv, both of which are data this
    library reads -- never a branch compiled into the library itself.
    """
    text = LIB.read_text(encoding="utf-8")
    for token in ("omnimarket", "omnibase_core"):
        assert token not in text, (
            f"the shared picker library names {token!r}; per-repo behavior "
            "belongs in prepush_hosts.tsv or the caller's argv, not in a "
            "branch inside the shared file"
        )
