# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Capture-redaction contract tests (OMN-17209, landed via OMN-17399).

RED-first. The corpus is three dated live incidents, used verbatim as
fixtures rather than invented:

* OMN-17154 -- ``valkey-cli config get requirepass`` printed a plaintext
  password into the session and into SSM output.
* 2026-08-19 morning -- ``kubectl get secret -o json`` printed a
  ``clientSecret``.
* 2026-08-19 evening -- ``env | grep -i POSTGRES`` printed a
  ``postgresql://user:pw@host`` URL.

Each was caught by a regex added AFTER the leak, which is the doctrine
failure the contract exists to end: a class hashes because of what a record
IS, without needing to recognise the secret inside it.

Two layers are asserted separately on purpose:

* Against a TEST contract that deliberately declares the content fields
  ``capture_verbatim`` -- this isolates the always-hashed CLASS and proves it
  overrides the field allowlist. Without this isolation the production
  contract's fail-closed default would hash those fields anyway and the class
  would be untested.
* Against the REAL production contract -- proving no plaintext crosses today
  even for a field nobody has classified.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_emit_daemon.event_registry import (
    TOPIC_SCOPED_TRANSFORM_REGISTRY as DAEMON_TOPIC_SCOPED,
)
from omnimarket.nodes.node_event_emit_effect.enrichment import (
    TOPIC_SCOPED_TRANSFORM_REGISTRY,
    apply_transform,
)
from omnimarket.nodes.node_event_emit_effect.errors import (
    MalformedRedactionContractError,
    TopicScopedTransformError,
    UngovernedTopicError,
)
from omnimarket.nodes.node_event_emit_effect.redaction import (
    EnumCaptureClass,
    EnumRedactionState,
    default_contract_path,
    load_contract,
    redact_capture,
)

TOOL_TOPIC = "onex.evt.omniclaude.tool-executed.v1"
PROMPT_TOPIC = "onex.evt.omniclaude.prompt-submitted.v1"

# --- the three incident payloads, verbatim in shape -------------------------

VALKEY_INCIDENT = {
    "tool_name": "Bash",
    "command": "valkey-cli -h omninode-valkey config get requirepass",
    "tool_output": '1) "requirepass"\n2) "S3cr3t-Valkey-Pw-2026"',
}
KUBECTL_INCIDENT = {
    "tool_name": "Bash",
    "command": "kubectl get secret onex-api-oidc -n onex-dev -o json",
    "tool_output": json.dumps(
        {"data": {"clientSecret": "aG9wZS15b3UtZGlkbnQtcmVhZC10aGlz"}}
    ),
}
ENV_INCIDENT = {
    "tool_name": "Bash",
    "command": "env | grep -i POSTGRES",
    # The 2026-08-19 evening incident's real URL named a lab host by IP. The
    # host is replaced with a reserved-for-documentation name (RFC 2606
    # `.invalid`) rather than annotated past the leaked-literals gate: the
    # gate is right that an internal address does not belong in a fixture,
    # and the shape under test is the URL AUTHORITY -- scheme, userinfo,
    # `@`, host, port, path -- which this preserves exactly.
    "tool_output": "POSTGRES_URL=postgresql://onex:pgpw2026@db.example.invalid:5432/onex",
}
# DoD probe 4: an SSM result with NO secret-shaped text anywhere in it.
SSM_NO_SECRET_SHAPE = {
    "tool_name": "Bash",
    "command": "aws ssm send-command --instance-ids i-06169517a92b45f86 --comment lane",
    "tool_output": "CommandId 9f2a1c30-4b77-4a5b-9d02-1c66f0f1b1aa Status Success",
}
# DoD probe 6, positive control: an ordinary read with nothing sensitive.
BENIGN_GREP = {
    "tool_name": "Grep",
    "command": "grep -rn 'def handle' src/omnimarket/nodes",
    "tool_output": "src/omnimarket/nodes/node_event_emit_effect/handlers/x.py:88:def handle",
}

SECRET_LITERALS = (
    "S3cr3t-Valkey-Pw-2026",
    "aG9wZS15b3UtZGlkbnQtcmVhZC10aGlz",
    "pgpw2026",
    "postgresql://onex:pgpw2026",
)


