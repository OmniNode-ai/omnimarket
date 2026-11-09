# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The delegation lane scenario R8 drives: the two backends behind one protocol (OMN-23175).

``InMemoryLane`` is the default: a typed model of a lane's projection, with the one switch the scenario tests, whether a
synthetic run is counted in the totals. ``HttpLane`` is the real backend: it sends runs with the ``onex delegate`` command and reads
the probe tenant's totals and recent runs from the tenant API. Every setting of the real backend (the endpoints, the keys of the
responses, the environment variables that hold the address and the key) is declared in the node contract's ``lane`` block and
read here, never written in code. A response that lacks a declared key is a typed ``LaneReadError``, never a default of zero.
"""

from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

HttpGet = Callable[[str, Mapping[str, str], float], object]
CommandRunner = Callable[[Sequence[str], float], tuple[int, str]]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelTotals(_Frozen):
    """The four totals a tenant sees: runs, tokens, spend and savings."""

    runs: int = Field(ge=0)
    tokens: int = Field(ge=0)
    spend_usd: Decimal = Field(ge=0)
    savings_usd: Decimal = Field(ge=0)


class ModelRunRow(_Frozen):
    """One row of the tenant's Runs list."""

    correlation_id: str = Field(min_length=1)
    traffic_class: str = Field(min_length=1)
    tokens: int = Field(ge=0)
    cost_usd: Decimal = Field(ge=0)
    saved_usd: Decimal = Field(ge=0)


class ModelSendRequest(_Frozen):
    """One run to send: the fixed task and, for a synthetic run, its traffic class and source surface."""

    task_type: str = Field(min_length=1)
    prompt: str = Field(min_length=1)
    traffic_class: str | None = None
    source_surface: str | None = None


class LaneReadError(RuntimeError):
    """The lane could not answer, or answered without a value the scenario needs. Always a failure, never a skip."""


class ProtocolR8Lane(Protocol):
    def read_totals(self) -> ModelTotals: ...

    def read_runs(self) -> tuple[ModelRunRow, ...]: ...

    def send(self, request: ModelSendRequest) -> str:
        """Send one run and return its correlation id."""
        ...


class ModelLaneSettings(_Frozen):
    """The contract's ``lane`` block: where the real backend reads and what it calls things."""

    backend: str = Field(pattern=r"^(inmemory|http)$")
    base_url_env: str = Field(min_length=1)
    api_key_env: str = Field(min_length=1)
    api_key_header: str = Field(min_length=1)
    totals_path: str = Field(min_length=1)
    runs_path: str = Field(min_length=1)
    runs_limit: int = Field(ge=2)
    request_timeout_seconds: float = Field(gt=0)
    delegate_command: tuple[str, ...] = Field(min_length=1)
    delegate_timeout_seconds: float = Field(gt=0)
    #: Names in the totals response: ``runs``, ``tokens``, ``spend_usd``, ``savings_usd``.
    totals_keys: Mapping[str, str]
    #: Names in a run row: ``correlation_id``, ``traffic_class``, ``tokens``, ``cost_usd``, ``saved_usd``.
    run_keys: Mapping[str, str]
    #: Key of the list of rows in the runs response.
    runs_list_key: str = Field(min_length=1)
    #: Key of the correlation id in the ``onex delegate --json`` output.
    delegate_id_key: str = Field(min_length=1)


TOTALS_NAMES = ("runs", "tokens", "spend_usd", "savings_usd")
RUN_NAMES = ("correlation_id", "traffic_class", "tokens", "cost_usd", "saved_usd")


class InMemoryLane:
    """A typed model of a lane's projection. ``count_synthetic`` is the break the failing control turns on."""

    def __init__(
        self,
        *,
        count_synthetic: bool = False,
        run_tokens: int = 120,
        run_cost_usd: Decimal = Decimal("0.000040"),
        run_saved_usd: Decimal = Decimal("0.001200"),
        label_synthetic: bool = True,
        list_synthetic: bool = True,
    ) -> None:
        self._count_synthetic = count_synthetic
        self._tokens = run_tokens
        self._cost = run_cost_usd
        self._saved = run_saved_usd
        self._label_synthetic = label_synthetic
        self._list_synthetic = list_synthetic
        self._rows: list[ModelRunRow] = []
        self._sent = 0

    def _counted(self, row: ModelRunRow) -> bool:
        return row.traffic_class != "synthetic" or self._count_synthetic

    def read_totals(self) -> ModelTotals:
        counted = [row for row in self._rows if self._counted(row)]
        return ModelTotals(
            runs=len(counted),
            tokens=sum(row.tokens for row in counted),
            spend_usd=sum((row.cost_usd for row in counted), Decimal(0)),
            savings_usd=sum((row.saved_usd for row in counted), Decimal(0)),
        )

    def read_runs(self) -> tuple[ModelRunRow, ...]:
        return tuple(self._rows)

    def send(self, request: ModelSendRequest) -> str:
        self._sent += 1
        correlation_id = f"inmemory-run-{self._sent}"
        synthetic = request.traffic_class == "synthetic"
        if synthetic and not self._list_synthetic:
            return correlation_id
        label = request.traffic_class or "organic"
        if synthetic and not self._label_synthetic:
            label = "organic"
        self._rows.append(
            ModelRunRow(
                correlation_id=correlation_id,
                traffic_class=label,
                tokens=self._tokens,
                cost_usd=self._cost,
                saved_usd=self._saved,
            )
        )
        return correlation_id


