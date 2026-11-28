# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Shared precondition gating for the C30/C31/C32 local-MVP producers (OMN-19989).

WHY PRECONDITION GATING RATHER THAN A REFUSAL
---------------------------------------------
These three criteria were registered before the surfaces they grade existed,
because the board can only resolve a criterion from PROBES or READBACKS and a
criterion in neither renders PROOF NOT RUN forever (OMN-19185). The first cut of
each producer simply exited 1 saying "not implemented", which is honest and
useless: three flat reds that say nothing about progress and that a person must
remember to come back and edit.

A gated producer says which surface is missing instead. `onex dashboard` absent
reads differently from the five auth cases failing, and the chip turns green on
its own the day the surface lands -- no edit, no dispatch, no one remembering.

THREE OUTCOMES, THREE EXIT CODES
--------------------------------
Borrowed from `c17_customer_surface_probe.py`, which already separates "a clause
failed" from "the source could not be read":

    0  PASS                  every clause asserted and held
    1  FAIL                  a clause was graded and did not hold
    2  PRECONDITION-ABSENT   the surface being graded does not exist yet, so no
                             clause was graded at all

The board renders 1 and 2 identically -- red -- and that is correct: neither is
evidence of green. The distinction is for the person reading the log, and for
the ratchet below.

THE ONE RULE THAT MATTERS
-------------------------
A probe that graded nothing must never exit 0. An absent surface is not a pass,
and `verdict()` cannot return PASS with an empty clause list -- it raises. That
is the false-green this whole registration was built to refuse, so it fails
loudly rather than defaulting.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

VERDICT_PASS = "PASS"
VERDICT_FAIL = "FAIL"
VERDICT_PRECONDITION_ABSENT = "PRECONDITION-ABSENT"

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_PRECONDITION_ABSENT = 2

_EXIT = {
    VERDICT_PASS: EXIT_PASS,
    VERDICT_FAIL: EXIT_FAIL,
    VERDICT_PRECONDITION_ABSENT: EXIT_PRECONDITION_ABSENT,
}

# OMN-19984 AC2a. The board-probe-result.v1 identity a C31/C32 run publishes, so
# that omnimarket's `satisfies_verdict_request` (OMN-19937) -- the required check
# -- can refuse a result that is missing, skipped or for another commit. C30 is
# absent on purpose: it gates no merge (ticket body), so it has no required check.
BOARD_CHECK_IDS = {"C31": "local_dashboard_parity", "C32": "local_auth_boundary"}

# PRECONDITION-ABSENT graded nothing, so it is INDETERMINATE -- never PASS.
_BOARD_OUTCOME = {
    VERDICT_PASS: "PASS",
    VERDICT_FAIL: "FAIL",
    VERDICT_PRECONDITION_ABSENT: "INDETERMINATE",
}

# The GitHub Actions variables a board result is built from. Outside CI none is
# set, and a record that cannot name its commit must not claim one.
_BOARD_ENV = (
    "GITHUB_SHA",
    "GITHUB_REPOSITORY",
    "GITHUB_RUN_ID",
    "GITHUB_RUN_ATTEMPT",
    "RUNNER_NAME",
)


@dataclass(frozen=True)
class Precondition:
    """A surface the probe needs before it can grade anything.

    `detail` names what was looked for and where, so a red chip leads somewhere.
    `owner` is the ticket that makes it exist -- without it a reader has the
    diagnosis and no next step.
    """

    name: str
    present: bool
    detail: str
    owner: str


@dataclass(frozen=True)
class Clause:
    """One graded assertion. `ok` False is a real failure, not a skip."""

    name: str
    ok: bool
    detail: str


