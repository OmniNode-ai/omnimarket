# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The session-start entry: open an orchestration session in one call.

In order, stopping at the first blocker with no CLAIM written and no ``session.env``:

1. the session lane (given, or ``<purpose>-<4 hex>``); a lane whose ``session.env`` exists is
   refused before anything runs;
2. the profile: ``operator`` only when the team identity declaration positively matches the
   session as the operator, ``team`` for everything else (an unknown, unavailable, mismatched or
   environment-only identity included);
3. the onex plugin's preflight runner with ``--intent tick --profile <profile>`` and the receipt
   in the session folder. A failed blocker returns that check's fix;
4. the required-check assertion on the receipt for the profile;
5. the host mode from the receipt and the bus configuration, decided before the first CLAIM;
6. the ledger digest and inbox counts (``onex-ledger catchup`` and ``inbox``);
7. the session CLAIM (``onex-ledger row CLAIM``). A refused append, or an append whose outcome is
   unknown (no ``OK CLAIM`` line), stops startup;
8. the session files: ``session.env`` (with the host mode, profile and principal) and ``lanes.txt``.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from omnimarket.models.session_scope import ModelIdentityClaim, ModelSessionScope

from ..models.model_session_front_door import (
    ModelHostModeVerdict,
    ModelReceiptAssertion,
    ModelSessionStartSpec,
    Profile,
    Status,
)
from . import receipt as rc
from . import session_files as sf

INBOX_RE = re.compile(r"^inbox \S+: (\d+) open, (\d+) need action")
EXIT_EMPTY_WINDOW = 3


@dataclass
class EntryOutcome:
    status: Status = "ok"
    exit_code: int = 0
    lane: str | None = None
    claim_ts: str | None = None
    receipt: str | None = None
    preflight_argv: tuple[str, ...] = ()
    assertion: ModelReceiptAssertion | None = None
    host_mode: ModelHostModeVerdict | None = None
    session_env: str | None = None
    lines: list[str] = field(default_factory=list)
    rule: str | None = None
    message: str = ""
    fix: str | None = None

    def stop(
        self, status: Status, code: int, rule: str, message: str, fix: str | None
    ) -> EntryOutcome:
        self.status, self.exit_code, self.rule, self.message, self.fix = (
            status,
            code,
            rule,
            message,
            fix,
        )
        return self


def resolve_preflight(
    spec: ModelSessionStartSpec, env: Mapping[str, str]
) -> Path | None:
    """--preflight_script, then ONEX_PREFLIGHT_SCRIPT, then the installed onex plugin's runner."""
    explicit = spec.preflight_script or env.get("ONEX_PREFLIGHT_SCRIPT")
    if explicit:
        return Path(explicit)
    home = env.get("HOME")
    if not home:
        return None
    registry = Path(home) / ".claude" / "plugins" / "installed_plugins.json"
    try:
        plugins = json.loads(registry.read_text(encoding="utf-8")).get("plugins", {})
    except (OSError, ValueError, AttributeError):
        return None
    candidates: list[tuple[int, str]] = []
    for key, installs in plugins.items() if isinstance(plugins, dict) else ():
        if not str(key).startswith("onex@"):
            continue
        for install in installs if isinstance(installs, list) else [installs]:
            path = install.get("installPath") if isinstance(install, dict) else None
            if path:
                candidates.append((0 if key == "onex@omninode-tools" else 1, str(path)))
    for _, path in sorted(candidates):
        script = Path(path) / "scripts" / "session_preflight.py"
        if script.is_file():
            return script
    return None


def resolve_ledger_command(
    spec: ModelSessionStartSpec, env: Mapping[str, str]
) -> tuple[str, ...] | None:
    if spec.ledger_command:
        return spec.ledger_command
    try:
        argv = tuple(shlex.split(env.get("ONEX_LEDGER_COMMAND", "")))
    except ValueError:
        return None
    return argv or None