@pytest.fixture
def permissive_contract(tmp_path: Path) -> Path:
    """A contract that declares the content fields ``capture_verbatim``.

    Exists to isolate the always-hashed CLASSES from the fail-closed default.
    If the classes did not work, these fields would cross in plaintext.
    """
    real = yaml.safe_load(default_contract_path().read_text(encoding="utf-8"))
    real["topics"][TOOL_TOPIC]["fields"].update(
        {
            "command": EnumCaptureClass.CAPTURE_VERBATIM.value,
            "tool_output": EnumCaptureClass.CAPTURE_VERBATIM.value,
        }
    )
    path = tmp_path / "permissive.yaml"
    path.write_text(yaml.safe_dump(real), encoding="utf-8")
    return path


def _rendered(result: dict[str, Any]) -> str:
    return json.dumps(result, sort_keys=True, default=str)


# ---------------------------------------------------------------------------
# DoD probes 1-4: always-hashed output classes beat the field allowlist
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("payload", "expected_class"),
    [
        (VALKEY_INCIDENT, "redis_valkey_config_get"),
        (KUBECTL_INCIDENT, "kubectl_secret_read"),
        (ENV_INCIDENT, "environment_dump"),
        (SSM_NO_SECRET_SHAPE, "ssm_send_command"),
    ],
    ids=["omn17154_valkey", "aug19_kubectl", "aug19_env", "ssm_no_secret_shape"],
)
def test_always_hashed_class_overrides_a_capture_verbatim_field(
    payload: dict[str, Any], expected_class: str, permissive_contract: Path
) -> None:
    """Probes 1-4. Declared verbatim, hashed anyway, because of what it IS."""
    contract = load_contract(permissive_contract)
    assert contract.topics[TOOL_TOPIC].fields["tool_output"] is (
        EnumCaptureClass.CAPTURE_VERBATIM
    ), "fixture must declare the content field verbatim or it proves nothing"
    assert any(c.name == expected_class for c in contract.output_classes)

    out = redact_capture(payload, TOOL_TOPIC, contract_path=permissive_contract)

    assert str(out["tool_output"]).startswith("sha256:")
    assert str(out["command"]).startswith("sha256:")
    assert out["redaction_state"] != EnumRedactionState.RAW.value


@pytest.mark.unit
@pytest.mark.parametrize(
    "payload",
    [VALKEY_INCIDENT, KUBECTL_INCIDENT, ENV_INCIDENT, SSM_NO_SECRET_SHAPE],
    ids=["omn17154_valkey", "aug19_kubectl", "aug19_env", "ssm_no_secret_shape"],
)
def test_no_incident_secret_survives_under_the_production_contract(
    payload: dict[str, Any],
) -> None:
    """No plaintext from any incident appears anywhere in the emitted record."""
    rendered = _rendered(redact_capture(payload, TOOL_TOPIC))
    for literal in SECRET_LITERALS:
        assert literal not in rendered


@pytest.mark.unit
def test_ssm_class_does_not_depend_on_a_pattern_matching(
    permissive_contract: Path,
) -> None:
    """Probe 4, stated as its own claim: the class fires on a clean output."""
    contract = load_contract(permissive_contract)
    output = str(SSM_NO_SECRET_SHAPE["tool_output"])
    assert not any(pattern.search(output) for _, pattern in contract.secret_patterns), (
        "fixture must contain no secret-shaped text or probe 4 proves nothing"
    )

    out = redact_capture(
        SSM_NO_SECRET_SHAPE, TOOL_TOPIC, contract_path=permissive_contract
    )
    assert str(out["tool_output"]).startswith("sha256:")