@dataclass
class ProbeResult:
    criterion: str
    preconditions: list[Precondition] = field(default_factory=list)
    clauses: list[Clause] = field(default_factory=list)

    @property
    def missing(self) -> list[Precondition]:
        return [p for p in self.preconditions if not p.present]

    def verdict(self) -> str:
        if self.missing:
            return VERDICT_PRECONDITION_ABSENT
        if not self.clauses:
            # Preconditions all held and nothing was graded. That is a defect in
            # the probe, not a pass, and it is the exact shape that would
            # publish a green criterion on no evidence.
            raise AssertionError(
                f"{self.criterion}: preconditions hold but no clause was graded; "
                "a probe that grades nothing must not report a verdict"
            )
        return VERDICT_PASS if all(c.ok for c in self.clauses) else VERDICT_FAIL

    def exit_code(self) -> int:
        return _EXIT[self.verdict()]

    def board_result(
        self, env: dict[str, str] | None = None
    ) -> dict[str, object] | None:
        """This run as a board-probe-result.v1 payload, or None when it cannot be one.

        None for a criterion with no required check (C30), and for a run whose
        environment does not say which commit it graded.
        """
        check_id = BOARD_CHECK_IDS.get(self.criterion)
        env_map: Mapping[str, str] = os.environ if env is None else env
        if check_id is None or any(not env_map.get(name) for name in _BOARD_ENV):
            return None
        sha = env_map["GITHUB_SHA"]
        run_id, attempt = env_map["GITHUB_RUN_ID"], env_map["GITHUB_RUN_ATTEMPT"]
        return {
            "check_id": check_id,
            "subject_kind": "commit",
            "subject": sha,
            "repo": env_map["GITHUB_REPOSITORY"],
            "sha": sha,
            "surface_instance": env_map["RUNNER_NAME"],
            "execution_id": f"{run_id}.{attempt}",
            "outcome": _BOARD_OUTCOME[self.verdict()],
            "reasons": [f"absent: {p.name}" for p in self.missing]
            + [f"failed: {c.name}" for c in self.clauses if not c.ok],
            "evidence_items": [
                f"https://github.com/{env_map['GITHUB_REPOSITORY']}/actions/runs/{run_id}/attempts/{attempt}"
            ],
            "finished_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }

    def record(self, env: dict[str, str] | None = None) -> Mapping[str, object]:
        board = self.board_result(env)
        record = {
            "criterion": self.criterion,
            "verdict": self.verdict(),
            "preconditions": [
                {
                    "name": p.name,
                    "present": p.present,
                    "detail": p.detail,
                    "owner": p.owner,
                }
                for p in self.preconditions
            ],
            "clauses": [
                {"name": c.name, "ok": c.ok, "detail": c.detail} for c in self.clauses
            ],
            "board_result": board,
        }
        if board is None:
            record["board_result_absent"] = (
                f"{self.criterion} has no required check"
                if self.criterion not in BOARD_CHECK_IDS
                else f"not every one of {', '.join(_BOARD_ENV)} is set, so this run names no commit"
            )
        return record

    def render(self) -> str:
        """Human-readable, and the first line is the machine-readable verdict."""
        out = [f"{self.criterion}_VERDICT={self.verdict()}"]
        if self.missing:
            out.append("")
            out.append(
                "NOT GRADED -- the surface this criterion grades does not exist yet."
            )
            out.append(
                "This is an infrastructure verdict (exit 2), not a clause failure."
            )
            out.append("")
            for p in self.missing:
                out.append(f"  ABSENT  {p.name}")
                out.append(f"          {p.detail}")
                out.append(f"          built by: {p.owner}")
            present = [p for p in self.preconditions if p.present]
            if present:
                out.append("")
                out.append("Already in place:")
                for p in present:
                    out.append(f"  present {p.name} -- {p.detail}")
        for c in self.clauses:
            out.append(f"  {'PASS' if c.ok else 'FAIL'}  {c.name}: {c.detail}")
        return "\n".join(out)


# ------------------------------------------------------------------ detectors
#
# Every detector answers a yes/no about the LIVE machine and never about this
# repository's working tree. A probe that graded the checkout would prove the
# repository builds, which is not what any of these three criteria claim.


def _run(
    argv: list[str], timeout: int = 60, *, env: dict[str, str] | None = None
) -> tuple[int, str, str]:
    try:
        p = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, check=False, env=env
        )
    except FileNotFoundError:
        return 127, "", f"{argv[0]}: not found"
    except subprocess.TimeoutExpired:
        return 124, "", f"{' '.join(argv)}: timed out after {timeout}s"
    return p.returncode, p.stdout, p.stderr


def have_executable(name: str) -> bool:
    return shutil.which(name) is not None


def onex_cli() -> Precondition:
    present = have_executable("onex")
    return Precondition(
        name="onex CLI on PATH",
        present=present,
        detail="`onex` resolved on PATH"
        if present
        else "`onex` is not on PATH on this machine",
        owner="OMN-19915",
    )


