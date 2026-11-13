# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Piece 2's hourly canary (hosts and capacity, base-first plan section 3.5).

For every host the lab host table pools, one canary lane goes through the route lanes take: the
placement's pool read admits it or refuses it, the lane wrapper starts it, and it ends with a typed
result carrying the nonce it was sent and the host's free disk. The reading is WORKING only when
every pooled host ended typed (a typed busy refusal counts), every disk read is over the floor and,
where the overlay names an advertisement source, every pooled host's newest capacity advertisement
is fresh.
A parked host is listed with the table's reason and never fails it: a host the table marks out of
rotation, and a host the route's pool read refuses for a reason of the table or the load bar (it takes
no unit of the canary's kind, or it is over the bar), which ends ``parked`` with the refusal in the
``refused`` cell. A refusal for any other reason stays a typed busy and is named there too.

What it never does (the OMN-22538 pool-read lesson): a canary cut at its deadline, a host the pool
read could not reach, or a host the pool read never named (a busy exit with no line for the host)
is read as a failed host, never as free capacity or a typed busy refusal.

A host the pool read admitted whose lane wrapper then exits with the overlay's guard-missing code and
marker (its installed lane guard copy is absent) is a failed host named ``canary-guard-missing-<host>``;
the pool read's own refusal for a guard it lacks stays a typed busy refusal.

Named break: kill the canary's wrapper, so the result is not typed; the reading goes NOT_WORKING
with ``canary-untyped-<host>``.
"""

from __future__ import annotations

import json
import re
import shlex
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

from omnimarket.models.model_host_reading import (
    EnumCanaryEnding,
    EnumHostProbe,
    ModelCanaryHost,
    ModelCanaryHostReading,
    ModelCanaryRun,
    ModelProbeReading,
)
from omnimarket.models.model_reading import EnumReadingOutcome, EnumReadingVerdict
from omnimarket.nodes.node_host_reading_effect.overlay import (
    ModelHostCanaryProbeSettings,
)
from omnimarket.nodes.node_host_reading_effect.protocols import (
    ProtocolAdvertisementSource,
    ProtocolCanaryRunner,
    ProtocolHostInventory,
)
from omnimarket.nodes.node_reading_compute.handlers.handler_reading_compute import (
    reason_token,
)

_PROBE = EnumHostProbe.HOST_CANARY
HOST_PLACEHOLDER = "{host}"

#: The canary itself, run by the host's interpreter: argv is marker, nonce, then the disk paths.
#: It prints one typed line; a path it cannot read is skipped, and none read gives a null disk.
CANARY_SOURCE = (
    "import json,os,shutil,sys\n"
    "free=[]\n"
    "for p in sys.argv[3:]:\n"
    "    try:\n"
    "        free.append(shutil.disk_usage(os.path.expanduser(p)).free)\n"
    "    except OSError:\n"
    "        pass\n"
    "gb=round(min(free)/1024**3,2) if free else None\n"
    "print(sys.argv[1]+' '+json.dumps({'nonce':sys.argv[2],'free_disk_gb':gb}),flush=True)\n"
)

_SLOTS = re.compile(r"\bslots=(\d+)")
_REFUSAL_TAIL = re.compile(r"\s+load/core=.*$")


class CanaryRouteError(ValueError):
    """The route command is not configured on this host."""


def canary_argv(
    settings: ModelHostCanaryProbeSettings,
    environ: Mapping[str, str],
    host: str,
    nonce: str,
) -> list[str]:
    """The route's words with ``{host}`` filled, then the canary and its arguments."""
    template = environ.get(settings.run_command_env, "").strip()
    if not template:
        raise CanaryRouteError(
            f"configured-route-unset-{settings.run_command_env.lower()}"
        )
    words = [word.replace(HOST_PLACEHOLDER, host) for word in shlex.split(template)]
    if host not in words:
        raise CanaryRouteError("route-names-no-host-placeholder")
    return [
        *words,
        settings.canary_interpreter,
        "-c",
        CANARY_SOURCE,
        settings.result_marker,
        nonce,
        *settings.disk_paths,
    ]


def _typed_result(
    settings: ModelHostCanaryProbeSettings, text: str, nonce: str
) -> tuple[bool, float | None]:
    """(a typed result carrying this nonce was printed, its free disk)."""
    for line in text.splitlines():
        if not line.startswith(settings.result_marker + " "):
            continue
        try:
            body = json.loads(line[len(settings.result_marker) + 1 :])
        except ValueError:
            continue
        if not isinstance(body, dict) or body.get("nonce") != nonce:
            continue
        disk = body.get("free_disk_gb")
        return True, float(disk) if isinstance(disk, (int, float)) else None
    return False, None


def _pool_line(text: str, host: str, marker: str) -> str | None:
    needle = f"{host}: {marker}"
    return next((line for line in text.splitlines() if needle in line), None)


