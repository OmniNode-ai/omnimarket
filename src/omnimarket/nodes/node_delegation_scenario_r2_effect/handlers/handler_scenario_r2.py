# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
# unimported-handler-allow: HandlerScenarioR2 and HandlerSubprocessCommand are built by the node CLI factory with the contract runtime block, not routed by handler_routing or imported by name
"""Scenario R2, a local model added after onboarding (OMN-23172, readiness plan 2.2).

One run, in a fresh home directory:

1. onboard with one key: ``onex models add <provider> --model <m>`` (the key on stdin);
2. a URL that does not answer is refused by ``add local`` and writes no route (the falsifier of the add);
3. ``onex models add local --url <first> --model <id>``;
4. ``onex models list --json`` names the key's provider and the local provider with the model id;
5. ``onex delegate ... --backend-id local-url --json``: the accepted attempt's backend and model are the local ones;
6. a second ``add local`` and ``onex models list --json``: both local models are listed, in the order added.

Every assertion compares fields or bytes of the command output; no model judges a pass. A setting that is missing, a
command that cannot run and an exception are failures, never a skip. The result goes to the ledger first (one STATUS row,
the row the green-days reading counts) and then to the bus; a bus emit that fails raises after the row is written.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import socket
import subprocess
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

import yaml
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.handlers.delegation_scenario_runtime import (
    LedgerAppender,
    ModelScenarioSchedule,
    ProtocolScenarioEventSink,
    build_event_sink,
    bus_appender,
)
from omnimarket.models.model_delegation_scenario import (
    SCENARIO_ID_PATTERN,
    EnumScenarioCadence,
    EnumScenarioConclusion,
    EnumScenarioTrigger,
    ModelScenarioR2RunCommand,
    ModelScenarioResult,
    floor_to_slot,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    CommandLedgerAppender,
)

CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"
NODE_NAME = "node_delegation_scenario_r2_effect"
NODE_MODULE = f"omnimarket.nodes.{NODE_NAME}"
DETAIL_LIMIT = 240

Clock = Callable[[], datetime]


class ModelOnboarding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    provider_env: str = Field(min_length=1)
    provider_url_env: str = Field(min_length=1)
    key_env: str = Field(min_length=1)
    model_env: str = Field(min_length=1)


class ModelScenarioR2Runtime(BaseModel):
    """The contract's ``runtime`` block."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    home_env: str = Field(min_length=1)
    ledger_append_command_env: str = Field(min_length=1)
    ledger_append_timeout_seconds: int = Field(ge=1)
    host_env: str = Field(min_length=1)
    bus_emit_command_env: str = Field(min_length=1)
    event_journal_relpath: str = Field(min_length=1)
    bus_emit_timeout_seconds: int = Field(ge=1)
    scenario_id: str = Field(pattern=SCENARIO_ID_PATTERN)
    cadence: EnumScenarioCadence
    ticket: str = Field(pattern=r"^OMN-\d+$")
    lane: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    work_relpath: str = Field(min_length=1)
    onex_command: tuple[str, ...] = Field(min_length=1)
    command_timeout_seconds: int = Field(ge=1)
    onboarding: ModelOnboarding
    local_first_env: str = Field(min_length=1)
    local_second_env: str = Field(min_length=1)
    dead_url_env: str = Field(min_length=1)
    local_backend_id: str = Field(min_length=1)
    delegate_task_type: str = Field(min_length=1)
    delegate_prompt: str = Field(min_length=1)
    #: Transport flags of the pinned delegation: a customer machine runs it in process, with no shared bus.
    delegate_args: tuple[str, ...] = ()


