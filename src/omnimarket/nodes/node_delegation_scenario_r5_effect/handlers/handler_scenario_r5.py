# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Scenario R5, several OpenRouter models on one key in order (OMN-23183, readiness plan 2.2).

One run, in a fresh home on the real backend:

1. register two free OpenRouter models A and B on one key, in that order, through the model registry (the handler
   ``onex models add openrouter --model A --model B`` drives); the listing names A then B;
2. a delegation pinned to model A and one pinned to model B: each receipt names its model;
3. one delegation with a faults-mode probe file that makes model A answer 429: the history is A ``rate_limited`` and
   then B accepted;
4. falsifier of the add: an id the key does not list is refused and stores nothing;
5. falsifier of the order: after a reorder to B, A the same faults file makes the run answer on B with no attempt on A.

The market ingress does not yet accept a per-call ``model`` (OMN-20844 step 2), so step 2 is red, with the assertions
``pinned_model_accepted_a|b``, until that ticket lands: a red scenario is the expected colour, never a skip.

The model registry and the delegator are injected (``ProtocolModelRegistry``, ``ProtocolR5Delegator``): the in-memory default
scripts both, the real backend drives the registry's own add and list functions and the delegation skill handler. The paid
probe key is read from the variable the contract names, at run time only; an empty variable in the real backend is a red
run. Every assertion compares typed fields; no model judges a pass. The result goes to the ledger first (one STATUS row, the
row the green-days reading counts) and then to the bus.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, NoReturn, Protocol
from uuid import uuid4

import yaml
from omnibase_core.enums.enum_delegation_traffic_class import (
    EnumDelegationTrafficClass,
)
from omnibase_core.models.delegation.wire import ModelDelegationProvenance
from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.handlers import handler_model_keys as model_keys
from omnimarket.handlers.delegation_readiness_probe import seed_probe_tenant
from omnimarket.handlers.delegation_readiness_scenario import (
    ModelReadinessRuntime,
    ReadinessScenarioBase,
    build_scenario,
    read_contract,
)
from omnimarket.handlers.delegation_readiness_scenario import (
    load_schedule as load_schedule_from,
)
from omnimarket.handlers.delegation_scenario_runtime import (
    LedgerAppender,
    ModelScenarioSchedule,
    ProtocolScenarioEventSink,
)
from omnimarket.models.delegation.wire.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)
from omnimarket.models.delegation.wire.model_probe_faults import (
    ModelProbeFault,
    ModelProbeFaults,
)
from omnimarket.models.delegation.wire.model_probe_ladder import (
    READINESS_PROBE_SOURCE_SURFACE,
)
from omnimarket.models.model_delegation_scenario import (
    EnumScenarioTrigger,
    ModelScenarioResult,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
    HandlerDelegateSkill,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
    LocalDelegationDispatchPort,
)

NODE_DIR = Path(__file__).resolve().parents[1]
CONTRACT_PATH = NODE_DIR / "contract.yaml"
NODE_NAME = "node_delegation_scenario_r5_effect"
NODE_MODULE = f"omnimarket.nodes.{NODE_NAME}"
FRESH_HOME_DIRNAME = "home"
DB_FILENAME = "r5.sqlite"

Clock = Callable[[], datetime]


class ModelRegistryRefusedError(Exception):
    """The registry refused an add; nothing was stored. The text carries no key and no provider response."""


class ModelPinRefusedError(Exception):
    """A delegation pinned to a model was refused at the request, before any dispatch."""


class ModelR5Runtime(ModelReadinessRuntime):
    """The contract's ``runtime`` block: the shared keys, the leg backend of this scenario and the probe key's variable."""

    model_backend: Literal["real", "inmemory"]
    #: The variable that, when set in the unit's environment file to ``real`` or ``inmemory``, overrides ``model_backend``
    #: for that run; unset keeps the contract's value; any other value is a refusal. Read by ``build_runner`` only.
    model_backend_env: str = Field(min_length=1)
    #: The variable that carries the paid OpenRouter probe key; its value is read at run time only.
    openrouter_probe_key_env: str = Field(min_length=1)