def _refusal_token(text: str, host: str, markers: tuple[str, ...]) -> str | None:
    """The first pool line that refuses this host for a listed reason, as one cell-safe token."""
    needle = f"{host}: "
    for line in text.splitlines():
        _, found, tail = line.partition(needle)
        if not found:
            continue
        if any(tail.startswith(marker) for marker in markers):
            return reason_token(_REFUSAL_TAIL.sub("", tail))
    return None


def _first_refusal_token(text: str, host: str, admitted_marker: str) -> str:
    """The reason the pool read gave this host in its first line that is not an admission."""
    needle = f"{host}: "
    for line in text.splitlines():
        _, found, tail = line.partition(needle)
        if found and tail.strip() and not tail.startswith(admitted_marker):
            return reason_token(_REFUSAL_TAIL.sub("", tail))
    return ""


def _pool_named(text: str, host: str) -> bool:
    """The route's pool read gave this host a line of its own (admitted or refused)."""
    needle = f"{host}: "
    return any(needle in line for line in text.splitlines())


def classify(
    settings: ModelHostCanaryProbeSettings,
    host: str,
    nonce: str,
    run: ModelCanaryRun,
) -> ModelCanaryHostReading:
    """One host's canary as a typed ending and the failures it gives the reading."""
    text = run.stdout + "\n" + run.stderr
    admitted = _pool_line(text, host, settings.pool_admitted_marker)
    slots_match = _SLOTS.search(admitted) if admitted else None
    slots = int(slots_match.group(1)) if slots_match else None
    typed, disk = _typed_result(settings, run.stdout, nonce)
    wrapper_ended = not settings.wrapper_end_marker or any(
        line.startswith(settings.wrapper_end_marker) for line in text.splitlines()
    )
    token = reason_token(host)
    refusal = ""
    if run.timed_out:
        ending = EnumCanaryEnding.TIMED_OUT
    elif _pool_line(text, host, settings.pool_unreadable_marker) is not None:
        ending = EnumCanaryEnding.UNREACHABLE
    elif (
        run.exit_code in settings.guard_missing_exit_codes
        and settings.guard_missing_marker in text
    ):
        # The pool read admitted the host and the lane wrapper's stub then found no installed copy
        # of the lane guard: no lane can start there, so the host fails and the row names the guard
        # (it read untyped before, which hid the cause: OMN-22869).
        ending = EnumCanaryEnding.GUARD_MISSING
    elif run.exit_code in settings.busy_exit_codes and (
        parked := _refusal_token(text, host, settings.pool_parked_markers)
    ):
        # The host table or the load bar keeps this host out: it is listed as parked with the
        # refusal and never fails the reading (a canary that fails there reads the host, not the
        # pipeline). Checked before busy, since the pool read may also have admitted the host.
        ending = EnumCanaryEnding.PARKED
        refusal = parked
    elif run.exit_code in settings.busy_exit_codes and _pool_named(text, host):
        ending = EnumCanaryEnding.BUSY
        refusal = _first_refusal_token(text, host, settings.pool_admitted_marker)
    elif run.exit_code in settings.busy_exit_codes:
        # The pool read never named the host (no advertisement reached it): the refusal is not
        # the host's own busy answer, so the host failed, never free or full capacity.
        ending = EnumCanaryEnding.UNREACHABLE
    elif run.exit_code == 0 and typed and wrapper_ended:
        ending = EnumCanaryEnding.TYPED
    else:
        ending = EnumCanaryEnding.UNTYPED
    failures: tuple[str, ...]
    if ending is EnumCanaryEnding.TYPED:
        if disk is None:
            failures = (f"disk-unread-{token}",)
        elif disk < settings.disk_floor_gb:
            failures = (f"disk-under-floor-{token}",)
        else:
            failures = ()
    elif ending in (EnumCanaryEnding.BUSY, EnumCanaryEnding.PARKED):
        failures = ()
    else:
        failures = (f"canary-{ending.value}-{token}",)
    return ModelCanaryHostReading(
        host=host,
        ending=ending,
        free_disk_gb=disk if ending is EnumCanaryEnding.TYPED else None,
        slots=slots,
        refusal=refusal,
        failures=failures,
    )


def _run_one(
    settings: ModelHostCanaryProbeSettings,
    runner: ProtocolCanaryRunner,
    environ: Mapping[str, str],
    host: str,
    nonce: str,
) -> ModelCanaryHostReading:
    argv = canary_argv(settings, environ, host, nonce)
    try:
        run = runner.run(argv, settings.run_timeout_seconds)
    except OSError as exc:
        run = ModelCanaryRun(exit_code=None, stderr=f"route-failed: {exc}")
    return classify(settings, host, nonce, run)


def _drill(settings: ModelHostCanaryProbeSettings) -> str:
    return "yes" if settings.drill else "no"


def _names(hosts: list[str]) -> str:
    return ",".join(hosts) if hosts else "none"


