# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Delegation targets share the existing effect, without wrapper scripts."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
import yaml

from omnimarket.models.delegation.model_delegation_target import (
    ModelDelegationGeneration,
    ModelDelegationTargetRequest,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_delegation_target import (
    HandlerClaudeCodeArguments,
    HandlerClaudeGlmArguments,
    HandlerCodexArguments,
    HandlerDelegationModelCall,
    HandlerDelegationTarget,
)

pytestmark = pytest.mark.unit


def request(**updates: object) -> ModelDelegationTargetRequest:
    values: dict[str, object] = {
        "request_id": str(uuid4()),
        "target": "codex",
        "brief": "Read the repository and return the answer.",
        "repo": "OmniNode-ai/omnimarket",
        "commit_sha": "a" * 40,
        "call_site": "lab_job",
    }
    values.update(updates)
    return ModelDelegationTargetRequest.model_validate(values)


def test_existing_effect_declares_the_submitted_job_consumer() -> None:
    root = Path(__file__).resolve().parents[4]
    contract = yaml.safe_load(
        (
            root / "src/omnimarket/nodes/node_llm_delegation_call_effect/contract.yaml"
        ).read_text()
    )
    assert (
        "onex.cmd.omnimarket.lab-job-submitted.v1"
        in contract["event_bus"]["subscribe_topics"]
    )
    route = next(
        route
        for route in contract["handler_routing"]["handlers"]
        if route["operation"] == "execute_lab_job"
    )
    assert route["event_model"]["name"] == "ModelLabJobSpec"
    assert (
        "onex.evt.omnimarket.lab-job-transitioned.v1"
        in contract["event_bus"]["publish_topics"]
    )


@pytest.mark.parametrize(
    ("adapter", "target", "binary"),
    [
        (HandlerCodexArguments, "codex", "codex"),
        (HandlerClaudeCodeArguments, "claude-code", "claude"),
        (HandlerClaudeGlmArguments, "claude-glm", "claude"),
    ],
)
def test_agent_adapters_pass_the_brief_on_stdin_without_bypassing_permissions(
    adapter: type,
    target: str,
    binary: str,
) -> None:
    argv = adapter().argv(request(target=target), model="configured-model")
    assert argv[0] == binary
    assert "Read the repository" not in " ".join(argv)
    assert not any("dangerously" in arg or "bypass" in arg for arg in argv)
    if target == "codex":
        assert "--json" in argv
        assert argv[-1] == "-"
    else:
        assert "acceptEdits" in argv


class Router:
    def __init__(self) -> None:
        self.requests: list[object] = []

    async def handle_async(self, value: object) -> object:
        self.requests.append(value)
        return SimpleNamespace(
            model_key="selected", endpoint_url="https://provider.invalid/custom/chat"
        )


class Inference:
    def __init__(self) -> None:
        self.requests: list[object] = []

    async def handle(self, value: object, *, correlation_id: object = None) -> object:
        self.requests.append(value)
        return SimpleNamespace(
            generated_text="answer", tool_calls=(), model="served-model"
        )


async def test_model_adapter_uses_router_endpoint_and_generic_inference() -> None:
    router, inference = Router(), Inference()
    adapter = HandlerDelegationModelCall(router=router, inference=inference)
    result = await adapter.execute(
        request(target="model-call", repo=None, commit_sha=None),
        generation=ModelDelegationGeneration(temperature=0.3, max_tokens=40),
        route={
            "model_name": "served-model",
            "supported_parameters": ["temperature", "max_tokens"],
        },
        timeout_seconds=25,
    )
    assert router.requests
    sent = inference.requests[0]
    assert sent.endpoint_url == "https://provider.invalid/custom/chat"
    assert sent.model == "served-model"
    assert sent.temperature == 0.3
    assert sent.max_tokens == 40
    assert result.content == "answer"


async def test_ignored_or_unmeasured_parameters_are_refused_before_inference() -> None:
    router, inference = Router(), Inference()
    adapter = HandlerDelegationModelCall(router=router, inference=inference)
    with pytest.raises(ValueError, match="seed"):
        await adapter.execute(
            request(target="model-call", repo=None, commit_sha=None),
            generation=ModelDelegationGeneration(seed=7),
            route={"model_name": "served-model", "supported_parameters": []},
            timeout_seconds=25,
        )
    assert not inference.requests


def test_generation_overrides_preserve_unset_contract_values() -> None:
    defaults = ModelDelegationGeneration(temperature=0.4, top_p=0.9, max_tokens=80)
    overrides = ModelDelegationGeneration(temperature=0.0)
    merged = defaults.overridden_by(overrides)
    assert merged.temperature == 0.0
    assert merged.top_p == 0.9
    assert merged.max_tokens == 80


def test_generation_defaults_survive_a_request_wire_round_trip() -> None:
    original = request(target="model-call", repo=None, commit_sha=None)
    decoded = ModelDelegationTargetRequest.model_validate_json(
        original.model_dump_json()
    )
    merged = ModelDelegationGeneration(temperature=0.4, max_tokens=80).overridden_by(
        decoded.generation
    )
    assert merged.temperature == 0.4
    assert merged.max_tokens == 80


def test_agent_request_with_unset_generation_survives_the_wire() -> None:
    original = request()
    decoded = ModelDelegationTargetRequest.model_validate_json(
        original.model_dump_json()
    )
    assert decoded.target == "codex"


def test_agent_requires_an_exact_commit_and_repository() -> None:
    with pytest.raises(ValueError, match="commit_sha"):
        request(commit_sha="dev")
    with pytest.raises(ValueError, match="require repo"):
        request(repo=None)


async def test_unknown_call_site_is_refused() -> None:
    handler = HandlerDelegationTarget(event_bus=None, config={"call_sites": {}})
    with pytest.raises(ValueError, match="call_site"):
        await handler.handle(request())


class JobStore:
    """Shared durable-store semantics used by two independent handlers."""

    def __init__(self) -> None:
        self.claims: set[str] = set()
        self.transitions: list[object] = []
        self.result: object = None

    async def claim(self, spec: object, runtime_id: str) -> bool:
        if spec.job_id in self.claims:
            return False
        self.claims.add(spec.job_id)
        return True

    async def append(self, value: object) -> None:
        self.transitions.append(value)

    async def finish(self, job_id: str, result: object) -> None:
        self.result = result

    async def flush(self, bus: object) -> None:
        from omnimarket.delegated_test_loop.lab_run_bus import envelope_bytes_for

        topic = "onex.evt.omnimarket.lab-job-transitioned.v1"
        for transition in self.transitions:
            await bus.publish(
                topic,
                transition.row.job_id.encode(),
                envelope_bytes_for(topic, transition.model_dump(mode="json"), uuid4()),
            )


class Target:
    def __init__(self, status: str = "completed") -> None:
        self.requests: list[object] = []
        self.status = status

    async def handle(self, value: object) -> object:
        from omnimarket.models.delegation.model_delegation_target import (
            ModelDelegationTargetResult,
        )

        self.requests.append(value)
        return ModelDelegationTargetResult(
            request_id=value.request_id,
            target=value.target,
            status=self.status,
            exit_code=0 if self.status == "completed" else 1,
            session_id="test-session",
            content="job answer",
            commit_sha=value.commit_sha,
        )


class Bus:
    def __init__(self) -> None:
        self.events: list[object] = []

    async def publish(self, *args: object) -> None:
        self.events.append(args)


def job_spec(**updates: object) -> object:
    from omnimarket.models.lab_job import ModelLabJobSpec

    values: dict[str, object] = {
        "job_id": "lj-0123456789abcdef",
        "kind": "lane",
        "brief": "Recorded brief",
        "repo": "OmniNode-ai/omnimarket",
        "ref": "a" * 40,
        "engine": "codex",
        "time_box_min": 5,
        "done_criteria": [{"kind": "exit_zero"}],
        "parent_lane": "test",
        "ticket": "OMN-20604",
    }
    values.update(updates)
    return ModelLabJobSpec.model_validate(values)


async def test_lab_submission_launches_once_and_publishes_reducer_rows() -> None:
    import asyncio

    from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_lab_job_delegation import (
        HandlerLabJobDelegation,
    )

    store, target, bus = JobStore(), Target(), Bus()
    one = HandlerLabJobDelegation(
        store=store, target_handler=target, event_bus=bus, runtime_id="lab-one"
    )
    two = HandlerLabJobDelegation(
        store=store, target_handler=target, event_bus=bus, runtime_id="lab-two"
    )
    results = await asyncio.gather(one.handle(job_spec()), two.handle(job_spec()))
    assert sorted(result.status for result in results) == ["completed", "duplicate"]
    assert len(target.requests) == 1
    assert target.requests[0].brief == "Recorded brief"
    assert target.requests[0].commit_sha == "a" * 40
    states = [value.transition.to_state.value for value in store.transitions]
    assert states == ["queued", "dispatched", "running", "checking", "done"]
    assert [value.row.seq for value in store.transitions] == [1, 2, 3, 4, 5]
    assert store.transitions[0].row.spec.brief == "Recorded brief"


async def test_exit_zero_never_completes_an_unverified_pr_criterion() -> None:
    from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_lab_job_delegation import (
        HandlerLabJobDelegation,
    )

    store = JobStore()
    handler = HandlerLabJobDelegation(
        store=store, target_handler=Target(), event_bus=Bus(), runtime_id="lab"
    )
    await handler.handle(
        job_spec(done_criteria=[{"kind": "pr_merged", "target": "owner/repo#1"}])
    )
    assert store.transitions[-1].row.state.value == "checking"


async def test_preferred_other_host_is_refused_before_claim() -> None:
    from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_lab_job_delegation import (
        HandlerLabJobDelegation,
    )

    store = JobStore()
    handler = HandlerLabJobDelegation(
        store=store, target_handler=Target(), event_bus=Bus(), runtime_id="lab"
    )
    with pytest.raises(ValueError, match="host"):
        await handler.handle(job_spec(host_preference=["another-lab"]))
    assert not store.claims


async def test_generic_handler_payload_retains_all_requested_parameters() -> None:
    from omnibase_infra.nodes.node_llm_inference_effect import (
        HandlerLlmOpenaiCompatible,
    )

    inference = Inference()
    parameters = ModelDelegationGeneration(
        temperature=0.0,
        top_p=0.8,
        max_tokens=50,
        stop=("END",),
        seed=8,
        response_format={"type": "json_object"},
        tools=(
            {
                "type": "function",
                "function": {"name": "answer", "parameters": {"type": "object"}},
            },
        ),
        tool_choice="required",
        frequency_penalty=0.1,
        presence_penalty=0.2,
        reasoning_effort="low",
        thinking={"enable_thinking": False},
    )
    await HandlerDelegationModelCall(router=Router(), inference=inference).execute(
        request(target="model-call", repo=None, commit_sha=None),
        generation=parameters,
        route={
            "model_name": "served",
            "supported_parameters": list(parameters.model_fields_set),
            "parameter_mappings": {"thinking": "chat_template_kwargs"},
        },
        timeout_seconds=20,
    )
    payload = HandlerLlmOpenaiCompatible._build_payload(inference.requests[0])
    assert payload["temperature"] == 0.0
    assert payload["top_p"] == 0.8
    assert payload["max_tokens"] == 50
    assert payload["stop"] == ["END"]
    assert payload["seed"] == 8
    assert payload["response_format"] == {"type": "json_object"}
    assert payload["tools"][0]["function"]["name"] == "answer"
    assert payload["tool_choice"] == "required"
    assert payload["frequency_penalty"] == 0.1
    assert payload["presence_penalty"] == 0.2
    assert payload["reasoning_effort"] == "low"
    assert payload["chat_template_kwargs"] == {"enable_thinking": False}


@pytest.mark.parametrize("status", ["completed", "failed", "timed_out"])
async def test_runtime_discovers_and_dispatches_the_real_submit_envelope(
    monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    import asyncio
    import json

    from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
    from omnibase_infra.runtime.auto_wiring.discovery import (
        discover_contracts_from_paths,
    )
    from omnibase_infra.runtime.auto_wiring.handler_wiring import wire_from_manifest
    from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine

    from omnimarket.nodes.node_lab_job_submit_effect.handlers.handler_lab_job_submit_effect import (
        HandlerLabJobSubmitEffect,
    )
    from omnimarket.nodes.node_llm_delegation_call_effect.handlers import (
        handler_lab_job_delegation as module,
    )

    store, target = JobStore(), Target(status=status)
    original = module.HandlerLabJobDelegation

    class Injected(original):
        def __init__(self, event_bus: object) -> None:
            super().__init__(
                event_bus=event_bus,
                store=store,
                target_handler=target,
                runtime_id="lab",
            )

    monkeypatch.setattr(module, "HandlerLabJobDelegation", Injected)
    contract_path = (
        Path(__file__).resolve().parents[4]
        / "src/omnimarket/nodes/node_llm_delegation_call_effect/contract.yaml"
    )
    manifest = discover_contracts_from_paths([contract_path])
    assert not manifest.errors
    bus = EventBusInmemory(environment="delegation-target-test", group="runtime")
    await bus.start()
    engine = MessageDispatchEngine()
    completed, results = asyncio.Event(), []

    async def receive(message: object) -> None:
        results.append(json.loads(message.value)["payload"])
        completed.set()

    await bus.subscribe(
        "onex.evt.omnimarket.delegation-target-completed.v1",
        on_message=receive,
        group_id="probe",
    )
    try:
        report = await wire_from_manifest(
            manifest, engine, event_bus=bus, environment="lab"
        )
        assert report.total_failed == 0, report
        engine.freeze()
        await HandlerLabJobSubmitEffect(bus).handle(job_spec())
        await asyncio.wait_for(completed.wait(), timeout=5)
        assert len(target.requests) == 1
        assert results[0]["status"] == status
    finally:
        await bus.close()


async def test_adapter_exception_becomes_a_failed_attempt_with_no_secret_text() -> None:
    from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_lab_job_delegation import (
        HandlerLabJobDelegation,
    )

    class BrokenTarget:
        async def handle(self, value: object) -> object:
            raise RuntimeError("credential-value-must-not-be-copied")

    store = JobStore()
    result = await HandlerLabJobDelegation(
        store=store, target_handler=BrokenTarget(), event_bus=Bus(), runtime_id="lab"
    ).handle(job_spec())
    assert result.status == "failed"
    assert "credential-value" not in result.model_dump_json()
    assert store.transitions[-1].row.state.value == "retrying"


def test_wire_models_agree_on_both_sides_of_each_lab_seam() -> None:
    root = Path(__file__).resolve().parents[4] / "src/omnimarket/nodes"

    def contract(name: str) -> dict:
        return yaml.safe_load((root / name / "contract.yaml").read_text())

    effect = contract("node_llm_delegation_call_effect")
    submit = contract("node_lab_job_submit_effect")
    projection = contract("node_projection_lab_job")
    assert (
        effect["lab_job_dispatch"]["command_topic"]
        == submit["submit_dispatch"]["command_topic"]
    )
    assert (
        effect["lab_job_dispatch"]["transition_topic"]
        in projection["event_bus"]["subscribe_topics"]
    )
    assert (
        effect["event_bus"]["publish_topic_metadata"][
            effect["lab_job_dispatch"]["transition_topic"]
        ]["schema_ref"]
        == "omnimarket.models.lab_job.ModelLabJobTransitioned"
    )


async def test_session_adapter_checks_out_exact_commit_and_retains_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise local git + subprocess with a deterministic CLI fixture, no model call."""
    import json
    import os
    import subprocess
    import sys

    from omnibase_core.validators.no_unguarded_git_subprocess import (
        scrub_git_location_env,
    )

    from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_delegation_target import (
        _agent_environment,
    )

    repos = tmp_path / "repos"
    clone = repos / "omnimarket"
    clone.mkdir(parents=True)
    env = _agent_environment()

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(clone), *args],
            env=scrub_git_location_env(env),
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    git("init", "-q")
    (clone / "README").write_text("fixture checkout")
    git("add", "README")
    git(
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-qm",
        "fixture",
    )
    sha = git("rev-parse", "HEAD")
    binaries = tmp_path / "bin"
    binaries.mkdir()
    binary = binaries / "codex"
    binary.write_text(
        f"#!{sys.executable}\nimport json,sys\nbrief=sys.stdin.read()\nprint(json.dumps({{'type':'thread.started','thread_id':'fixture-thread'}}))\nprint(json.dumps({{'type':'item.completed','item':{{'type':'agent_message','text':brief}}}}))\n"
    )
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", str(binaries) + os.pathsep + os.environ["PATH"])
    handler = HandlerDelegationTarget(
        event_bus=None,
        config={
            "repository_root": str(repos),
            "work_root": str(tmp_path / "runs"),
            "allowed_owners": ["OmniNode-ai"],
            "call_sites": {"lab_job": {"targets": ["codex"], "timeout_seconds": 10}},
        },
    )
    value = request(commit_sha=sha)
    result = await handler.handle(value)
    assert result.status == "completed"
    assert result.content == value.brief
    assert result.session_id == "fixture-thread"
    assert git("-C", result.worktree, "rev-parse", "HEAD") == sha
    assert json.loads(Path(result.receipt_path).read_text())["commit_sha"] == sha
    assert await handler.handle(value) == result
    with pytest.raises(ValueError, match="different delegation"):
        await handler.handle(value.model_copy(update={"brief": "a different brief"}))


def test_consumer_group_is_shared_within_one_lab_and_distinct_between_labs() -> None:
    from omnibase_infra.enums import EnumConsumerGroupPurpose
    from omnibase_infra.models import ModelNodeIdentity
    from omnibase_infra.utils import compute_consumer_group_id

    identity = {
        "service": "omnimarket",
        "node_name": "node_llm_delegation_call_effect",
        "version": "0.1.0",
    }
    group = compute_consumer_group_id(
        ModelNodeIdentity(env="lab-a", **identity), EnumConsumerGroupPurpose.CONSUME
    )
    assert group == compute_consumer_group_id(
        ModelNodeIdentity(env="lab-a", **identity), EnumConsumerGroupPurpose.CONSUME
    )
    assert group != compute_consumer_group_id(
        ModelNodeIdentity(env="lab-b", **identity), EnumConsumerGroupPurpose.CONSUME
    )


@pytest.mark.parametrize("status", ["ignored", "rejected", "unmeasured"])
async def test_non_honored_parameter_status_refuses_the_call(status: str) -> None:
    inference = Inference()
    with pytest.raises(ValueError, match="temperature"):
        await HandlerDelegationModelCall(router=Router(), inference=inference).execute(
            request(target="model-call", repo=None, commit_sha=None),
            generation=ModelDelegationGeneration(temperature=0.3),
            route={"model_name": "served", "parameter_status": {"temperature": status}},
            timeout_seconds=20,
        )
    assert not inference.requests


async def test_model_call_requires_its_callers_contract() -> None:
    handler = HandlerDelegationTarget(
        event_bus=None,
        config={
            "call_sites": {
                "lab_job": {"targets": ["model-call"], "timeout_seconds": 20},
            }
        },
    )
    with pytest.raises(ValueError, match="caller contract_path"):
        await handler.handle(request(target="model-call", repo=None, commit_sha=None))


async def test_output_limit_can_use_a_measured_provider_wire_alias() -> None:
    from omnibase_infra.nodes.node_llm_inference_effect import (
        HandlerLlmOpenaiCompatible,
    )

    inference = Inference()
    await HandlerDelegationModelCall(router=Router(), inference=inference).execute(
        request(target="model-call", repo=None, commit_sha=None),
        generation=ModelDelegationGeneration(max_tokens=80),
        route={
            "model_name": "served",
            "supported_parameters": ["max_tokens"],
            "parameter_mappings": {"max_tokens": "max_completion_tokens"},
        },
        timeout_seconds=20,
    )
    payload = HandlerLlmOpenaiCompatible._build_payload(inference.requests[0])
    assert payload["max_completion_tokens"] == 80
    assert "max_tokens" not in payload


@pytest.mark.parametrize("destination", ["model", "messages", "temperature"])
async def test_parameter_mapping_cannot_replace_authority_or_another_parameter(
    destination: str,
) -> None:
    inference = Inference()
    with pytest.raises(ValueError, match="parameter mapping"):
        await HandlerDelegationModelCall(router=Router(), inference=inference).execute(
            request(target="model-call", repo=None, commit_sha=None),
            generation=ModelDelegationGeneration(seed=8),
            route={
                "model_name": "served",
                "supported_parameters": ["seed"],
                "parameter_mappings": {"seed": destination},
            },
            timeout_seconds=20,
        )
    assert not inference.requests


async def test_tool_call_only_response_survives_the_generic_handler() -> None:
    from omnibase_infra.nodes.node_llm_inference_effect import (
        HandlerLlmOpenaiCompatible,
    )

    class Transport:
        async def _execute_llm_http_call(self, **kwargs: object) -> dict:
            return {
                "id": "fixture-call",
                "model": "served-model",
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_one",
                                    "type": "function",
                                    "function": {
                                        "name": "answer",
                                        "arguments": '{"value":42}',
                                    },
                                }
                            ],
                        },
                    }
                ],
                "usage": {
                    "prompt_tokens": 5,
                    "completion_tokens": 4,
                    "total_tokens": 9,
                },
            }

    adapter = HandlerDelegationModelCall(
        router=Router(), inference=HandlerLlmOpenaiCompatible(transport=Transport())
    )
    result = await adapter.execute(
        request(target="model-call", repo=None, commit_sha=None),
        generation=ModelDelegationGeneration(),
        route={"model_name": "served-model", "supported_parameters": []},
        timeout_seconds=20,
    )
    assert result.status == "completed"
    assert result.content == ""
    assert result.tool_calls[0]["function"]["name"] == "answer"
    assert result.tool_calls[0]["function"]["arguments"] == '{"value":42}'
