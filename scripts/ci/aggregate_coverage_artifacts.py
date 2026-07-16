#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shadow coverage-artifact aggregator (OMN-14680 / merge-flow WS2).

Eliminate the second full ``pytest --cov`` pass. The ``test`` matrix already
runs the whole suite (split across shards); this aggregator CONSUMES the
per-shard versioned coverage artifacts those shards now emit, combines them
ONCE into a single ``coverage.json``, dispatches ``NodeCoverageSweep`` against
that combined artifact, and (optionally) compares the combined totals against
the authoritative second-pass ``coverage.json`` for shadow parity.

SHADOW ONLY (do not cut over yet). This path is NOT wired into the required
``CI Summary``. The authoritative ``coverage-sweep-gate`` job — which still
runs its own full ``pytest --cov`` — remains the merge-blocking census until
parity is proven across enough real PRs to cut over. Per the merge-flow plan
§2 WS2 rollback: "Restore the existing sweep as required if comparison
diverges; do not waive coverage." This script is therefore free to fail LOUD
without wedging merges: a red shadow job is a *signal not to cut over*, never a
merge block.

Fail-closed invariants (a missing/stale/malformed/wrong-head artifact is NEVER
a silent pass — mirrors ``run_coverage_sweep_gate.py`` and
``reference_ci_gate_enforcement_mechanics``):

  * Every shard's coverage artifact must be bound to the *expected head SHA*.
    An artifact bound to another head is stale-by-construction and rejected
    (``ARTIFACT_WRONG_HEAD``).
  * Every expected split (1..split_count) must be present, with both its
    metadata sidecar and its ``.coverage.<split>`` data file
    (``ARTIFACT_MISSING``).
  * Metadata must parse and carry the pinned schema version
    (``ARTIFACT_MALFORMED`` / ``ARTIFACT_SCHEMA_MISMATCH``).
  * With ``--max-age-seconds``, an artifact older than the window is rejected
    (``ARTIFACT_STALE``) even if head-bound — defence in depth.

Parity semantics (one-sided, justified). The shards execute
``@pytest.mark.integration`` real-DB tests under a provisioned Postgres
service that the authoritative gate does NOT provision (so the gate self-skips
them). The combined shard census is therefore a *superset* of the authoritative
census, so exact equality is not the right invariant. The parity gate asserts
the shadow census does not LOSE coverage versus authoritative:

    shadow_percent >= authoritative_percent - TOLERANCE

with a named ``--tolerance`` (default 0.5 percentage points) absorbing
per-process import-time execution jitter and rounding. Over-coverage (shadow
higher, because integration tests ran) is expected and PASSES. Parity is only
*gated* when the shards ran the FULL suite (``test_scope == "full"``); under
smart selection the combined census is a deliberate subset, so parity is
reported INFORMATIONALLY and never gates.

Exit codes (reason-coded so a red shadow job is diagnosable at a glance):
  0 — validated, combined once, swept (clean/gaps_found), parity ok or
      informational.
  3 — ARTIFACT validation failed (missing/wrong-head/malformed/stale):
      fail-closed.
  4 — combine/json generation engine failure (or timeout: orphaned child
      process group reaped).
  5 — sweep reported status="error" (no usable census produced).
  6 — parity divergence beyond tolerance on a FULL-scope run (shadow lost
      coverage vs authoritative): do not cut over.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

# Add src/ to path so omnimarket imports work in CI without editable install.
_REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

from omnimarket.nodes.node_coverage_sweep.handlers.handler_coverage_sweep import (  # noqa: E402
    CoverageSweepRequest,
    NodeCoverageSweep,
)

SCHEMA_VERSION = "omnimarket.coverage-artifact/v1"

# Reason codes (stable strings; asserted by tests and grep-able in CI logs).
REASON_MISSING = "ARTIFACT_MISSING"
REASON_WRONG_HEAD = "ARTIFACT_WRONG_HEAD"
REASON_MALFORMED = "ARTIFACT_MALFORMED"
REASON_SCHEMA_MISMATCH = "ARTIFACT_SCHEMA_MISMATCH"
REASON_STALE = "ARTIFACT_STALE"

