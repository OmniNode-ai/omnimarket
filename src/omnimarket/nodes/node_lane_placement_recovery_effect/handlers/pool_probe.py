# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The pool probe's cache and the launcher-gap verdicts of the lane placement recovery (OMN-21809, OMN-17427).

A pool host's probe that ends on its timeout under concurrent readers is not read UNREADABLE while the host's last good
reading is young (``pool_reading``): the text of each good probe is kept with its read time, one file per host so concurrent
readers never share a read-modify-write, and a probe that ends on its timeout answers with that text while it is younger than
the placement contract's ``pool_reading_max_age_s``. A cached answer is never stored again, so it never refreshes its own age.

A host this launching host cannot reach, or cannot log in to, is not probed on every tick (``launcher_gap``): a probe that fails
with a launcher-side cause (the name does not resolve, the login is refused, nothing routes, sshd refuses) is kept with its time
and its owner action in the placement directory, one file per host, and the host is not probed again until the placement
contract's ``launcher_gap_cooldown_s`` for that cause has passed. A host-key refusal and the probe's own timeout are not launcher
gaps. The file is this launching host's own state: the shared host table is never touched.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

RECEIPT_WINDOW = timedelta(
    hours=24
)  # the lane receipts the throughput tick reads: those in lane directories written this recently


POOL_READINGS_DIR = "pool-readings"
GAP_COOLDOWN_KEY = "launcher_gap_cooldown_s"
PROBE_TIMEOUT_NOTE = "timed out after"
POOL_READING_MAX_AGE_KEY = "pool_reading_max_age_s"
PROBE_LINE = re.compile(r"^PROBE \d+ \S+ \d+$", re.MULTILINE)


def probe_capture(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    """Run ``argv`` for at most ``timeout`` seconds; a timeout or a start failure is an answer (rc 255), never a raise."""
    try:
        return subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            argv, 255, "", f"{PROBE_TIMEOUT_NOTE} {timeout:.0f}s"
        )
    except OSError as exc:
        return subprocess.CompletedProcess(argv, 255, "", str(exc))


def probe_timed_out(done: subprocess.CompletedProcess[str]) -> bool:
    return done.returncode == 255 and (done.stderr or "").startswith(PROBE_TIMEOUT_NOTE)


# Launcher-side causes, by the same stderr fragments as node_host_pool_alarm_effect's TEXT_RULES (a test holds the two together), each with
# the action its owner takes. A host-key refusal is not here (the next read repairs it in code) and neither is the probe's own timeout.
GAP_RULES: tuple[tuple[str, tuple[str, ...], str], ...] = (
    (
        "NAME-UNRESOLVED",
        (
            "could not resolve hostname",
            "name or service not known",
            "nodename nor servname",
            "no address associated with hostname",
            "temporary failure in name resolution",
        ),
        "the launching host's owner adds an ssh alias or DNS entry that resolves the row's target, or the host's owner wakes it",
    ),
    (
        "AUTH-REFUSED",
        (
            "permission denied",
            "too many authentication failures",
            "no supported authentication methods",
        ),
        "the host's owner adds this launching host's public key to the account's authorized_keys",
    ),
    (
        "SSHD-REFUSED",
        ("connection refused",),
        "the host's owner starts sshd, or allows this launching host's address",
    ),
    (
        "NO-ROUTE",
        (
            "no route to host",
            "network is unreachable",
            "operation timed out",
            "connection timed out",
            "host is down",
            "connection closed by remote host",
            "stdio forwarding failed",
        ),
        "the launching host's owner adds a route, jump host or ssh alias that reaches the row's target",
    ),
)
BANNER_SLOW = "banner exchange"  # ssh reached the host and the banner was late: the host's load, not a gap


def gap_cause(done: subprocess.CompletedProcess[str]) -> tuple[str, str] | None:
    """(cause, owner action) when ``done`` is a failed ssh read that never reached the host from this launcher, else None."""
    if done.returncode == 0 or probe_timed_out(done):
        return None
    text = (done.stderr or "").lower()
    if BANNER_SLOW in text:
        return None
    return next(
        (
            (cause, owner)
            for cause, fragments, owner in GAP_RULES
            if any(f in text for f in fragments)
        ),
        None,
    )