class ModelR5Attempt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    model_id: str
    failure_class: str | None
    accepted: bool


class ModelR5Receipt(BaseModel):
    """The typed facts of one delegation terminal that R5 asserts on."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: str
    attempts: tuple[ModelR5Attempt, ...]
    final_answer: str

    @property
    def walked(self) -> tuple[str, ...]:
        return tuple(attempt.model_id for attempt in self.attempts)

    @property
    def accepted_model(self) -> str | None:
        for attempt in self.attempts:
            if attempt.accepted:
                return attempt.model_id
        return None


class ModelR5Inputs(BaseModel):
    """The committed inputs file of scenario R5."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str = Field(min_length=1)
    model_a: str = Field(min_length=1)
    model_b: str = Field(min_length=1)
    #: An id the key does not list: the add must refuse it.
    unlisted_model: str = Field(min_length=1)
    #: The model list the scripted in-memory key offers.
    key_listed_models: tuple[str, ...] = Field(min_length=2)
    task_type: str = Field(min_length=1)
    prompt: str = Field(min_length=1)
    answer_marker: str = Field(min_length=8)
    answer: str = Field(min_length=1)
    fault_status_code: int = Field(ge=400, le=599)
    rate_limited_class: str = Field(min_length=1)
    model_wire_key: str = Field(min_length=1)

    @model_validator(mode="after")
    def _models_are_distinct_and_listed(self) -> ModelR5Inputs:
        if self.model_a == self.model_b:
            raise ValueError("model A and model B must be two different models")
        for name in (self.model_a, self.model_b):
            if name not in self.key_listed_models:
                raise ValueError(f"{name} is not in the key's listed models")
        if self.unlisted_model in self.key_listed_models:
            raise ValueError("the unlisted model must not be in the key's list")
        return self

    def faults(self) -> ModelProbeFaults:
        """The faults file: model A answers the fault status on every call."""
        return ModelProbeFaults(
            faults=(
                ModelProbeFault(
                    model_id=self.model_a, status_code=self.fault_status_code
                ),
            )
        )

    def pinned_payload(self, model: str) -> dict[str, object]:
        return {
            "prompt": self.prompt,
            "task_type": self.task_type,
            "source": "claude-code",
            self.model_wire_key: model,
        }


def load_inputs(path: Path) -> ModelR5Inputs:
    return ModelR5Inputs.model_validate(
        yaml.safe_load(path.read_text(encoding="utf-8"))
    )


def load_runtime(path: Path = CONTRACT_PATH) -> ModelR5Runtime:
    return ModelR5Runtime.model_validate(read_contract(path)["runtime"])


def load_schedule(path: Path = CONTRACT_PATH) -> ModelScenarioSchedule:
    return load_schedule_from(path)


# -- the injected dependencies ----------------------------------------------------------------------------------------


class ProtocolModelRegistry(Protocol):
    """The model registry of one key: the add and the listing of ``onex models``."""

    def reset(self) -> None:
        """Forget every stored model (a fresh home)."""
        ...

    def add(self, provider: str, models: tuple[str, ...]) -> None:
        """Store ``models`` as the provider's ordered list, or raise ``ModelRegistryRefusedError`` and store nothing."""
        ...

    def listing(self, provider: str) -> tuple[str, ...]:
        """The provider's models, first tried first."""
        ...


class ProtocolR5Delegator(Protocol):
    """The delegations R5 runs: one pinned to a model, one with a faults file."""

    def pinned(self, model: str) -> ModelR5Receipt:
        """Raise ``ModelPinRefusedError`` when the request does not accept the model."""
        ...

    def forced(self, faults: ModelProbeFaults) -> ModelR5Receipt: ...


@dataclass(frozen=True)
class ModelR5Backend:
    """The two injected dependencies of one run."""

    registry: ProtocolModelRegistry
    delegator: ProtocolR5Delegator