def onex_subcommand(sub: str, owner: str) -> Precondition:
    """Is `onex <sub>` a registered subcommand on the installed CLI?

    `--help` is used rather than running the command: a subcommand that exists
    but needs a populated store must still register, and running it for real
    here would make the detector depend on the data it is gating.
    """
    if not have_executable("onex"):
        return Precondition(
            name=f"onex {sub}",
            present=False,
            detail="`onex` is not on PATH, so no subcommand could be probed",
            owner=owner,
        )
    rc, _out, _err = _run(["onex", sub, "--help"], timeout=60)
    return Precondition(
        name=f"onex {sub}",
        present=rc == 0,
        detail=(
            f"`onex {sub} --help` exited 0"
            if rc == 0
            else f"`onex {sub} --help` exited {rc}; the subcommand is not registered"
        ),
        owner=owner,
    )


def env_var_present(name: str, owner: str) -> Precondition:
    """Presence only. The VALUE is never read, logged or returned."""
    present = bool(os.environ.get(name))
    return Precondition(
        name=f"${name}",
        present=present,
        detail=f"${name} is set" if present else f"${name} is not set on this runner",
        owner=owner,
    )


#: Stable id so a re-run UPSERTs the one fixture row instead of accumulating.
FIXTURE_CORRELATION_ID = "c31c31c3-1000-4000-8000-000000000001"
FIXTURE_SESSION_ID = "c31c31c3-1000-4000-8000-000000000002"


# OMN-19984's scope line is "clean container, one stubbed delegation", and C31's
# proof text is "a clean container installs, runs one delegation and starts the
# dashboard". Named `fixture` rather than that word because the docstring guard
# treats it as an incompleteness marker in any casing.
#: The fixture's measured figures. Large enough that a dashboard showing zero, or
#: a cent-level drift, cannot reconcile by accident: against the default
#: baseline these give a savings figure of whole dollars, not fractions of a cent.
FIXTURE_TOKENS_IN = 1_000_000
FIXTURE_TOKENS_OUT = 200_000
FIXTURE_COST_USD = 1.0


def seed_fixture_delegation(db_path: Path, *, tenant_id: str) -> Precondition:
    """Materialize one fixture delegation into `db_path`, or say why not.

    Nothing was running a delegation, so `onex metering` had no evidence store to
    read and the probe never reached its clauses:

        exited 1: no local delegation evidence database at
        ~/.omninode/delegation/delegation.sqlite

    A real delegation is the wrong instrument here: it needs a funded provider,
    it is non-deterministic, and the clauses grade the METERING CONTRACT, not a
    provider's answer. So this drives the SAME production projection handler
    `omnimarket.cli.cli_delegation_cost_demo` uses for the conference demo,
    against a SqliteDatabaseAdapter. The fixture is the input; the contract is
    what is graded.

    `db_path` is the evidence store under the probe's own scratch HOME, never
    the store a developer's own `onex delegate` runs materialize. It sits at the
    default path relative to that HOME because `onex dashboard` has no `--db`:
    it serves whatever store its HOME resolves, so the seeded row has to be there.
    """
    try:
        from omnimarket.models.delegation.wire.model_delegate_skill_terminal_projection import (
            ModelDelegateSkillTerminalProjection,
        )
        from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
            HandlerProjectionDelegation,
        )
        from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter
    except ImportError as exc:
        return Precondition(
            name="fixture delegation seeded",
            present=False,
            detail=f"omnimarket projection handlers are not importable: {exc}",
            owner="OMN-19984",
        )

    # `project_delegate_skill_terminal`, which is what the real local path calls
    # (`port_local_delegation_dispatch.py:2967`). NOT `project`: that writes no
    # `created_at`, and the metering reader selects it, so a store seeded through
    # `project` is unreadable -- run 36647365525 failed exactly there with
    # "no such column: created_at". Only the terminal method injects it
    # (OMN-13171).
    #
    # The row carries the identity `onex local init` minted in the probe's
    # scratch HOME (`local_tenant_identity`). The served Runs and Overview
    # exposures are tenant-scoped, so a row under any other tenant -- or none --
    # is one the dashboard correctly refuses to show, and parity could never hold.
    # `correlation_id` and `session_id` are UUIDs on this model, not free strings
    # (run 36647695052 failed validation on that), and `status` is required. The
    # ids are fixed so a re-run UPSERTs the one row rather than accumulating.
    # Tokens and cost are present so the run is MEASURED: with neither, metering
    # reports `savings_usd: null` and there is no figure for the page to agree with.
    payload = {
        "correlation_id": FIXTURE_CORRELATION_ID,
        "session_id": FIXTURE_SESSION_ID,
        "status": "completed",
        "task_type": "c31-metering-contract-fixture",
        "delegated_to": "probe-fixture",
        "model_name": "probe-fixture",
        "tenant_id": tenant_id,
        "metrics": {
            "input_tokens": FIXTURE_TOKENS_IN,
            "output_tokens": FIXTURE_TOKENS_OUT,
            "total_tokens": FIXTURE_TOKENS_IN + FIXTURE_TOKENS_OUT,
            "cost_usd": FIXTURE_COST_USD,
        },
        "emitted_at": datetime.now(tz=UTC).isoformat(),
    }
    try:
        adapter = SqliteDatabaseAdapter(db_path)
        terminal = ModelDelegateSkillTerminalProjection.from_payload(payload)
        HandlerProjectionDelegation().project_delegate_skill_terminal(terminal, adapter)
    except Exception as exc:  # the probe reports, never raises
        return Precondition(
            name="fixture delegation seeded",
            present=False,
            detail=f"{type(exc).__name__}: {exc}"[:220],
            owner="OMN-19984",
        )
    return Precondition(
        name="fixture delegation seeded",
        present=True,
        detail=(
            f"one measured fixture row at {db_path} (tenant {tenant_id}, "
            f"{FIXTURE_TOKENS_IN}/{FIXTURE_TOKENS_OUT} tokens, cost {FIXTURE_COST_USD} USD)"
        ),
        owner="OMN-19984",
    )