# --- the destination a host row is reached at, from this launching host (fallback_targets, OMN-17427) ----------------
#
# A row's ``target`` is the address or alias the lab's own launchers use. A launching host that is not on the lab network,
# or that has no alias for it, cannot route to it at all: on 2026-11-02 this launcher sat on another subnet and every probe
# of a lab host row timed out, while the hosts themselves were up and carrying lanes for other launchers. A row may
# therefore list further ssh destinations for the same machine (``fallback_targets``, a tailnet peer or another alias), and
# a launcher uses the first one whose sshd answers. The committed ``target`` is never replaced and no shared state is
# written: the choice is this process's, kept for ``ANSWERED_TTL_S`` so one pool read asks once, and a row with no
# fallbacks is probed for nothing. Reaching a destination is not being allowed in: a host that answers and then refuses
# this launcher's key reads as AUTH-REFUSED, whose owner action is the host owner's, not a routing problem.

#: Reach sshd and offer no credential (``none`` is the only method tried), write no known_hosts entry.
ANSWER_PROBE_OPTS: tuple[str, ...] = (
    "-o",
    "BatchMode=yes",
    "-o",
    "ConnectTimeout=4",
    "-o",
    "PreferredAuthentications=none",
    "-o",
    "StrictHostKeyChecking=no",
    "-o",
    "UserKnownHostsFile=/dev/null",
    "-o",
    "ControlPath=none",
    "-o",
    "LogLevel=ERROR",
)
#: "Permission denied" or a host-key refusal proves sshd answered; a timeout, an unresolved name or a refused connection does not.
SSHD_ANSWERED_RE = re.compile(
    r"permission denied|host key verification failed", re.IGNORECASE
)
ANSWER_PROBE_TIMEOUT_S = 10.0
ANSWERED_TTL_S = 300.0
#: (row name, target, fallbacks) -> (the destination that answered, when it was decided). A long-lived controller re-decides after the TTL.
ANSWERED: dict[tuple[str, str, tuple[str, ...]], tuple[str, float]] = {}
_ANSWER_LOCKS: dict[tuple[str, str, tuple[str, ...]], threading.Lock] = {}


class HostRow(Protocol):
    """What resolving a destination needs of a host row (the pool's ``Host``). ``fallback_targets`` is read with a default,
    because a caller's own host double predates the column and a row without it must read exactly as it did."""

    @property
    def name(self) -> str: ...

    @property
    def target(self) -> str | None: ...

    @property
    def local(self) -> bool: ...


def fallback_names(value: Any) -> tuple[str, ...]:
    """A row's ``fallback_targets`` as names: an inline ``[a, b]``, a comma string or a list; blanks and repeats dropped."""
    if isinstance(value, str):
        value = value.strip().removeprefix("[").removesuffix("]").split(",")
    names = (
        (str(n).strip().strip("'\"") for n in value)
        if isinstance(value, (list, tuple))
        else ()
    )
    return tuple(dict.fromkeys(n for n in names if n))


def row_fallbacks(host: HostRow) -> tuple[str, ...]:
    """A host row's declared ``fallback_targets``, or none when it has no such column."""
    return tuple(getattr(host, "fallback_targets", ()) or ())


def sshd_answered(done: subprocess.CompletedProcess[str]) -> bool:
    """True when the credential-free probe reached the destination's sshd (it logged in, or sshd refused the credential)."""
    return done.returncode == 0 or bool(SSHD_ANSWERED_RE.search(done.stderr or ""))


def resolve_target(
    host: HostRow,
    *,
    capture: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    bound: Callable[[float], float] = float,
    now: float | None = None,
) -> str:
    """The ssh destination for ``host``: its ``target`` first, then each ``fallback_targets`` name in order, the first
    whose sshd answers. A row with no fallbacks returns ``target`` with no probe, so its commands are unchanged; when
    nothing answers the row's ``target`` comes back and the failure reads as it always did. The answer is kept for
    ``ANSWERED_TTL_S``, so the ping and the probe of one pool read ask together and one of them decides."""
    assert host.target is not None
    fallbacks = row_fallbacks(host)
    if not fallbacks or host.local:
        return host.target
    key = (host.name, host.target, fallbacks)
    with _ANSWER_LOCKS.setdefault(key, threading.Lock()):
        clock = time.monotonic() if now is None else now
        held = ANSWERED.get(key)
        if held is not None and clock - held[1] < ANSWERED_TTL_S:
            return held[0]
        run = capture or probe_capture
        answered = next(
            (
                name
                for name in (host.target, *fallbacks)
                if sshd_answered(
                    run(
                        ["ssh", *ANSWER_PROBE_OPTS, name, "true"],
                        timeout=bound(ANSWER_PROBE_TIMEOUT_S),
                    )
                )
            ),
            host.target,
        )
        ANSWERED[key] = (answered, clock)
        return answered