def _urllib_get(url: str, headers: Mapping[str, str], timeout: float) -> object:
    request = urllib.request.Request(url, headers=dict(headers))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise LaneReadError(f"GET {url} failed: {type(exc).__name__}") from exc


def _subprocess_run(argv: Sequence[str], timeout: float) -> tuple[int, str]:
    try:
        done = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LaneReadError(f"{argv[0]} did not finish: {type(exc).__name__}") from exc
    return done.returncode, done.stdout


def _decimal(value: object, name: str) -> Decimal:
    try:
        return Decimal(str(value))
    except InvalidOperation as exc:
        raise LaneReadError(f"{name} is not a number") from exc


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise LaneReadError(f"{name} is not an integer")
    return value


def _pick(
    body: Mapping[str, object], keys: Mapping[str, str], names: Sequence[str]
) -> dict[str, object]:
    picked: dict[str, object] = {}
    for name in names:
        key = keys.get(name)
        if key is None:
            raise LaneReadError(f"the contract names no response key for {name}")
        if key not in body:
            raise LaneReadError(f"the response carries no {key} ({name})")
        picked[name] = body[key]
    return picked


class HttpLane:
    """The real backend: the tenant API for reads and the ``onex delegate`` command for sends."""

    def __init__(
        self,
        settings: ModelLaneSettings,
        environment: Mapping[str, str],
        *,
        http_get: HttpGet = _urllib_get,
        run_command: CommandRunner = _subprocess_run,
    ) -> None:
        self._settings = settings
        base = environment.get(settings.base_url_env, "").strip()
        key = environment.get(settings.api_key_env, "").strip()
        if not base:
            raise LaneReadError(f"{settings.base_url_env} is not set")
        if not key:
            raise LaneReadError(f"{settings.api_key_env} is not set")
        self._base = base.rstrip("/")
        self._headers = {settings.api_key_header: key}
        self._get = http_get
        self._run = run_command

    def _fetch(self, path: str) -> Mapping[str, object]:
        body = self._get(
            f"{self._base}{path}", self._headers, self._settings.request_timeout_seconds
        )
        if not isinstance(body, dict):
            raise LaneReadError(f"{path} did not answer an object")
        return body

    def read_totals(self) -> ModelTotals:
        body = self._fetch(self._settings.totals_path)
        got = _pick(body, self._settings.totals_keys, TOTALS_NAMES)
        return ModelTotals(
            runs=_integer(got["runs"], "runs"),
            tokens=_integer(got["tokens"], "tokens"),
            spend_usd=_decimal(got["spend_usd"], "spend_usd"),
            savings_usd=_decimal(got["savings_usd"], "savings_usd"),
        )

    def read_runs(self) -> tuple[ModelRunRow, ...]:
        body = self._fetch(
            f"{self._settings.runs_path}?limit={self._settings.runs_limit}"
        )
        listed = body.get(self._settings.runs_list_key)
        if not isinstance(listed, list):
            raise LaneReadError(
                f"the response carries no {self._settings.runs_list_key} list"
            )
        rows: list[ModelRunRow] = []
        for item in listed:
            if not isinstance(item, dict):
                raise LaneReadError("a run row is not an object")
            got = _pick(item, self._settings.run_keys, RUN_NAMES)
            rows.append(
                ModelRunRow(
                    correlation_id=str(got["correlation_id"]),
                    traffic_class=str(got["traffic_class"]),
                    tokens=_integer(got["tokens"], "tokens"),
                    cost_usd=_decimal(got["cost_usd"], "cost_usd"),
                    saved_usd=_decimal(got["saved_usd"], "saved_usd"),
                )
            )
        return tuple(rows)

    def send(self, request: ModelSendRequest) -> str:
        argv = [
            *self._settings.delegate_command,
            request.prompt,
            "--task-type",
            request.task_type,
            "--json",
        ]
        if request.traffic_class is not None:
            argv += ["--traffic-class", request.traffic_class]
        if request.source_surface is not None:
            argv += ["--source-surface", request.source_surface]
        code, out = self._run(argv, self._settings.delegate_timeout_seconds)
        try:
            parsed = json.loads(out)
        except ValueError as exc:
            raise LaneReadError(
                f"the delegate command (exit {code}) printed no JSON"
            ) from exc
        correlation_id = (
            parsed.get(self._settings.delegate_id_key)
            if isinstance(parsed, dict)
            else None
        )
        if not isinstance(correlation_id, str) or not correlation_id:
            raise LaneReadError(
                f"the delegate command (exit {code}) named no {self._settings.delegate_id_key}"
            )
        return correlation_id