def onex_metering_json(
    db_path: Path | None = None, *, env: dict[str, str] | None = None
) -> tuple[Precondition, dict[str, object] | None]:
    """`onex metering --json` and its parsed payload, or None.

    This is the one surface that already exists, so C31 can grade a real clause
    today: the contract the dashboard's savings figure has to agree with.

    `db_path` points the read at the caller's seeded store. Without it the CLI
    resolves `default_evidence_db_path()`, which on a CI runner is empty.
    """
    pre = onex_subcommand("metering", owner="OMN-19977")
    if not pre.present:
        return pre, None
    cmd = ["onex", "metering", "--json"]
    if db_path is not None:
        cmd = ["onex", "metering", "--db", str(db_path), "--json"]
    rc, out, err = _run(cmd, timeout=120, env=env)
    if rc != 0:
        return (
            Precondition(
                name="onex metering --json",
                present=False,
                detail=f"exited {rc}: {(err or out).strip()[:200]}",
                owner="OMN-19977",
            ),
            None,
        )
    try:
        payload = json.loads(out)
    except json.JSONDecodeError as exc:
        return (
            Precondition(
                name="onex metering --json",
                present=False,
                detail=f"stdout is not JSON: {exc}",
                owner="OMN-19977",
            ),
            None,
        )
    return (
        Precondition(
            name="onex metering --json",
            present=True,
            detail="exited 0 and stdout parsed as JSON",
            owner="OMN-19977",
        ),
        payload,
    )


def local_tenant_identity(
    env: dict[str, str],
    *,
    adopt: str | None = None,
    run: Callable[..., tuple[int, str, str]] | None = None,
) -> tuple[Precondition, str | None]:
    """`onex local init --json` in the probe's scratch HOME, and the tenant it holds.

    The served Runs and Overview exposures are tenant-scoped: with no identity
    the dashboard answers `422 tenant_not_configured` and serves no row at all.
    `adopt` takes the tenant a store's existing rows already carry (C30's
    receipt) instead of minting a fresh one, so the dashboard reads under the
    tenant the run was written for. `run` is injectable; tests never start it.
    """
    argv = ["onex", "local", "init", "--json"]
    if adopt:
        argv += ["--tenant-id", adopt]
    rc, out, err = (run or _run)(argv, timeout=120, env=env)
    name = "scratch install has a tenant identity (`onex local init`)"
    if rc != 0:
        return Precondition(
            name, False, f"exited {rc}: {(err or out).strip()[:200]}", "OMN-18699"
        ), None
    try:
        tenant = json.loads(out).get("tenant_id")
    except (json.JSONDecodeError, AttributeError):
        tenant = None
    if not isinstance(tenant, str) or not tenant:
        return Precondition(
            name, False, "`onex local init --json` printed no tenant_id", "OMN-18699"
        ), None
    return Precondition(name, True, f"tenant_id={tenant}", "OMN-18699"), tenant