BackendFactory = Callable[[ModelR5Inputs], ModelR5Backend]


def wire_pin_refusal(inputs: ModelR5Inputs, model: str) -> None:
    """The request model's own admission of a pinned model: raises ``ModelPinRefusedError`` while the ingress refuses it."""
    try:
        ModelDelegateSkillRequest.model_validate(inputs.pinned_payload(model))
    except ValueError as exc:
        raise ModelPinRefusedError(
            f"the request refuses the model key: {type(exc).__name__}"
        ) from None


class ScriptedModelList:
    """The in-memory registry: the key lists ``key_listed_models``; an add stores the ordered list or refuses an unlisted id."""

    def __init__(self, key_listed_models: tuple[str, ...]) -> None:
        self._listed = key_listed_models
        self._stored: dict[str, tuple[str, ...]] = {}

    def reset(self) -> None:
        self._stored.clear()

    def add(self, provider: str, models: tuple[str, ...]) -> None:
        if len(set(models)) != len(models):
            raise ModelRegistryRefusedError("a model is named twice")
        for model in models:
            if model not in self._listed:
                raise ModelRegistryRefusedError(f"{provider} does not list {model}")
        self._stored[provider] = models

    def listing(self, provider: str) -> tuple[str, ...]:
        return self._stored.get(provider, ())


class ScriptedDelegator:
    """The in-memory delegator: a faulted model answers its fault, the next model in the registry's order answers."""

    def __init__(
        self,
        registry: ScriptedModelList,
        inputs: ModelR5Inputs,
        *,
        admit: Callable[[ModelR5Inputs, str], None] = wire_pin_refusal,
    ) -> None:
        self._registry = registry
        self._inputs = inputs
        self._admit = admit

    def pinned(self, model: str) -> ModelR5Receipt:
        self._admit(self._inputs, model)
        return ModelR5Receipt(
            status="completed",
            attempts=(
                ModelR5Attempt(model_id=model, failure_class=None, accepted=True),
            ),
            final_answer=self._inputs.answer,
        )

    def forced(self, faults: ModelProbeFaults) -> ModelR5Receipt:
        attempts: list[ModelR5Attempt] = []
        for model in self._registry.listing(self._inputs.provider):
            fault = faults.fault_for(model)
            if fault is not None:
                attempts.append(
                    ModelR5Attempt(
                        model_id=model,
                        failure_class=fault.failure_class.value,
                        accepted=False,
                    )
                )
                continue
            attempts.append(
                ModelR5Attempt(model_id=model, failure_class=None, accepted=True)
            )
            return ModelR5Receipt(
                status="completed",
                attempts=tuple(attempts),
                final_answer=self._inputs.answer,
            )
        return ModelR5Receipt(
            status="failed", attempts=tuple(attempts), final_answer=""
        )


@contextmanager
def home_scope(home: Path) -> Iterator[None]:
    """Point HOME at the scenario's fresh home for one call and put the host's value back after."""
    before = os.environ.get("HOME")  # ONEX_FLAG_EXEMPT
    os.environ["HOME"] = str(home)  # ONEX_FLAG_EXEMPT
    try:
        yield
    finally:
        if before is None:
            os.environ.pop("HOME", None)  # ONEX_FLAG_EXEMPT
        else:
            os.environ["HOME"] = before  # ONEX_FLAG_EXEMPT


AddKey = Callable[..., object]
ListProviders = Callable[[], tuple[object, ...]]