# ---------------------------------------------------------------------------
# DoD probe 5: fail-closed default
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_an_undeclared_tool_and_field_are_hashed_not_published() -> None:
    """Probe 5. A tool nobody has classified cannot leak by being forgotten."""
    contract = load_contract()
    assert contract.default_field_class is EnumCaptureClass.CAPTURE_HASHED

    out = redact_capture(
        {
            "session_id": "s-1",
            "tool_name": "BrandNewToolNobodyDeclared",
            "brand_new_arg_field": "sk-live-000111222333444555666",
        },
        TOOL_TOPIC,
    )
    assert out["brand_new_arg_field"] != "sk-live-000111222333444555666"
    assert str(out["brand_new_arg_field"]).startswith("sha256:")


@pytest.mark.unit
def test_deleting_a_field_entry_does_not_widen_capture(tmp_path: Path) -> None:
    """Probe 5, second half: removing a declaration narrows, never widens."""
    raw = yaml.safe_load(default_contract_path().read_text(encoding="utf-8"))
    del raw["topics"][TOOL_TOPIC]["fields"]["working_directory"]
    path = tmp_path / "narrowed.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    payload = {"session_id": "s-1", "working_directory": "omni_home"}
    assert redact_capture(payload, TOOL_TOPIC)["working_directory"] == "omni_home"
    narrowed = redact_capture(payload, TOOL_TOPIC, contract_path=path)
    assert str(narrowed["working_directory"]).startswith("sha256:")


# ---------------------------------------------------------------------------
# DoD probe 6: positive control -- the contract is not "hash everything"
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_benign_record_crosses_verbatim_per_its_allowlist() -> None:
    """Probe 6. A declared, benign field is not degraded into a hash."""
    out = redact_capture(
        {
            "session_id": "9787a4a3-ec49-4819-8bdc-5044efb94550",
            "tool_name": "Grep",
            "duration_ms": 702,
            "interrupted": False,
            "hook_source": "post_tool_use",
            "working_directory": "omni_home",
        },
        TOOL_TOPIC,
    )
    assert out["tool_name"] == "Grep"
    assert out["duration_ms"] == 702
    assert out["interrupted"] is False
    assert out["working_directory"] == "omni_home"
    assert out["session_id"] == "9787a4a3-ec49-4819-8bdc-5044efb94550"
    assert out["redaction_state"] == EnumRedactionState.RAW.value


@pytest.mark.unit
def test_benign_grep_command_matches_no_always_hashed_class(
    permissive_contract: Path,
) -> None:
    """Probe 6 against the classes: an ordinary Grep is not swept up."""
    out = redact_capture(BENIGN_GREP, TOOL_TOPIC, contract_path=permissive_contract)
    assert out["command"] == BENIGN_GREP["command"]
    assert out["tool_output"] == BENIGN_GREP["tool_output"]


# ---------------------------------------------------------------------------
# DoD probe 7: the contract cannot be substituted by a Python constant
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_no_python_side_policy_constants_in_the_resolver() -> None:
    """Probe 7. The resolver holds no field, topic, tool or pattern of its own.

    Adding a regex to a Python constant instead of a contract entry is the
    exact substitution OMN-17209 forbids -- three of its four inventoried
    mechanisms were hardcoded tuples, two of them hand-synced copies.
    """
    source = (
        Path(__file__).resolve().parents[4]
        / "src/omnimarket/nodes/node_event_emit_effect/redaction.py"
    ).read_text(encoding="utf-8")
    code = "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith("#")
    )
    # Strip docstrings so prose may name things the code may not encode.
    code = re.sub(r'"""(?:.|\n)*?"""', "", code)

    contract = load_contract()
    forbidden: list[str] = [
        *contract.topics,
        *(c.name for c in contract.output_classes),
        *(n for n, _ in contract.secret_patterns),
        *contract.content_fields,
        *contract.command_fields,
        contract.tool_name_field,
        contract.redaction_state_field,
        *contract.topics[TOOL_TOPIC].fields,
        *contract.topics[PROMPT_TOPIC].fields,
    ]
    leaked = sorted({token for token in forbidden if f'"{token}"' in code})
    assert not leaked, (
        f"redaction.py encodes contract policy as Python literals: {leaked}. "
        "Policy belongs in contracts/capture_redaction.yaml."
    )
    assert "re.compile(" in code, "patterns must be compiled from the contract"
    for _, pattern in contract.secret_patterns:
        assert pattern.pattern not in code