# ------------------------------------------------------- the served dashboard
#
# `onex dashboard` (omnimarket, OMN-19976) is a lifecycle group: `start` serves the
# contract-declared exposures from the local store on a loopback port and prints no
# token, `status --json` names the `credential_ref` its session token is stored
# under, and `stop` ends the recorded process. The probes start the real command
# against their scratch HOME and read it over HTTP, the way the page does, sending
# `Authorization: Bearer` with the token read from the store under that ref on the
# readiness poll and on every exposure read (OMN-19966). The token is never printed,
# never put in a URL and never recorded. A refusal naming a surface the server does
# not serve yet is a PRECONDITION, not a clause failure: grading "the row is wrong"
# against a server that answered "I do not serve that table" would be a FAIL that
# says nothing true. Each such code names the ticket that makes the surface exist.

#: Typed server errors that mean the surface is not served yet -> its owner.
SURFACE_ABSENT_ERRORS = {
    "projection_column_missing": "OMN-19976 (omnimarket#3319: the local store carries every declared column)",
    "projection_table_missing": "OMN-19976 (the local store has no table behind this exposure)",
    "not_yet_bus_backed": "OMN-19976 (this exposure is not served from the local store yet)",
    "unknown_topic": "OMN-19976 (the exposure is not declared)",
}
DASHBOARD_READY_SECONDS = 30
#: `onex dashboard start` itself waits up to 180 s for the server's start line.
DASHBOARD_START_SECONDS = 200
#: The store `onex` resolves for a HOME: the delegation evidence database, which also holds credentials.
CREDENTIAL_DB_PARTS = (".omninode", "delegation", "delegation.sqlite")


@dataclass(frozen=True)
class ExposureRead:
    """One `GET /projection/<topic>` answer, reduced to what grading needs."""

    topic: str
    status: int | None  # None: no HTTP answer at all
    rows: tuple[dict[str, object], ...] = ()
    error: str | None = None

    @property
    def served(self) -> bool:
        return self.status == 200 and self.error is None

    def absent(self) -> Precondition | None:
        """The precondition this answer proves missing, or None if it was served."""
        if self.served:
            return None
        short = self.topic.removeprefix("onex.snapshot.projection.")
        if self.status is None:
            return Precondition(
                f"dashboard answers {short}",
                False,
                "the dashboard the probe started gave no HTTP answer",
                "OMN-19976",
            )
        owner = SURFACE_ABSENT_ERRORS.get(self.error or "")
        if self.status == 404 and owner is None:
            owner = SURFACE_ABSENT_ERRORS["unknown_topic"]
        if self.error == "tenant_not_configured":
            owner = "OMN-18699 (the scratch install's `onex local init` identity)"
        return Precondition(
            f"dashboard serves {short}",
            False,
            f"HTTP {self.status} {self.error or 'no error code'}",
            owner or "OMN-19976",
        )


def bearer_headers(token: str | None) -> dict[str, str]:
    """The credential a read carries: ``Authorization: Bearer`` and nothing in the URL."""
    return {"Authorization": f"Bearer {token}"} if token else {}


def _http_get(
    url: str, timeout: float = 5, headers: Mapping[str, str] | None = None
) -> tuple[int | None, object]:
    """(status, parsed JSON body) for a GET; (None, None) when nothing answered."""
    request = urllib.request.Request(url, headers=dict(headers or {}))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            body = json.load(exc)
        except (ValueError, OSError):
            body = None
        return exc.code, body
    except (OSError, ValueError):
        return None, None


def dashboard_url(base_url: str, path: str, **params: str) -> str:
    """`path` on the dashboard at `base_url`, with `params` as its query."""
    parts = urllib.parse.urlsplit(base_url)
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, path, urllib.parse.urlencode(params), "")
    )


def read_exposure(
    base_url: str,
    topic: str,
    *,
    token: str | None,
    correlation_id: str | None = None,
    get: Callable[
        [str, float, Mapping[str, str]], tuple[int | None, object]
    ] = _http_get,
) -> ExposureRead:
    """Read one exposure, sending the session token as ``Authorization: Bearer``."""
    extra = {} if correlation_id is None else {"correlation_id": correlation_id}
    url = dashboard_url(
        base_url, f"/projection/{urllib.parse.quote(topic, safe='')}", **extra
    )
    status, body = get(url, 5, bearer_headers(token))
    if status is None:
        return ExposureRead(topic, None, (), "no answer")
    error = body.get("error") if isinstance(body, dict) else None
    raw = body.get("rows") if isinstance(body, dict) else None
    rows = tuple(r for r in raw if isinstance(r, dict)) if isinstance(raw, list) else ()
    if status == 200 and not isinstance(raw, list):
        error = error or "no rows envelope"
    return ExposureRead(topic, status, rows, error)