class ModelKeysRegistry:
    """The real registry: the functions behind ``onex models add`` and ``onex models list``, in a fresh home.

    The probe key is read from its variable at the moment of the add and handed to the add function; it is not kept.
    """

    def __init__(
        self,
        *,
        home: Path,
        read_key: Callable[[], str],
        add_key: AddKey = model_keys.add_key,
        list_providers: ListProviders = model_keys.list_providers,
    ) -> None:
        self._home = home
        self._read_key = read_key
        self._add_key = add_key
        self._list_providers = list_providers

    def reset(self) -> None:
        shutil.rmtree(self._home, ignore_errors=True)
        self._home.mkdir(parents=True, exist_ok=True)

    def add(self, provider: str, models: tuple[str, ...]) -> None:
        try:
            with home_scope(self._home):
                self._add_key(provider, self._read_key(), models)
        except model_keys.ModelKeyRefusedError as exc:
            raise ModelRegistryRefusedError(
                f"add refused: {type(exc).__name__}"
            ) from None

    def listing(self, provider: str) -> tuple[str, ...]:
        with home_scope(self._home):
            for status in self._list_providers():
                if getattr(status, "provider", None) == provider:
                    return tuple(getattr(status, "models", ()))
        return ()


class RealDelegator:
    """The real delegator: the delegation skill handler against the models the registry stored, in the same fresh home."""

    def __init__(
        self,
        inputs: ModelR5Inputs,
        *,
        home: Path,
        work_dir: Path,
        tenant_slug: str,
    ) -> None:
        self._inputs = inputs
        self._home = home
        self._work_dir = work_dir
        self._tenant_slug = tenant_slug

    def _provenance(self) -> ModelDelegationProvenance:
        return ModelDelegationProvenance(
            source="claude-code",
            traffic_class=EnumDelegationTrafficClass.SYNTHETIC,
            source_surface=READINESS_PROBE_SOURCE_SURFACE,
        )

    def _handler(self) -> HandlerDelegateSkill:
        self._work_dir.mkdir(parents=True, exist_ok=True)
        db_path = self._work_dir / DB_FILENAME
        seed_probe_tenant(db_path, self._tenant_slug)
        port = LocalDelegationDispatchPort(
            effect_handler=None,
            evidence_db_path=db_path,
            effect_process_boundary=False,
        )
        return HandlerDelegateSkill(dispatch_port=port)

    def _run(self, request: ModelDelegateSkillRequest) -> ModelR5Receipt:
        with home_scope(self._home):
            response = asyncio.run(self._handler().handle(request))
        if response is None:
            raise RuntimeError("the delegation produced no terminal")
        return ModelR5Receipt(
            status=response.status,
            attempts=tuple(
                ModelR5Attempt(
                    model_id=attempt.model_id,
                    failure_class=attempt.failure_class,
                    accepted=attempt.quality_gate_passed,
                )
                for attempt in response.attempts
            ),
            final_answer=response.response,
        )

    def pinned(self, model: str) -> ModelR5Receipt:
        payload = {
            **self._inputs.pinned_payload(model),
            "correlation_id": uuid4(),
            "tenant_id": self._tenant_slug,
            "provenance": self._provenance(),
        }
        try:
            request = ModelDelegateSkillRequest.model_validate(payload)
        except ValueError as exc:
            raise ModelPinRefusedError(
                f"the request refuses the model key: {type(exc).__name__}"
            ) from None
        return self._run(request)

    def forced(self, faults: ModelProbeFaults) -> ModelR5Receipt:
        request = ModelDelegateSkillRequest(
            prompt=self._inputs.prompt,
            task_type=self._inputs.task_type,
            source="claude-code",
            correlation_id=uuid4(),
            tenant_id=self._tenant_slug,
            provenance=self._provenance(),
            probe_ladder=faults,
        )
        return self._run(request)


def scripted_backend(inputs: ModelR5Inputs) -> ModelR5Backend:
    registry = ScriptedModelList(inputs.key_listed_models)
    return ModelR5Backend(
        registry=registry, delegator=ScriptedDelegator(registry, inputs)
    )


# -- the scenario ---------------------------------------------------------------------------------------------------