def answered_target(host: HostRow) -> str | None:
    """The destination ``resolve_target`` last chose for ``host``, or None when it has not decided (always for a row with no fallbacks)."""
    held = ANSWERED.get((host.name, host.target or "", row_fallbacks(host)))
    return None if held is None else held[0]


def target_note(host: HostRow) -> str:
    """`` via=<name>`` when a fallback answered in place of the row's ``target``, else empty."""
    via = answered_target(host)
    return f" via={via}" if via is not None and via != host.target else ""


@dataclass(frozen=True)
class ModelLauncherGap:
    """A launcher-side verdict: the cause, the owner action, the seconds since the probe that found it, the cooldown that
    bounds it and what the probe said. ``line`` is the one-line form a Reading prints."""

    cause: str
    owner: str
    age_s: float
    cooldown_s: float
    detail: str
    probed: bool = False  # True: this very read found it; False: a kept verdict stood in for a probe

    @property
    def line(self) -> str:
        when = (
            "probed now"
            if self.probed
            else f"probed {self.age_s:.0f}s ago, next probe in {max(0.0, self.cooldown_s - self.age_s):.0f}s"
        )
        return f"LAUNCHER-GAP:{self.cause} {when}; owner: {self.owner}"[:200]


@dataclass(frozen=True)
class ModelPoolReadingRequest:
    """One host's live probe answer, the placement directory its last good text is kept in (None: keep none), the read time
    (epoch seconds) and the placement contract file that declares ``pool_reading_max_age_s``."""

    directory: str | None
    host: str
    done: subprocess.CompletedProcess[str]
    now: float
    contract: str
    target: str | None = (
        None  # the row's ssh destination: a launcher-gap verdict is kept for this target only (None: none is kept)
    )


@dataclass(frozen=True)
class ModelPoolReadingResult:
    """The answer to parse: the live one, or the kept one (return code 0) with ``cached_age_s`` set to its age in seconds;
    ``gap`` is the launcher-side verdict when the answer is a failure of that kind."""

    done: subprocess.CompletedProcess[str]
    cached_age_s: float | None = None
    gap: ModelLauncherGap | None = None


def pool_reading_bound(contract: Path) -> float | None:
    """The contract's declared bound; None when it declares none. A declared bound that is not a positive number raises."""
    try:
        doc = json.loads(contract.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"placement contract unreadable: {contract}: {exc}") from exc
    bound = doc.get(POOL_READING_MAX_AGE_KEY) if isinstance(doc, dict) else None
    if bound is not None and (
        isinstance(bound, bool) or not isinstance(bound, (int, float)) or bound <= 0
    ):
        raise ValueError(
            f"placement contract malformed: {contract}: {POOL_READING_MAX_AGE_KEY} must be a positive number of seconds"
        )
    return None if bound is None else float(bound)


def gap_cooldowns(contract: Path) -> dict[str, float]:
    """The contract's cooldown per launcher-gap cause; empty when it declares none. A malformed declaration raises."""
    try:
        doc = json.loads(contract.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"placement contract unreadable: {contract}: {exc}") from exc
    declared = doc.get(GAP_COOLDOWN_KEY) if isinstance(doc, dict) else None
    if declared is None:
        return {}
    known = {cause for cause, _, _ in GAP_RULES}
    if (
        not isinstance(declared, dict)
        or not declared
        or set(declared) - known
        or not all(
            isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0
            for v in declared.values()
        )
    ):
        raise ValueError(
            f"placement contract malformed: {contract}: {GAP_COOLDOWN_KEY} must map causes of {sorted(known)} to positive seconds"
        )
    return {str(k): float(v) for k, v in declared.items()}


def _store(directory: str, host: str, suffix: str) -> Path:
    return (
        Path(directory)
        / POOL_READINGS_DIR
        / f"{re.sub(r'[^A-Za-z0-9_.-]', '_', host)}{suffix}"
    )


def launcher_gap(request: ModelPoolReadingRequest) -> ModelLauncherGap | None:
    """The kept launcher-side verdict for the request's host while it is younger than its cause's cooldown and the request's
    ``target`` is the one it was found for, else None (the host is probed). No directory, no target, no declared cooldown or an
    unreadable file is None."""
    if request.directory is None or not request.target:
        return None
    try:
        kept = json.loads(
            _store(request.directory, request.host, ".gap.json").read_text(
                encoding="utf-8"
            )
        )
        cause, age, detail, owner = (
            str(kept["cause"]),
            request.now - float(kept["at"]),
            str(kept["detail"]),
            str(kept["owner"]),
        )
        cooldown = gap_cooldowns(Path(request.contract)).get(cause)
        if (
            kept["target"] != request.target
            or cooldown is None
            or not 0 <= age < cooldown
        ):
            return None
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return ModelLauncherGap(cause, owner, age, cooldown, detail)