# Exit codes.
EXIT_OK = 0
EXIT_ARTIFACT = 3
EXIT_ENGINE = 4
EXIT_SWEEP_ERROR = 5
EXIT_PARITY = 6


class ArtifactValidationError(Exception):
    """A fail-closed rejection of the shard artifact set.

    Carries a stable ``reason`` code so CI logs (and tests) can distinguish
    the failure classes the ticket enumerates: wrong-head, missing, malformed,
    stale.
    """

    def __init__(self, reason: str, message: str) -> None:
        self.reason = reason
        self.message = message
        super().__init__(f"{reason}: {message}")


# ---------------------------------------------------------------------------
# Bounded child-process execution (timeout + heartbeat + process-group reap)
# ---------------------------------------------------------------------------
def _reap_process_group(proc: subprocess.Popen[bytes]) -> None:
    """Best-effort SIGTERM then SIGKILL of the child's ENTIRE process group.

    Mirrors ``run_coverage_sweep_gate.py``: the child is started in its own
    session (``start_new_session=True``) so a timeout reaps the whole tree, not
    just the direct child — an orphaned ``coverage`` subprocess is a
    runner-slot-hold vector. Purely best-effort; every lookup/signal is guarded
    because the group may already be gone.
    """
    if os.name != "posix":
        proc.kill()
        return
    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, OSError):
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, OSError):
            return
        try:
            proc.wait(timeout=5)
            return
        except subprocess.TimeoutExpired:
            continue


def run_bounded(
    cmd: list[str],
    *,
    cwd: Path,
    label: str,
    timeout_s: int = 300,
    heartbeat_s: int = 30,
) -> tuple[bool, str]:
    """Run ``cmd`` with an explicit timeout, progress heartbeat, and guaranteed
    child-process-group cleanup.

    Returns ``(succeeded, message)``. Output streams live (never captured) so
    the CI run keeps heartbeating and a wedge is visible in the streamed log.
    On timeout the whole process group is reaped and the message names the
    orphan-reap explicitly, so a timeout is distinguishable from a plain
    non-zero exit (code failure) in the diagnostics.
    """
    print(f"aggregate: [{label}] running: {' '.join(cmd)}", flush=True)
    try:
        proc = subprocess.Popen(cmd, cwd=str(cwd), start_new_session=True)
    except OSError as exc:
        return False, f"[{label}] subprocess failed to start: {exc}"

    started_at = time.monotonic()
    next_heartbeat_at = started_at + heartbeat_s
    deadline_at = started_at + timeout_s

    while proc.poll() is None:
        now = time.monotonic()
        if now >= deadline_at:
            _reap_process_group(proc)
            return False, (
                f"[{label}] timed out after {timeout_s}s; child process GROUP "
                f"reaped (orphaned-child cleanup). This is runner pressure / a "
                f"wedge, NOT a coverage-content failure."
            )
        if now >= next_heartbeat_at:
            print(
                f"aggregate: [{label}] still running after {int(now - started_at)}s",
                flush=True,
            )
            next_heartbeat_at = now + heartbeat_s
        time.sleep(min(1.0, max(0.1, next_heartbeat_at - now)))

    if proc.returncode:
        return False, (
            f"[{label}] exited {proc.returncode} (code failure — see streamed "
            f"output above; this is NOT a timeout/runner-pressure case)."
        )
    return True, f"[{label}] ok"