# ---------------------------------------------------------------------------
# OMN-17399 AC: mandatory redaction_state
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_every_governed_record_carries_a_declared_redaction_state() -> None:
    field = load_contract().redaction_state_field
    for topic in (TOOL_TOPIC, PROMPT_TOPIC):
        out = redact_capture({"session_id": "s-1"}, topic)
        assert field in out
        assert out[field] in {s.value for s in EnumRedactionState}


@pytest.mark.unit
def test_a_producer_supplied_redaction_state_is_overridden_not_trusted() -> None:
    """An injected `raw` claim must not survive -- probes the AC's inverse."""
    out = redact_capture(
        {"session_id": "s-1", "redaction_state": "raw", "unclassified": "x"},
        TOOL_TOPIC,
    )
    assert out["redaction_state"] == EnumRedactionState.REDACTED.value


@pytest.mark.unit
def test_a_secret_pattern_hit_escalates_the_state_to_secret_detected() -> None:
    out = redact_capture(
        {
            "session_id": "s-1",
            "working_directory": "postgresql://onex:pgpw2026@host/db",
        },
        TOOL_TOPIC,
    )
    assert out["redaction_state"] == EnumRedactionState.SECRET_DETECTED.value
    assert str(out["working_directory"]).startswith("sha256:")
    assert "pgpw2026" not in _rendered(out)


@pytest.mark.unit
def test_redaction_state_values_match_omnibase_core() -> None:
    """The enum is re-declared for import cost (OMN-17224); parity is asserted."""
    from omnibase_core.enums.artifacts.enum_artifact_redaction_state import (
        EnumArtifactRedactionState,
    )

    assert {s.value for s in EnumRedactionState} == {
        s.value for s in EnumArtifactRedactionState
    }


# ---------------------------------------------------------------------------
# Determinism / replay
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_a_redacted_record_is_deterministic_and_replayable() -> None:
    """Same input, byte-identical output -- twice, and against a pinned hash."""
    first = redact_capture(VALKEY_INCIDENT, TOOL_TOPIC)
    second = redact_capture(dict(VALKEY_INCIDENT), TOOL_TOPIC)
    assert _rendered(first) == _rendered(second)

    # Pinned so a future change to the canonical form is a visible break, not
    # a silent one that makes every historical record unreplayable.
    import hashlib

    expected = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(
                VALKEY_INCIDENT["tool_output"],
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        ).hexdigest()
    )
    assert first["tool_output"] == expected


@pytest.mark.unit
def test_the_hash_is_unsalted_so_replay_reproduces_it() -> None:
    """A salt would make the record unreplayable; assert none is applied."""
    a = redact_capture({"session_id": "s", "x": "v"}, TOOL_TOPIC)
    b = redact_capture({"session_id": "s", "x": "v"}, TOOL_TOPIC)
    assert a["x"] == b["x"]


# ---------------------------------------------------------------------------
# never_capture / capture_shape_only
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_prompt_body_is_dropped_not_hashed() -> None:
    """A hash of a short prompt is a lookup handle, so the field is dropped."""
    out = redact_capture(
        {
            "session_id": "s-1",
            "prompt": "rotate the valkey requirepass to hunter2",
            "prompt_b64": "cm90YXRl",
            "prompt_length": 41,
        },
        PROMPT_TOPIC,
    )
    assert "prompt" not in out
    assert "prompt_b64" not in out
    assert out["prompt_length"] == 41
    assert "hunter2" not in _rendered(out)


@pytest.mark.unit
def test_prompt_preview_is_reduced_to_its_shape() -> None:
    out = redact_capture(
        {"session_id": "s-1", "prompt_preview": "AKIAIOSFODNN7EXAMPLE and more"},
        PROMPT_TOPIC,
    )
    assert out["prompt_preview"] == {"type": "str", "length": 29}
    assert "AKIA" not in _rendered(out)


# ---------------------------------------------------------------------------
# Fail-closed refusals
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_a_topic_naming_the_transform_but_absent_from_the_contract_is_refused() -> None:
    with pytest.raises(UngovernedTopicError) as excinfo:
        redact_capture({"a": 1}, "onex.evt.omniclaude.tool-output-captured.v1")
    assert "tool-output-captured" in str(excinfo.value)