class ModelScenarioR5RunCommand(BaseModel):
    """One run of scenario R5. It carries no value: every setting is in the node's contract and inputs file."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class HandlerScenarioR5(ReadinessScenarioBase):
    """Run scenario R5 once, write its STATUS row and emit its result on the bus."""

    def __init__(
        self,
        runtime: ModelR5Runtime,
        *,
        home: Path,
        environment: Mapping[str, str],
        appender: LedgerAppender,
        sink: ProtocolScenarioEventSink,
        terminal_topic: str,
        backend_factory: BackendFactory = scripted_backend,
        trigger: EnumScenarioTrigger = EnumScenarioTrigger.SCHEDULE,
        clock: Clock = lambda: datetime.now(UTC),
        inputs_path: Path | None = None,
    ) -> None:
        super().__init__(
            runtime,
            home=home,
            environment=environment,
            appender=appender,
            sink=sink,
            terminal_topic=terminal_topic,
            transport_factory=_no_transport,
            trigger=trigger,
            clock=clock,
        )
        self._r5 = runtime
        self._backend_factory = backend_factory
        self._inputs_path = inputs_path or NODE_DIR / runtime.inputs_relpath

    def handle(self, request: ModelScenarioR5RunCommand) -> ModelScenarioResult:
        return self.run()

    def _scenario(self, assertions: list[str]) -> str:
        problems: list[str] = []

        def check(name: str, held: bool, why: str = "") -> None:
            assertions.append(("+" if held else "-") + name)
            if not held and why:
                problems.append(f"{name}: {why}")

        try:
            inputs = load_inputs(self._inputs_path)
        except (OSError, ValueError) as exc:
            assertions.append("-inputs_readable")
            return f"inputs unreadable: {type(exc).__name__}"
        assertions.append("+inputs_readable")

        if self._r5.model_backend == "real":
            key_set = bool(
                self._environment.get(self._r5.openrouter_probe_key_env, "").strip()
            )
            check(
                "probe_key_present",
                key_set,
                f"{self._r5.openrouter_probe_key_env} is empty",
            )
            if not key_set:
                return "; ".join(problems)

        backend = self._backend_factory(inputs)
        registry, delegator = backend.registry, backend.delegator
        provider, model_a, model_b = inputs.provider, inputs.model_a, inputs.model_b
        order = (model_a, model_b)
        registry.reset()

        # 1. register A then B on one key, in that order.
        try:
            registry.add(provider, order)
            added = True
        except ModelRegistryRefusedError as exc:
            added = False
            problems.append(f"models_added: {exc}")
        check("models_added", added)
        listed = registry.listing(provider)
        check(
            "listing_a_then_b",
            listed == order,
            f"listing {list(listed)}, want {list(order)}",
        )

        # 2. one delegation pinned to each model; each receipt names its model.
        for tag, model in (("a", model_a), ("b", model_b)):
            try:
                receipt: ModelR5Receipt | None = delegator.pinned(model)
                refusal = ""
            except ModelPinRefusedError as exc:
                receipt, refusal = None, str(exc)
            check(f"pinned_model_accepted_{tag}", receipt is not None, refusal)
            check(
                f"pinned_receipt_names_model_{tag}",
                receipt is not None
                and receipt.status == "completed"
                and receipt.walked == (model,)
                and receipt.accepted_model == model,
                "no receipt" if receipt is None else f"walked {list(receipt.walked)}",
            )

        # 3. the faults file makes A answer 429: A rate_limited, then B accepted.
        faults = inputs.faults()
        forced = delegator.forced(faults)
        self._history(check, "forced", forced, inputs, (model_a, model_b))

        # 4. falsifier of the add: an id the key does not list is refused and stores nothing.
        try:
            registry.add(provider, (model_a, inputs.unlisted_model))
            refused = False
        except ModelRegistryRefusedError:
            refused = True
        check("unlisted_id_refused_by_add", refused, "the add took an unlisted id")
        check(
            "unlisted_id_stored_nothing",
            registry.listing(provider) == order,
            f"listing {list(registry.listing(provider))}",
        )

        # 5. falsifier of the order: after a reorder to B, A the same faults file answers on B with no attempt on A.
        reordered = (model_b, model_a)
        try:
            registry.add(provider, reordered)
        except ModelRegistryRefusedError as exc:
            problems.append(f"reorder: {exc}")
        check(
            "reorder_listing_b_then_a",
            registry.listing(provider) == reordered,
            f"listing {list(registry.listing(provider))}",
        )
        again = delegator.forced(faults)
        check(
            "reorder_answers_on_b",
            again.status == "completed" and again.accepted_model == model_b,
            f"status {again.status}, accepted {again.accepted_model}",
        )
        check(
            "reorder_no_attempt_on_a",
            model_a not in again.walked,
            f"walked {list(again.walked)}",
        )
        return "; ".join(problems) or "every assertion held"

    @staticmethod
    def _history(
        check: Callable[[str, bool, str], None],
        prefix: str,
        receipt: ModelR5Receipt,
        inputs: ModelR5Inputs,
        order: tuple[str, str],
    ) -> None:
        model_a, model_b = order
        first = receipt.attempts[0] if receipt.attempts else None
        second = receipt.attempts[1] if len(receipt.attempts) > 1 else None
        check(
            f"{prefix}_first_attempt_a_rate_limited",
            first is not None
            and first.model_id == model_a
            and first.failure_class == inputs.rate_limited_class
            and not first.accepted,
            f"first attempt {first}",
        )
        check(
            f"{prefix}_second_attempt_b_accepted",
            second is not None
            and second.model_id == model_b
            and second.accepted
            and receipt.status == "completed",
            f"second attempt {second}, status {receipt.status}",
        )
        check(
            f"{prefix}_answer_bytes",
            inputs.answer_marker in receipt.final_answer,
            "the returned bytes do not carry the fixed answer",
        )


def _no_transport() -> NoReturn:
    raise RuntimeError("scenario R5 runs no loopback transport")


def build_runner(
    env: Mapping[str, str] | None = None,
    *,
    trigger: EnumScenarioTrigger = EnumScenarioTrigger.SCHEDULE,
) -> HandlerScenarioR5:
    """The production wiring: the registry home, the bus commands and the contract's backend profile."""
    runtime = load_runtime()
    environment = dict(os.environ if env is None else env)  # ONEX_FLAG_EXEMPT
    # The unit's environment file may override the contract's model backend for this one run: `real` or `inmemory` is
    # honoured, unset keeps the contract's value and any other value refuses the tick (exit 65, conclusion `refused`).
    override = environment.get(runtime.model_backend_env, "").strip()
    if override:
        if override not in ("real", "inmemory"):
            raise RuntimeError(
                f"{runtime.model_backend_env} must be 'real' or 'inmemory', not {override!r}"
            )
        runtime = runtime.model_copy(update={"model_backend": override})
    home = Path(environment[runtime.home_env])
    work_dir = home / runtime.work_relpath
    fresh_home = work_dir / FRESH_HOME_DIRNAME
    slug = runtime.probe_tenant_slug
    key_env = runtime.openrouter_probe_key_env

    def real_backend(inputs: ModelR5Inputs) -> ModelR5Backend:
        return ModelR5Backend(
            registry=ModelKeysRegistry(
                home=fresh_home, read_key=lambda: environment.get(key_env, "").strip()
            ),
            delegator=RealDelegator(
                inputs, home=fresh_home, work_dir=work_dir, tenant_slug=slug
            ),
        )

    factory: BackendFactory = (
        real_backend if runtime.model_backend == "real" else scripted_backend
    )
    built = build_scenario(
        lambda rt, **kwargs: HandlerScenarioR5(
            ModelR5Runtime.model_validate(rt.model_dump()),
            backend_factory=factory,
            **{k: v for k, v in kwargs.items() if k != "transport_factory"},
        ),
        runtime,
        contract_path=CONTRACT_PATH,
        env=environment,
        trigger=trigger,
    )
    assert isinstance(built, HandlerScenarioR5)
    return built