def advert_failures(
    settings: ModelHostCanaryProbeSettings,
    adverts: ProtocolAdvertisementSource | None,
    hosts: list[str],
    now: datetime,
) -> tuple[dict[str, str], dict[str, int], str]:
    """(failure reason by host, advertisement age by host, the advert cell).

    Not configured reads nothing and fails nothing; a configured source that cannot be read fails
    every pooled host, since placement would then read no capacity at all."""
    if settings.advertisements_command_env is None:
        return {}, {}, "not-configured"
    try:
        if adverts is None:
            raise OSError("no advertisement source wired")
        read = adverts.read()
    except (OSError, ValueError):
        return (
            {host: f"advert-unread-{reason_token(host)}" for host in hosts},
            {},
            "unread",
        )
    newest: dict[str, datetime] = {}
    for advert in read:
        stamp = advert.advertised_at.astimezone(UTC)
        if advert.host_name not in newest or stamp > newest[advert.host_name]:
            newest[advert.host_name] = stamp
    ages = {
        host: max(0, int((now - newest[host]).total_seconds()))
        for host in hosts
        if host in newest
    }
    failures: dict[str, str] = {}
    for host in hosts:
        if host not in ages:
            failures[host] = f"advert-missing-{reason_token(host)}"
        elif ages[host] > settings.advert_max_age_seconds:
            failures[host] = f"advert-stale-{reason_token(host)}"
    return failures, ages, "read"


def read_canary(
    settings: ModelHostCanaryProbeSettings,
    inventory: ProtocolHostInventory,
    runner: ProtocolCanaryRunner,
    environ: Mapping[str, str],
    nonce_for: Callable[[str], str],
    adverts: ProtocolAdvertisementSource | None = None,
    now: datetime | None = None,
) -> ModelProbeReading:
    try:
        table = inventory.hosts()
    except (OSError, ValueError):
        return ModelProbeReading(
            probe=_PROBE,
            verdict=EnumReadingVerdict.UNKNOWN,
            outcome=EnumReadingOutcome.SOURCE_UNREADABLE,
            reasons=("host-table-unreadable",),
            cells={"drill": _drill(settings)},
        )
    pooled: list[ModelCanaryHost] = [host for host in table if host.pooled]
    parked = [host.name for host in table if not host.pooled]
    if not pooled:
        return ModelProbeReading(
            probe=_PROBE,
            verdict=EnumReadingVerdict.NOT_WORKING,
            outcome=EnumReadingOutcome.READ,
            reasons=("no-pooled-host",),
            cells={
                "pooled": "none",
                "parked": _names(parked),
                "drill": _drill(settings),
            },
        )
    try:
        canary_argv(settings, environ, pooled[0].name, "check")
    except CanaryRouteError as exc:
        return ModelProbeReading(
            probe=_PROBE,
            verdict=EnumReadingVerdict.UNKNOWN,
            outcome=EnumReadingOutcome.SOURCE_UNREADABLE,
            reasons=(str(exc),),
            cells={"drill": _drill(settings)},
        )
    with ThreadPoolExecutor(max_workers=len(pooled)) as pool:
        readings = list(
            pool.map(
                lambda host: _run_one(
                    settings, runner, environ, host.name, nonce_for(host.name)
                ),
                pooled,
            )
        )
    names = [reading.host for reading in readings]
    advert_failed, ages, advert_state = advert_failures(
        settings, adverts, names, now or datetime.now(UTC)
    )
    failures = [
        reason
        for reading in readings
        for reason in (
            *reading.failures,
            *((advert_failed[reading.host],) if reading.host in advert_failed else ()),
        )
    ]
    failed_hosts = [r.host for r in readings if not r.passed or r.host in advert_failed]

    def by(ending: EnumCanaryEnding) -> str:
        return _names([r.host for r in readings if r.ending is ending])

    parked_by_run = [r.host for r in readings if r.ending is EnumCanaryEnding.PARKED]
    cells = {
        "pooled": _names([r.host for r in readings]),
        "typed": by(EnumCanaryEnding.TYPED),
        "busy": by(EnumCanaryEnding.BUSY),
        "failed_hosts": _names(failed_hosts),
        "parked": _names([*parked, *parked_by_run]),
        "refused": _names([f"{r.host}:{r.refusal}" for r in readings if r.refusal]),
        "free_disk_gb": _names(
            [
                f"{r.host}:{r.free_disk_gb:g}"
                for r in readings
                if r.free_disk_gb is not None
            ]
        ),
        "slots": _names(
            [f"{r.host}:{r.slots}" for r in readings if r.slots is not None]
        ),
        "disk_floor_gb": f"{settings.disk_floor_gb:g}",
        "advert": advert_state,
        "advert_age_s": _names([f"{host}:{age}" for host, age in sorted(ages.items())]),
        "drill": _drill(settings),
    }
    return ModelProbeReading(
        probe=_PROBE,
        verdict=EnumReadingVerdict.NOT_WORKING
        if failures
        else EnumReadingVerdict.WORKING,
        outcome=EnumReadingOutcome.READ,
        reasons=tuple(failures)
        or ("every-pooled-host-ended-typed-over-the-disk-floor",),
        cells=cells,
    )