@pytest.mark.unit
def test_the_topic_scoped_transform_refuses_a_missing_topic() -> None:
    with pytest.raises(TopicScopedTransformError):
        apply_transform("redact_capture", {"a": 1})


@pytest.mark.unit
@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda d: d.pop("default_field_class"), "missing default"),
        (lambda d: d["topics"][TOOL_TOPIC].update({"fields": {}}), "empty policy"),
        (lambda d: d["always_hashed_output_classes"][0].pop("reason"), "no reason"),
        (
            lambda d: d["secret_patterns"][0].update({"pattern": "(unclosed"}),
            "bad regex",
        ),
        (
            lambda d: d["topics"][TOOL_TOPIC]["fields"].update(
                {"session_id": "capture_maybe"}
            ),
            "unknown class",
        ),
    ],
    ids=[
        "missing_default",
        "empty_policy",
        "class_no_reason",
        "bad_regex",
        "bad_class",
    ],
)
def test_a_malformed_contract_is_refused_not_defaulted(
    mutate: Any, reason: str, tmp_path: Path
) -> None:
    raw = yaml.safe_load(default_contract_path().read_text(encoding="utf-8"))
    mutate(raw)
    path = tmp_path / f"broken-{reason.replace(' ', '-')}.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(MalformedRedactionContractError):
        redact_capture({"session_id": "s"}, TOOL_TOPIC, contract_path=path)


# ---------------------------------------------------------------------------
# Registry <-> contract binding, and the partition key
# ---------------------------------------------------------------------------


def _registry() -> dict[str, Any]:
    path = (
        Path(__file__).resolve().parents[4]
        / "src/omnimarket/nodes/node_emit_daemon/registries/topics.yaml"
    )
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@pytest.mark.unit
def test_every_governed_topic_is_declared_with_the_transform_in_the_registry() -> None:
    """The contract and the fan-out rules cannot drift apart, either way."""
    declared = {
        rule["topic"]
        for event in _registry()["events"].values()
        if isinstance(event, dict)
        for rule in event.get("fan_out", [])
        if rule.get("transform") == "redact_capture"
    }
    assert declared == set(load_contract().topics)


@pytest.mark.unit
def test_the_transform_is_registered_in_both_registries_as_one_callable() -> None:
    """Node-only redaction would read as an OMN-16048 parity break, not a control."""
    assert TOPIC_SCOPED_TRANSFORM_REGISTRY["redact_capture"] is redact_capture
    assert DAEMON_TOPIC_SCOPED["redact_capture"] is redact_capture


@pytest.mark.unit
def test_the_partition_key_field_survives_redaction_verbatim() -> None:
    """session_id is the declared partition key and the key is derived POST
    transform -- hashing it would silently repartition the topic."""
    for topic in (TOOL_TOPIC, PROMPT_TOPIC):
        policy = load_contract().topics[topic]
        assert policy.fields["session_id"] is EnumCaptureClass.CAPTURE_VERBATIM
    for event in ("tool.executed", "prompt.submitted"):
        assert _registry()["events"][event]["partition_key_field"] == "session_id"


@pytest.mark.unit
def test_the_contract_governs_every_field_the_live_hooks_emit() -> None:
    """Pins the live payload shape read off .201 stability 2026-09-04T14:22Z.

    A field the hooks emit that the contract does not name would be hashed --
    correct, but a silent degradation of working telemetry rather than a
    decision. This fails when the hooks widen without the contract following.
    """
    live = {
        TOOL_TOPIC: {
            "duration_ms",
            "hook_source",
            "interrupted",
            "session_id",
            "tool_name",
            "working_directory",
        },
        PROMPT_TOPIC: {
            "hook_source",
            "prompt_length",
            "session_id",
            "working_directory",
        },
    }
    enrichment = {
        "correlation_id",
        "causation_id",
        "emitted_at",
        "entity_id",
        "schema_version",
    }
    contract = load_contract()
    for topic, fields in live.items():
        declared = set(contract.topics[topic].fields)
        assert (fields | enrichment) <= declared, (
            f"{topic}: undeclared live fields {(fields | enrichment) - declared}"
        )