# ---------------------------------------------------------------------------
# Metadata discovery + fail-closed validation
# ---------------------------------------------------------------------------
def load_metadata(artifacts_dir: Path) -> list[dict[str, object]]:
    """Parse every ``coverage-meta-*.json`` sidecar under ``artifacts_dir``.

    A sidecar that does not parse is itself a fail-closed condition
    (``ARTIFACT_MALFORMED``) — a coverage census cannot be trusted if the
    provenance sidecar describing it is corrupt.
    """
    metas: list[dict[str, object]] = []
    for meta_path in sorted(artifacts_dir.glob("coverage-meta-*.json")):
        try:
            metas.append(json.loads(meta_path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError) as exc:
            raise ArtifactValidationError(
                REASON_MALFORMED, f"{meta_path.name} did not parse: {exc}"
            ) from exc
    return metas


def validate_artifacts(
    metas: list[dict[str, object]],
    artifacts_dir: Path,
    *,
    expected_head: str,
    split_count: int,
    max_age_seconds: int | None = None,
    now: datetime | None = None,
) -> str:
    """Fail-closed validation of the shard artifact set.

    Returns the consensus ``test_scope`` ("full" | "smart") on success; raises
    ``ArtifactValidationError`` (reason-coded) on any defect. Never returns a
    "clean" verdict over an incomplete/mis-bound set — absence of a required
    shard is a FAILURE, not a silent skip.
    """
    if not metas:
        raise ArtifactValidationError(
            REASON_MISSING,
            "no coverage-meta-*.json artifacts found — the test shards emitted "
            "no coverage census to aggregate.",
        )

    required = {"schema_version", "head_sha", "split", "split_count", "test_scope"}
    seen_splits: dict[int, dict[str, object]] = {}
    scopes: set[str] = set()

    for meta in metas:
        missing_keys = required - set(meta.keys())
        if missing_keys:
            raise ArtifactValidationError(
                REASON_MALFORMED,
                f"metadata missing required keys {sorted(missing_keys)}: {meta!r}",
            )
        if meta["schema_version"] != SCHEMA_VERSION:
            raise ArtifactValidationError(
                REASON_SCHEMA_MISMATCH,
                f"schema_version {meta['schema_version']!r} != expected "
                f"{SCHEMA_VERSION!r} — producer/consumer version skew.",
            )
        if meta["head_sha"] != expected_head:
            raise ArtifactValidationError(
                REASON_WRONG_HEAD,
                f"split {meta.get('split')!r} artifact is bound to head "
                f"{meta['head_sha']!r}, expected {expected_head!r} — stale / "
                f"cross-head artifact, refusing to combine.",
            )
        split = meta["split"]
        if not isinstance(split, int):
            raise ArtifactValidationError(
                REASON_MALFORMED, f"split is not an int: {split!r}"
            )
        if split in seen_splits:
            raise ArtifactValidationError(
                REASON_MALFORMED, f"duplicate artifact for split {split}"
            )
        seen_splits[split] = meta
        scope = meta["test_scope"]
        if not isinstance(scope, str):
            raise ArtifactValidationError(
                REASON_MALFORMED, f"test_scope is not a str: {scope!r}"
            )
        scopes.add(scope)

        if max_age_seconds is not None:
            created_raw = meta.get("created_at")
            if not isinstance(created_raw, str):
                raise ArtifactValidationError(
                    REASON_MALFORMED,
                    f"created_at missing/invalid for split {split}: {created_raw!r}",
                )
            try:
                created = datetime.fromisoformat(created_raw)
            except ValueError as exc:
                raise ArtifactValidationError(
                    REASON_MALFORMED,
                    f"created_at unparseable for split {split}: {created_raw!r}",
                ) from exc
            if created.tzinfo is None:
                created = created.replace(tzinfo=UTC)
            ref = now or datetime.now(UTC)
            age = (ref - created).total_seconds()
            if age > max_age_seconds:
                raise ArtifactValidationError(
                    REASON_STALE,
                    f"split {split} artifact is {int(age)}s old > "
                    f"{max_age_seconds}s window — stale.",
                )

        # The data file must physically exist alongside the sidecar.
        data_file = artifacts_dir / f".coverage.{split}"
        if not data_file.is_file():
            raise ArtifactValidationError(
                REASON_MISSING,
                f"split {split} metadata present but its coverage data file "
                f"{data_file.name} is absent — partial/lost shard artifact.",
            )

    expected = set(range(1, split_count + 1))
    if set(seen_splits) != expected:
        missing = sorted(expected - set(seen_splits))
        extra = sorted(set(seen_splits) - expected)
        raise ArtifactValidationError(
            REASON_MISSING,
            f"shard set incomplete for split_count={split_count}: "
            f"missing={missing} extra={extra} — refusing to combine a partial "
            f"census (would under-count coverage silently).",
        )

    if len(scopes) != 1:
        raise ArtifactValidationError(
            REASON_MALFORMED,
            f"inconsistent test_scope across shards: {sorted(scopes)} — the "
            f"shards did not agree on full vs smart selection.",
        )
    return scopes.pop()


# ---------------------------------------------------------------------------
# Combine once + generate coverage.json
# ---------------------------------------------------------------------------
def combine_coverage(
    artifacts_dir: Path,
    split_count: int,
    target_dir: Path,
    out_path: Path,
    *,
    timeout_s: int = 300,
) -> tuple[bool, str]:
    """Combine the per-shard ``.coverage.<split>`` data files ONCE into a
    single ``coverage.json`` at ``out_path`` — the whole point of WS2: no
    second ``pytest --cov`` pass, just an arithmetic merge of data already
    collected by the shards.

    The data files are copied into ``target_dir`` (so their recorded source
    paths resolve against the real checkout) and combined with ``--keep`` so
    the originals survive for diagnostics.
    """
    py = sys.executable
    staged: list[Path] = []
    for split in range(1, split_count + 1):
        src = artifacts_dir / f".coverage.{split}"
        dst = target_dir / f".coverage.{split}"
        shutil.copy2(src, dst)
        staged.append(dst)

    # Fresh combined data file — never trust a leftover .coverage from a
    # previous step in the same runner.
    combined = target_dir / ".coverage"
    if combined.exists():
        combined.unlink()

    ok, msg = run_bounded(
        [py, "-m", "coverage", "combine", "--keep", *[str(p) for p in staged]],
        cwd=target_dir,
        label="coverage-combine",
        timeout_s=timeout_s,
    )
    if not ok:
        return False, msg

    # Combined shard data can contain traces from C extensions or provider
    # modules whose original source paths are not present in the checkout.
    # `coverage json -i` keeps those records from making the shadow aggregate
    # red while still failing on malformed data, combine errors, and timeouts.
    ok, msg = run_bounded(
        [py, "-m", "coverage", "json", "-i", "-o", str(out_path)],
        cwd=target_dir,
        label="coverage-json",
        timeout_s=timeout_s,
    )
    if not ok:
        return False, msg
    if not out_path.is_file():
        return False, f"coverage json reported success but {out_path} is absent"
    return True, f"combined {split_count} shard(s) -> {out_path}"


# ---------------------------------------------------------------------------
# Shadow parity comparison
# ---------------------------------------------------------------------------
def _totals(coverage_json: Path) -> dict[str, float]:
    data = json.loads(coverage_json.read_text(encoding="utf-8"))
    totals = data.get("totals", {})
    return {
        "percent_covered": float(totals.get("percent_covered", 0.0)),
        "covered_lines": float(totals.get("covered_lines", 0)),
        "num_statements": float(totals.get("num_statements", 0)),
    }


def parity_compare(
    shadow_json: Path,
    authoritative_json: Path,
    *,
    tolerance: float,
) -> tuple[bool, dict[str, float]]:
    """One-sided shadow parity: PASS iff the shadow census does not LOSE
    coverage versus the authoritative census.

        shadow_percent >= authoritative_percent - tolerance

    Over-coverage (shadow higher, because shard integration tests ran under a
    provisioned Postgres the authoritative gate skips) is expected and passes.
    Returns ``(ok, report)`` where report carries both totals and the signed
    delta for the readback.
    """
    shadow = _totals(shadow_json)
    auth = _totals(authoritative_json)
    delta = shadow["percent_covered"] - auth["percent_covered"]
    ok = delta >= -tolerance
    report = {
        "shadow_percent": shadow["percent_covered"],
        "authoritative_percent": auth["percent_covered"],
        "delta_percent": delta,
        "tolerance": tolerance,
        "shadow_covered_lines": shadow["covered_lines"],
        "authoritative_covered_lines": auth["covered_lines"],
        "shadow_num_statements": shadow["num_statements"],
        "authoritative_num_statements": auth["num_statements"],
    }
    return ok, report


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts-dir", required=True)
    parser.add_argument("--expected-head", required=True)
    parser.add_argument("--split-count", type=int, required=True)
    parser.add_argument(
        "--target-dir",
        default=str(_REPO_ROOT),
        help="Repo checkout root to sweep (default: this repo).",
    )
    parser.add_argument("--out", default=None, help="Combined coverage.json path.")
    parser.add_argument(
        "--compare-to",
        default=None,
        help="Authoritative coverage.json for shadow parity (optional).",
    )
    parser.add_argument("--tolerance", type=float, default=0.5)
    parser.add_argument("--target-pct", type=float, default=50.0)
    parser.add_argument(
        "--max-age-seconds",
        type=int,
        default=None,
        help="Reject artifacts older than this (defence-in-depth staleness).",
    )
    args = parser.parse_args(argv)

    artifacts_dir = Path(args.artifacts_dir).resolve()
    target_dir = Path(args.target_dir).resolve()
    out_path = Path(args.out).resolve() if args.out else (target_dir / "coverage.json")

    print("=== coverage-aggregate-shadow (OMN-14680) ===", flush=True)
    print(f"aggregate: expected_head={args.expected_head}", flush=True)
    print(f"aggregate: split_count={args.split_count}", flush=True)
    print(f"aggregate: artifacts_dir={artifacts_dir}", flush=True)

    # 1) Fail-closed validation.
    try:
        metas = load_metadata(artifacts_dir)
        scope = validate_artifacts(
            metas,
            artifacts_dir,
            expected_head=args.expected_head,
            split_count=args.split_count,
            max_age_seconds=args.max_age_seconds,
        )
    except ArtifactValidationError as exc:
        print(f"::error::aggregate FAIL-CLOSED [{exc.reason}] {exc.message}")
        return EXIT_ARTIFACT
    print(f"aggregate: validation OK — {len(metas)} shard(s), scope={scope}")

    # 2) Combine once (no second pytest --cov pass).
    ok, msg = combine_coverage(artifacts_dir, args.split_count, target_dir, out_path)
    print(f"aggregate: {msg}")
    if not ok:
        print(f"::error::aggregate combine engine failure: {msg}")
        return EXIT_ENGINE

    # 3) Sweep the combined artifact (same handler the authoritative gate uses).
    result = NodeCoverageSweep().handle(
        CoverageSweepRequest(target_dirs=[str(target_dir)], target_pct=args.target_pct)
    )
    print(json.dumps(result.model_dump(mode="json"), indent=2, default=str))
    if result.status == "error":
        print(
            "::error::aggregate sweep status=error — "
            f"coverage_missing={result.coverage_missing!r}, "
            f"total_modules={result.total_modules}"
        )
        return EXIT_SWEEP_ERROR
    print(
        f"aggregate: combined census — modules={result.total_modules}, "
        f"avg={result.average_coverage}, status={result.status}"
    )

    # 4) Shadow parity (informational under smart selection; gated on full).
    compare_to = Path(args.compare_to).resolve() if args.compare_to else None
    if compare_to is None or not compare_to.is_file():
        print(
            "aggregate: parity SKIPPED — no authoritative coverage.json available "
            f"({compare_to}). Combine+sweep succeeded; parity will be observed "
            "when the authoritative artifact is present."
        )
        return EXIT_OK

    parity_ok, report = parity_compare(out_path, compare_to, tolerance=args.tolerance)
    print("=== shadow parity report ===")
    print(json.dumps(report, indent=2))
    if scope != "full":
        print(
            f"aggregate: parity INFORMATIONAL (scope={scope}); combined census is a "
            "deliberate subset under smart selection, so parity is not gated."
        )
        return EXIT_OK
    if not parity_ok:
        print(
            f"::error::aggregate PARITY DIVERGENCE — shadow lost coverage vs "
            f"authoritative (delta={report['delta_percent']:.3f}pp < "
            f"-{args.tolerance}pp). Do NOT cut over; the authoritative sweep "
            f"remains required."
        )
        return EXIT_PARITY
    print(
        f"aggregate: parity PASS — shadow {report['shadow_percent']:.3f}% vs "
        f"authoritative {report['authoritative_percent']:.3f}% "
        f"(delta {report['delta_percent']:+.3f}pp, tolerance {args.tolerance}pp)."
    )
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