class ModelCommandOutcome(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    returncode: int
    stdout: str = ""
    stderr: str = ""


class ProtocolScenarioCommandRunner(Protocol):
    def run(
        self,
        argv: Sequence[str],
        *,
        stdin: str | None,
        home: Path,
        timeout_seconds: int,
    ) -> ModelCommandOutcome: ...


class HandlerSubprocessCommand:
    """The real backend: the command in an environment that holds only the scenario's home and the search path."""

    def __init__(self, environment: Mapping[str, str] | None = None) -> None:
        source = os.environ if environment is None else environment  # ONEX_FLAG_EXEMPT
        self._path = source.get("PATH", "")

    def run(
        self,
        argv: Sequence[str],
        *,
        stdin: str | None,
        home: Path,
        timeout_seconds: int,
    ) -> ModelCommandOutcome:
        try:
            done = subprocess.run(
                list(argv),
                input=stdin,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
                env={
                    "HOME": str(home),
                    "PATH": self._path,
                    # Receipts of the scenario's own delegations stay in its fresh home.
                    "ONEX_STATE_DIR": str(home / ".onex_state"),
                },
            )
        except FileNotFoundError as exc:
            return ModelCommandOutcome(returncode=127, stderr=str(exc))
        except subprocess.TimeoutExpired:
            return ModelCommandOutcome(returncode=124, stderr="timed out")
        return ModelCommandOutcome(
            returncode=done.returncode, stdout=done.stdout, stderr=done.stderr
        )


def _contract(path: Path) -> dict[str, object]:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError(f"{path} is not a contract")
    return loaded


def load_runtime(path: Path = CONTRACT_PATH) -> ModelScenarioR2Runtime:
    return ModelScenarioR2Runtime.model_validate(_contract(path)["runtime"])


def load_schedule(path: Path = CONTRACT_PATH) -> ModelScenarioSchedule:
    return ModelScenarioSchedule.model_validate(_contract(path)["schedule"])


def load_terminal_topic(path: Path = CONTRACT_PATH) -> str:
    return str(_contract(path)["terminal_event"])


def _documents(text: str) -> list[dict[str, object]]:
    """Every line of ``text`` that is a JSON object, in order."""
    found: list[dict[str, object]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            found.append(parsed)
    return found


def _listing(outcome: ModelCommandOutcome) -> dict[str, dict[str, object]] | None:
    """``onex models list --json`` as rows keyed by provider, or ``None`` when it did not list."""
    if outcome.returncode != 0:
        return None
    rows = {
        str(row["provider"]): row
        for row in _documents(outcome.stdout)
        if "provider" in row
    }
    return rows or None


def _local_models(
    listing: dict[str, dict[str, object]] | None,
) -> tuple[str, ...] | None:
    if listing is None or "local" not in listing:
        return None
    row = listing["local"]
    models = row.get("models")
    if not row.get("set_up") or not isinstance(models, list):
        return ()
    return tuple(str(model) for model in models)


def _attempts(node: object, depth: int = 0) -> list[dict[str, object]]:
    """The ``attempts`` list of a delegate receipt, wherever the receipt nests its terminal payload."""
    if depth > 6:
        return []
    if isinstance(node, dict):
        attempts = node.get("attempts")
        if isinstance(attempts, list):
            return [a for a in attempts if isinstance(a, dict)]
        for value in node.values():
            found = _attempts(value, depth + 1)
            if found:
                return found
    return []


def _accepted_attempt(outcome: ModelCommandOutcome) -> dict[str, object] | None:
    """The attempt the quality gate accepted, from the receipt a delegate printed; ``None`` when there is none."""
    for document in reversed(_documents(outcome.stdout)):
        for attempt in _attempts(document):
            if attempt.get("acceptance_decision") == "accept":
                return attempt
    return None


def _pair(value: str) -> tuple[str, str] | None:
    url, _, model = value.partition("|")
    return (url.strip(), model.strip()) if url.strip() and model.strip() else None


class HandlerScenarioR2:
    """Run scenario R2 once, write its STATUS row and emit its result on the bus."""

    def __init__(
        self,
        runtime: ModelScenarioR2Runtime,
        *,
        home: Path,
        environment: Mapping[str, str],
        runner: ProtocolScenarioCommandRunner,
        appender: LedgerAppender,
        sink: ProtocolScenarioEventSink,
        terminal_topic: str | None = None,
        trigger: EnumScenarioTrigger = EnumScenarioTrigger.SCHEDULE,
        clock: Clock = lambda: datetime.now(UTC),
    ) -> None:
        self._runtime = runtime
        self._home = home
        self._environment = environment
        self._runner = runner
        self._append = appender
        self._sink = sink
        self._topic = terminal_topic or load_terminal_topic()
        self._trigger = trigger
        self._clock = clock

    @property
    def work_home(self) -> Path:
        return self._home / self._runtime.work_relpath

    def handle(self, request: ModelScenarioR2RunCommand) -> ModelScenarioResult:
        return self.run()

    def run(self) -> ModelScenarioResult:
        now = self._clock()
        assertions: list[str] = []
        detail = ""
        try:
            detail = self._scenario(assertions)
        except Exception as exc:
            assertions.append("-unexpected_error")
            detail = f"{type(exc).__name__}: {exc}"
        held = all(item.startswith("+") for item in assertions) and bool(assertions)
        result = ModelScenarioResult(
            scenario_id=self._runtime.scenario_id,
            slot_start=floor_to_slot(now, self._runtime.cadence),
            trigger=self._trigger,
            conclusion=EnumScenarioConclusion.SUCCESS
            if held
            else EnumScenarioConclusion.FAILURE,
            recorded_at=now,
            assertions=tuple(assertions),
            detail=detail[:DETAIL_LIMIT],
        )
        self._append(
            result.to_status_row(
                lane=self._runtime.lane,
                ticket=self._runtime.ticket,
                actor=self._runtime.actor,
            )
        )
        self._sink.emit(self._topic, result.model_dump(mode="json"))
        return result

    # -- the scenario ---------------------------------------------------------------------------------------------

    def _settings(self) -> tuple[dict[str, str], list[str]]:
        runtime = self._runtime
        onboarding = runtime.onboarding
        names = {
            "provider": onboarding.provider_env,
            "key": onboarding.key_env,
            "provider_model": onboarding.model_env,
            "local_first": runtime.local_first_env,
            "local_second": runtime.local_second_env,
            "dead_url": runtime.dead_url_env,
        }
        values = {
            key: self._environment.get(name, "").strip() for key, name in names.items()
        }
        values["provider_url"] = self._environment.get(
            onboarding.provider_url_env, ""
        ).strip()
        missing = [name for key, name in names.items() if not values[key]]
        return values, missing

    def _scenario(self, assertions: list[str]) -> str:
        runtime = self._runtime
        settings, missing = self._settings()
        if missing:
            assertions.append("-settings_present")
            return "missing settings: " + ", ".join(missing)
        first, second = _pair(settings["local_first"]), _pair(settings["local_second"])
        if first is None or second is None:
            assertions.append("-settings_present")
            return "a local model setting is not <base url>|<model id>"
        assertions.append("+settings_present")

        self._fresh_home()
        secret = settings["key"]
        problems: list[str] = []

        def run(*args: str, stdin: str | None = None) -> ModelCommandOutcome:
            outcome = self._runner.run(
                [*runtime.onex_command, *args],
                stdin=stdin,
                home=self.work_home,
                timeout_seconds=runtime.command_timeout_seconds,
            )
            return outcome

        def check(name: str, held: bool, why: str = "") -> None:
            assertions.append(("+" if held else "-") + name)
            if not held and why:
                problems.append(f"{name}: {why}")

        empty = _listing(run("models", "list", "--json"))
        check(
            "fresh_home",
            empty is not None and not any(row.get("set_up") for row in empty.values()),
            "the home already held a provider",
        )

        onboard = [
            "models",
            "add",
            settings["provider"],
            "--model",
            settings["provider_model"],
            "--json",
        ]
        if settings["provider_url"]:
            onboard += ["--url", settings["provider_url"]]
        run(*onboard, stdin=secret + "\n")
        onboarded = (_listing(run("models", "list", "--json")) or {}).get(
            settings["provider"]
        )
        # What the scenario holds is the registration: the add also runs a test delegation through the bus, which a
        # fresh home without a lane identity cannot reach, so the add's exit status is not the assertion.
        listed_models = onboarded.get("models") if onboarded else None
        check(
            "onboarded",
            bool(onboarded and onboarded.get("set_up"))
            and isinstance(listed_models, list)
            and settings["provider_model"] in listed_models,
            f"provider row: {onboarded}",
        )

        dead = run(
            "models",
            "add",
            "local",
            "--url",
            settings["dead_url"],
            "--model",
            first[1],
            "--json",
        )
        after_dead = _local_models(_listing(run("models", "list", "--json")))
        check(
            "dead_url_refused",
            dead.returncode != 0,
            "a URL that does not answer was accepted",
        )
        check(
            "dead_url_wrote_no_route",
            after_dead == (),
            f"local models after: {after_dead}",
        )

        run("models", "add", "local", "--url", first[0], "--model", first[1], "--json")
        listing = _listing(run("models", "list", "--json"))
        check(
            "local_added",
            _local_models(listing) == (first[1],),
            f"local models: {_local_models(listing)}",
        )
        provider_row = (listing or {}).get(settings["provider"])
        check(
            "list_names_both",
            bool(provider_row and provider_row.get("set_up"))
            and _local_models(listing) == (first[1],),
            f"local models: {_local_models(listing)}",
        )

        delegated = run(
            "delegate",
            runtime.delegate_prompt,
            "--task-type",
            runtime.delegate_task_type,
            "--backend-id",
            runtime.local_backend_id,
            *runtime.delegate_args,
            "--json",
        )
        accepted = _accepted_attempt(delegated)
        check(
            "delegated_to_local_url",
            accepted is not None
            and accepted.get("backend_id") == runtime.local_backend_id
            and accepted.get("model_id") == first[1],
            f"exit {delegated.returncode}, accepted attempt: {accepted and accepted.get('backend_id')}",
        )

        run(
            "models", "add", "local", "--url", second[0], "--model", second[1], "--json"
        )
        both = _local_models(_listing(run("models", "list", "--json")))
        check(
            "both_listed_in_order",
            both == (first[1], second[1]),
            f"local models: {both}",
        )
        text = "; ".join(problems) or "every assertion held"
        return text.replace(secret, "***")

    def _fresh_home(self) -> None:
        target = self.work_home.resolve()
        if self._home.resolve() not in target.parents:
            raise RuntimeError("the scenario home is outside the registry home")
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True)


def build_runner(
    env: Mapping[str, str] | None = None,
    *,
    trigger: EnumScenarioTrigger = EnumScenarioTrigger.SCHEDULE,
    command: HandlerSubprocessCommand | None = None,
) -> HandlerScenarioR2:
    """The production wiring: the registry home, the bus commands and the scenario's settings from the environment."""
    environment = dict(os.environ if env is None else env)  # ONEX_FLAG_EXEMPT
    runtime = load_runtime()
    append_argv = shlex.split(environment.get(runtime.ledger_append_command_env, ""))
    if not append_argv:
        raise RuntimeError(
            f"{runtime.ledger_append_command_env} is not set; the result has no ledger route"
        )
    host = environment.get(runtime.host_env, "").strip() or socket.gethostname()
    home = Path(environment[runtime.home_env])
    return HandlerScenarioR2(
        runtime,
        home=home,
        environment=environment,
        runner=command or HandlerSubprocessCommand(environment),
        appender=bus_appender(
            CommandLedgerAppender(
                append_argv, runtime.lane, host, runtime.ledger_append_timeout_seconds
            )
        ),
        sink=build_event_sink(
            environment,
            command_env=runtime.bus_emit_command_env,
            journal=home / runtime.event_journal_relpath,
            requested_by=runtime.lane,
            timeout_seconds=runtime.bus_emit_timeout_seconds,
        ),
        trigger=trigger,
    )