# ---------------------------------------------------------------------------
# OMN-17969: the secret scrub must see a value that is not a string
# ---------------------------------------------------------------------------
#
# `_matches_secret` early-returned None for any non-str value, so a credential
# nested in a list or a dict under a `capture_verbatim` field crossed the seam
# unchanged and was stamped `raw`. The scrub is the ONE control that runs on
# top of `capture_verbatim`, and the contract says so in terms: "Applied ON TOP
# of every class, including capture_verbatim." The sibling class matcher in the
# same module already canonicalises a non-str candidate before matching.
#
# The three shapes below are the ones a widened producer (OMN-17206 tool args,
# OMN-17207 diff bodies) would actually emit: a list, a nested dict, and a dict
# whose VALUE alone is secret-shaped with no key framing to help.

# Documentation-reserved host (RFC 2606 `.invalid`) and AWS's own published
# example key id -- neither resolves to anything and neither is a live value.
NESTED_URL_SECRET = "postgresql://onex:pgpw2026@db.example.invalid:5432/onex"
NESTED_AWS_KEY_ID = "AKIA" + "IOSFODNN7EXAMPLE"

NESTED_VERBATIM_SHAPES: list[tuple[str, Any]] = [
    ("list", ["/repo/omnimarket", NESTED_URL_SECRET]),
    ("nested_dict", {"env": {"DATABASE_URL": NESTED_URL_SECRET}}),
    ("dict_with_aws_key_shaped_value", {"profile": "default", "id": NESTED_AWS_KEY_ID}),
]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("shape", "value"),
    NESTED_VERBATIM_SHAPES,
    ids=[shape for shape, _ in NESTED_VERBATIM_SHAPES],
)
def test_secret_nested_under_a_verbatim_field_is_scrubbed(
    shape: str, value: Any
) -> None:
    """A container under a `capture_verbatim` field is not exempt from the scrub."""
    contract = load_contract()
    assert contract.topics[TOOL_TOPIC].fields["working_directory"] is (
        EnumCaptureClass.CAPTURE_VERBATIM
    ), "fixture must ride a verbatim field or it proves nothing"

    out = redact_capture(
        {"session_id": "s-1", "tool_name": "Grep", "working_directory": value},
        TOOL_TOPIC,
    )

    assert str(out["working_directory"]).startswith("sha256:"), (
        f"{shape}: a nested credential crossed unchanged"
    )
    assert out["redaction_state"] == EnumRedactionState.SECRET_DETECTED.value
    rendered = _rendered(out)
    for literal in (NESTED_URL_SECRET, NESTED_AWS_KEY_ID, "pgpw2026"):
        assert literal not in rendered


@pytest.mark.unit
def test_a_match_anywhere_in_a_container_redacts_the_whole_field() -> None:
    """Fail-closed: one bad leaf does not get to travel with its clean siblings."""
    out = redact_capture(
        {
            "session_id": "s-1",
            "tool_name": "Grep",
            "working_directory": {
                "cwd": "/repo/omnimarket",
                "branch": "jonah/omn-17969",
                "leaked": {"deep": [NESTED_URL_SECRET]},
            },
        },
        TOOL_TOPIC,
    )
    rendered = _rendered(out)
    assert str(out["working_directory"]).startswith("sha256:")
    assert "/repo/omnimarket" not in rendered
    assert "jonah/omn-17969" not in rendered


@pytest.mark.unit
def test_a_benign_container_under_a_verbatim_field_still_crosses_verbatim() -> None:
    """Positive control: the widened scrub does not degrade into hash-everything."""
    benign = {"cwd": "/repo/omnimarket", "parts": ["repo", "omnimarket"], "depth": 2}
    out = redact_capture(
        {"session_id": "s-1", "tool_name": "Grep", "working_directory": benign},
        TOOL_TOPIC,
    )
    assert out["working_directory"] == benign
    assert out["redaction_state"] == EnumRedactionState.RAW.value