def _keep_gap(
    request: ModelPoolReadingRequest, gap: tuple[str, str]
) -> ModelLauncherGap | None:
    """Keep the verdict this probe found and return it; None when no cooldown is declared for the cause (nothing is kept)."""
    assert request.directory is not None
    cooldown = gap_cooldowns(Path(request.contract)).get(gap[0])
    if cooldown is None:
        return None
    detail = (request.done.stderr or request.done.stdout or "").strip()[:100]
    path = _store(request.directory, request.host, ".gap.json")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(
            json.dumps(
                {
                    "host": request.host,
                    "target": request.target,
                    "cause": gap[0],
                    "owner": gap[1],
                    "at": request.now,
                    "detail": detail,
                }
            ),
            encoding="utf-8",
        )
        os.replace(tmp, path)
    except OSError:
        pass  # a verdict that cannot be kept costs only the next tick its probe
    return ModelLauncherGap(gap[0], gap[1], 0.0, cooldown, detail, probed=True)


def pool_probe(
    request: ModelPoolReadingRequest,
    run: Callable[[], subprocess.CompletedProcess[str]],
) -> ModelPoolReadingResult:
    """One host's read: a kept launcher-side verdict still inside its cooldown answers without running ``run`` (rc 255 and the
    probe's own words, so the cause reads as it did), else ``run`` is probed and ``pool_reading`` judges the answer."""
    if (gap := launcher_gap(request)) is not None:
        return ModelPoolReadingResult(
            subprocess.CompletedProcess([], 255, "", gap.detail), gap=gap
        )
    return pool_reading(replace(request, done=run()))


def pool_reading(request: ModelPoolReadingRequest) -> ModelPoolReadingResult:
    """A good probe answer is kept and returned; a timed-out one is replaced by the kept text when it is under the bound; a
    launcher-side failure is kept as a verdict (``launcher_gap``) and a good answer ends it."""
    live = ModelPoolReadingResult(request.done)
    if request.directory is None:
        return live
    path = _store(request.directory, request.host, ".json")
    if request.done.returncode == 0 and PROBE_LINE.search(request.done.stdout or ""):
        try:
            _store(request.directory, request.host, ".gap.json").unlink(missing_ok=True)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(
                f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
            )
            tmp.write_text(
                json.dumps(
                    {
                        "host": request.host,
                        "read_at": request.now,
                        "text": request.done.stdout,
                    }
                ),
                encoding="utf-8",
            )
            os.replace(tmp, path)
        except OSError:
            pass  # a reading that cannot be kept costs only the next timeout its fallback
        return live
    if request.target and (gap := gap_cause(request.done)) is not None:
        return replace(live, gap=_keep_gap(request, gap))
    if (
        not probe_timed_out(request.done)
        or (bound := pool_reading_bound(Path(request.contract))) is None
    ):
        return live
    try:
        kept = json.loads(path.read_text(encoding="utf-8"))
        text, age = kept["text"], request.now - float(kept["read_at"])
    except (OSError, ValueError, KeyError, TypeError):
        return live
    if not isinstance(text, str) or not 0 <= age < bound:
        return live
    return ModelPoolReadingResult(
        subprocess.CompletedProcess(request.done.args, 0, text, ""), age
    )


def recent_lane_receipts(lanes_dir: Path, since: datetime) -> list[Path]:
    """The runner's lane receipt files (``<lane>/<run>.json``) in lane directories written at or after ``since`` (OMN-19815).

    The runner writes a receipt or job record through ``write_receipt`` (a temp file renamed over the name), and a rename into a
    directory moves that directory's mtime, so a lane directory untouched since ``since`` holds no receipt written since. On
    2026-10-29 the directory held 43,777 receipts in 15,508 lane directories, 315 written in the last day, and each tick opened
    all of them three times and took over 100 s. A receipt started at or after ``since`` was written at or after it, so it is
    always listed; the readers filter on ``started_at`` themselves."""
    floor = since.timestamp()
    out: list[Path] = []
    try:
        with os.scandir(lanes_dir) as entries:
            for entry in entries:
                try:
                    if (
                        not entry.name.startswith(".")
                        and entry.is_dir()
                        and entry.stat().st_mtime >= floor
                    ):
                        out.extend(sorted(Path(entry.path).glob("*.json")))
                except OSError:
                    continue
    except OSError:
        return []
    return out