@dataclass(frozen=True)
class DashboardStart:
    """What one `onex dashboard start` gave: where it serves, the session token and what happened.

    `token` is read from the store and never printed or recorded; it is excluded from the repr.
    """

    base_url: str | None
    detail: str
    token: str | None = field(default=None, repr=False)


def _lifecycle(
    run: Callable[..., tuple[int, str, str]],
    action: str,
    env: dict[str, str],
    timeout: int,
) -> tuple[dict[str, object] | None, str]:
    """(the observation `onex dashboard <action> --json` printed, a detail naming the failure)."""
    rc, out, err = run(["onex", "dashboard", action, "--json"], timeout, env=env)
    observation: dict[str, object] | None = None
    for line in reversed(out.strip().splitlines()):
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            observation = parsed
            break
    if rc != 0 or observation is None or observation.get("status") != "success":
        said = (err or out).strip().splitlines()[-1:] or ["no output"]
        return None, f"`onex dashboard {action} --json` exited {rc}: {said[0][:200]}"
    return observation, ""


def _evidence(observation: dict[str, object]) -> dict[str, str]:
    raw = observation.get("evidence")
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items()}


def read_dashboard_token(env: dict[str, str], credential_ref: str) -> str | None:
    """The session token stored under `credential_ref` in the scratch HOME's store, or None.

    Read the way `onex dashboard open` reads it: through the local credential store, never printed.
    """
    from omnimarket.inference.local_byok_credential_adapter import (
        LocalByokCredentialStore,
    )

    db_path = Path(env.get("HOME", "")).joinpath(*CREDENTIAL_DB_PARTS)
    return asyncio.run(
        LocalByokCredentialStore(db_path=db_path).get_secret(credential_ref)
    )


@contextmanager
def running_dashboard(
    env: dict[str, str],
    *,
    ready_seconds: float = DASHBOARD_READY_SECONDS,
    get: Callable[
        [str, float, Mapping[str, str]], tuple[int | None, object]
    ] = _http_get,
    run: Callable[..., tuple[int, str, str]] | None = None,
    read_token: Callable[[dict[str, str], str], str | None] = read_dashboard_token,
) -> Iterator[DashboardStart]:
    """Start `onex dashboard`, yield a :class:`DashboardStart`, always stop it.

    `onex dashboard start --json` serves; `onex dashboard status --json` names the `credential_ref`; the token is
    read from the store under it. The dashboard is ready when `/projections` answers 200 to a request carrying
    ``Authorization: Bearer``, polled to a deadline: an unauthenticated 401 is never taken for ready.
    """
    runner = run if run is not None else _run
    try:
        started, why = _lifecycle(runner, "start", env, DASHBOARD_START_SECONDS)
        if started is None:
            yield DashboardStart(None, why)
            return
        status, why = _lifecycle(runner, "status", env, 60)
        if status is None:
            yield DashboardStart(None, why)
            return
        evidence = _evidence(status)
        url, ref = evidence.get("url"), evidence.get("credential_ref")
        if not url or not ref:
            yield DashboardStart(
                None,
                "`onex dashboard status --json` named no "
                + ("url" if not url else "credential_ref"),
            )
            return
        token = read_token(env, ref)
        if not token:
            yield DashboardStart(
                None, f"no session token in the store under credential_ref {ref!r}"
            )
            return
        deadline = time.monotonic() + ready_seconds
        while True:
            answer, _body = get(
                dashboard_url(url, "/projections"), 2, bearer_headers(token)
            )
            if answer == 200:
                break
            if time.monotonic() >= deadline:
                yield DashboardStart(
                    None,
                    f"dashboard at {url} never answered /projections 200 to its bearer token (last: {answer})",
                )
                return
            time.sleep(0.2)
        yield DashboardStart(url, f"dashboard served at {url}", token)
    finally:
        runner(["onex", "dashboard", "stop", "--json"], 60, env=env)