def _run(
    argv: Sequence[str], env: Mapping[str, str], timeout: int, detail: Path
) -> tuple[int | None, list[str]]:
    """Run one command; append its output to the detail file. ``None`` when it timed out."""
    with detail.open("a", encoding="utf-8") as fh:
        fh.write("$ " + shlex.join(argv) + "\n")
    try:
        done = subprocess.run(
            list(argv),
            env=dict(env),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        with detail.open("a", encoding="utf-8") as fh:
            fh.write(f"timed out after {timeout}s\n")
        return None, []
    except OSError as exc:
        with detail.open("a", encoding="utf-8") as fh:
            fh.write(f"cannot run: {exc}\n")
        return 127, [f"cannot run {argv[0]}: {exc}"]
    output = (done.stdout or "") + (done.stderr or "")
    with detail.open("a", encoding="utf-8") as fh:
        fh.write(output)
    return done.returncode, output.splitlines()


def _fields(line: str) -> dict[str, str]:
    return dict(part.split("=", 1) for part in line.split() if "=" in part)


def _ledger_lines(lines: list[str]) -> str:
    kept = [
        ln
        for ln in lines
        if ln.startswith(("REFUSED ", "RETRY ", "FIX: ", "NOT-APPENDED "))
    ]
    return "; ".join(kept) or (lines[-1] if lines else "no output")


def run_entry(
    spec: ModelSessionStartSpec,
    *,
    claims: Sequence[ModelIdentityClaim],
    scope: ModelSessionScope,
    profile: Profile,
    env: Mapping[str, str],
    root: Path,
    max_age_seconds: int,
    expected_overlay_sha256: str | None,
) -> EntryOutcome:
    out = EntryOutcome()
    for name in ("OMNI_HOME", "ONEX_LEDGER_PATH"):
        if not env.get(name):
            return out.stop("refused", 2, "env", f"{name} is required", f"set {name}")
    try:
        sf.ticket(spec.ticket)
        lane = sf.token("lane", spec.lane) if spec.lane else sf.lane_name(spec.purpose)
    except sf.RefusedError as exc:
        return out.stop(
            "refused",
            64,
            "usage",
            str(exc),
            "pass --lane, or a --purpose of lowercase words",
        )
    out.lane = lane
    folder = root / lane
    if (folder / "session.env").exists():
        return out.stop(
            "refused",
            3,
            "lane-taken",
            f"{folder / 'session.env'} exists",
            "pick a new session lane name",
        )
    preflight = resolve_preflight(spec, env)
    if preflight is None or not preflight.is_file():
        return out.stop(
            "refused",
            2,
            "preflight-missing",
            "no session_preflight.py resolved (tried preflight_script, ONEX_PREFLIGHT_SCRIPT, "
            "installed_plugins.json)",
            "pass the onex plugin's scripts/session_preflight.py, or set ONEX_PREFLIGHT_SCRIPT",
        )
    ledger = resolve_ledger_command(spec, env)
    if ledger is None:
        return out.stop(
            "refused",
            2,
            "ledger-command",
            "no onex-ledger command: ledger_command and ONEX_LEDGER_COMMAND are both unset",
            "pass ledger_command, or set ONEX_LEDGER_COMMAND to the onex-ledger command line",
        )

    folder.mkdir(parents=True, exist_ok=True)
    detail = folder / "session-start.detail"
    receipt_path = folder / "preflight.json"
    receipt_path.unlink(missing_ok=True)
    out.receipt = str(receipt_path)

    # ---- preflight under the session's profile
    argv = [
        spec.python,
        str(preflight),
        "--intent",
        "tick",
        "--receipt",
        str(receipt_path),
    ]
    argv += ["--profile", profile]
    if spec.preflight_overlay:
        argv += ["--overlay", spec.preflight_overlay]
    out.preflight_argv = tuple(argv)
    code, lines = _run(argv, env, spec.preflight_timeout_seconds, detail)
    try:
        data = rc.read_receipt(receipt_path)
    except rc.ReceiptError as exc:
        return out.stop(
            "refused",
            2,
            "preflight",
            f"the run exited {code} and left no usable receipt ({exc}; {lines[-1] if lines else ''})",
            "repair what the message names (an overlay, usually), then rerun",
        )
    warnings = 0
    for check in data.get("checks") or []:
        if not isinstance(check, dict) or check.get("ok"):
            continue
        if check.get("severity") == "blocker":
            text = str(check.get("title"))
            if check.get("detail"):
                text += f" ({check['detail']})"
            return out.stop(
                "blocked", 1, str(check.get("check_id")), text, str(check.get("fix"))
            )
        warnings += 1
    state = "ok" if warnings == 0 else f"ok+{warnings}warn"

    # ---- the kept required-check assertion
    out.assertion = rc.assert_required_checks(
        data, profile, expected_overlay_sha256=expected_overlay_sha256
    )
    if not out.assertion.ok:
        return out.stop(
            "refused",
            2,
            "required-checks",
            "; ".join(out.assertion.findings),
            rc.STALE_OVERLAY_FIX,
        )

    # ---- host mode, before the first CLAIM
    out.host_mode = rc.decide_host_mode(
        data,
        profile=profile,
        receipt_path=receipt_path,
        claims=claims,
        scope=scope,
        env=env,
        max_age_seconds=max_age_seconds,
        expected_overlay_sha256=expected_overlay_sha256,
    )

    # ---- digest and inbox, as counts
    counts = {"open-claims": "0", "holds": "0", "rulings": "0"}
    budget = spec.ledger_timeout_seconds
    code, lines = _run(
        [*ledger, "catchup", "--hours", str(spec.hours), "--cap", "0"],
        env,
        budget,
        detail,
    )
    if code == 0:
        found = [ln for ln in lines if ln.startswith("catchup since=")]
        if not found:
            return out.stop(
                "refused",
                2,
                "onex-ledger-protocol",
                f"catchup printed no summary; detail={detail}",
                f"read {detail} and repair the reported failure before retrying",
            )
        counts.update({k: v for k, v in _fields(found[-1]).items() if k in counts})
    elif code != EXIT_EMPTY_WINDOW:
        return out.stop(
            "refused", code or 2, "catchup", _ledger_lines(lines), f"read {detail}"
        )
    code, lines = _run([*ledger, "inbox", lane, "--limit", "0"], env, budget, detail)
    inbox = [m for m in (INBOX_RE.match(ln) for ln in lines) if m]
    if code != 0 or not inbox:
        return out.stop(
            "refused", code or 2, "inbox", _ledger_lines(lines), f"read {detail}"
        )
    inbox_counts = f"{inbox[-1].group(1)}/{inbox[-1].group(2)}"

    # ---- the session CLAIM: a refused or unknown append stops startup
    claim = [
        *ledger, "row", "CLAIM", "--lane", lane, "--ticket", spec.ticket, "--actor", spec.actor,
        "--model", spec.model, "--est-hours", f"{spec.est_hours:g}", "--displaces", spec.displaces,
        "--scope", spec.scope, "--kv", "worktree=none", "--text", spec.text,
    ]  # fmt: skip
    code, lines = _run(claim, env, budget, detail)
    if code is None:
        return out.stop(
            "refused",
            2,
            "append-unknown",
            f"the CLAIM append timed out after {budget}s; its outcome is unknown",
            f"read {detail} and the ledger before retrying; never append a second CLAIM blind",
        )
    if code != 0:
        return out.stop(
            "refused", code, "append-refused", _ledger_lines(lines), f"read {detail}"
        )
    ok_lines = [ln for ln in lines if ln.startswith("OK CLAIM ")]
    if not ok_lines or len(ok_lines[-1].split()) < 3:
        return out.stop(
            "refused",
            2,
            "append-unknown",
            f"the CLAIM append printed no OK CLAIM line; detail={detail}",
            f"read {detail} and the ledger before retrying; never append a second CLAIM blind",
        )
    out.claim_ts = ok_lines[-1].split()[2]

    # ---- the session files
    try:
        body = sf.session_env_body(
            lane=lane, claim_ts=out.claim_ts, goal_ticket=spec.ticket,
            host_mode=out.host_mode.mode, scope=scope, env=env,
        )  # fmt: skip
        _, path = sf.init(root, lane, body, dry_run=False)
    except sf.RefusedError as exc:
        return out.stop(
            "refused", 2, "session-init", str(exc),
            f"the CLAIM at {out.claim_ts} is written; run init by hand with that stamp",
        )  # fmt: skip
    out.session_env = str(path)
    out.lines.append(
        f"SESSION lane={lane} claim={out.claim_ts} host={out.host_mode.mode} profile={profile} "
        f"principal={scope.writer_principal or '-'} preflight={state} "
        f"open-claims={counts['open-claims']} holds={counts['holds']} rulings={counts['rulings']} "
        f"inbox={inbox_counts} env={path} detail={detail}"
    )
    return out
