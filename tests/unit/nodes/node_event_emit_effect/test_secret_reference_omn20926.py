# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Secret references survive the fan-out scrub (OMN-20926).

The producer replaces a detected secret with a reference to the secret store.
The emit effect's fan-out applies the contract again to every chunk, and one
declared pattern (``labelled_secret_assignment``) matches the reference's own
scheme, so without a keep rule the fan-out would scrub every reference the
producer minted. These tests hold the keep rule to its two halves: a reference
stands, and a credential beside one is still removed.

Planted credentials are built by concatenation and carry ``FAKE``.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_event_emit_effect.errors import (
    MalformedRedactionContractError,
)
from omnimarket.nodes.node_event_emit_effect.redaction import (
    default_contract_path,
    load_contract,
    plan_content_chunks,
    redact_capture,
    scrub_text,
)

CONTENT_TOPIC = "onex.cmd.omniintelligence.content-captured.v1"
_GH = "ghp" + "_" + ("FAKE" + "a1b2c3d4e5" * 5)[:40]


def _ref(seed: str = "one") -> str:
    reference = load_contract().secret_reference
    assert reference is not None
    return reference.reference_for(hashlib.sha256(seed.encode()).hexdigest())


@pytest.mark.unit
def test_secret_reference_is_declared_and_matches_its_own_template() -> None:
    reference = load_contract().secret_reference
    assert reference is not None
    assert reference.pattern.fullmatch(_ref()) is not None
    assert "{digest}" in reference.store_key_template


@pytest.mark.unit
@pytest.mark.parametrize(
    "text",
    [
        "{ref}",
        "the key is {ref} now",
        "password={ref}",
        'payload {{"api_token": "{ref}"}}',
        "curl -H 'Authorization: {ref}' https://x.test",
        "dsn postgresql://svc:{ref}@db.internal:5432/app",
        "{ref}\n{ref}",
    ],
)
def test_secret_reference_survives_the_fan_out_scrub(text: str) -> None:
    planted = text.format(ref=_ref())
    scrubbed, hits = scrub_text(planted)
    assert scrubbed == planted
    assert hits == {}


@pytest.mark.unit
def test_secret_reference_does_not_shelter_a_credential_beside_it() -> None:
    scrubbed, hits = scrub_text(f"{_ref()} and {_GH} after")
    assert _GH not in scrubbed
    assert _ref() in scrubbed
    assert hits == {"github_token": 1}


@pytest.mark.unit
def test_secret_reference_text_after_a_reference_is_still_scanned() -> None:
    # The labelled pattern matches the reference's own scheme; the scan must
    # resume after the reference, not skip the rest of the match.
    tail = "password=" + "FAKE" + "hunter22"
    scrubbed, hits = scrub_text(_ref() + "," + tail)
    assert scrubbed.startswith(_ref() + ",")
    assert "FAKE" not in scrubbed
    assert hits == {"labelled_secret_assignment": 1}


@pytest.mark.unit
def test_secret_reference_overlap_with_a_credential_fails_closed() -> None:
    # The label's value runs past the reference into a credential, so the
    # match is not a reference: it is scrubbed whole, reference included.
    text = "token=" + _ref() + "," + "sk-" + ("FAKE" + "x" * 30)
    scrubbed, _ = scrub_text(text)
    assert "FAKE" not in scrubbed


@pytest.mark.unit
@pytest.mark.parametrize(
    "lookalike",
    [
        lambda r: r.upper(),  # uppercase hex is not a reference
        lambda r: r[:-1],  # 63 hex digits
        lambda r: r + "z",  # runs on into a word character
        lambda r: r.replace("captured", "other"),  # another namespace
    ],
)
def test_secret_reference_lookalikes_are_not_protected(lookalike: object) -> None:
    assert callable(lookalike)
    text = str(lookalike(_ref()))
    scrubbed, hits = scrub_text("x " + text)
    assert hits, f"a look-alike crossed unscrubbed: {scrubbed}"


@pytest.mark.unit
def test_secret_reference_and_store_outcome_cross_the_content_topic() -> None:
    record = {
        "session_id": "s-1",
        "content_kind": "tool_response",
        "content": "token=" + _ref() + " then " + _GH,
        "stored_refs": {"stored": 1, "fallback": 1, "reason": "write_refused"},
    }
    out = redact_capture(record, CONTENT_TOPIC)
    assert _ref() in out["content"]
    assert _GH not in out["content"]
    assert out["stored_refs"] == record["stored_refs"]


@pytest.mark.unit
def test_secret_reference_value_groups_sit_inside_their_match() -> None:
    contract = load_contract()
    reference = contract.secret_reference
    assert reference is not None
    grouped = [
        name
        for name, pattern in contract.secret_patterns
        if reference.value_group in pattern.groupindex
    ]
    assert set(grouped) >= {
        "authorization_header",
        "credential_header",
        "url_authority_credentials",
        "bearer_token",
        "json_secret_key",
        "camel_case_secret_key",
        "labelled_secret_assignment",
    }


def _write_variant(tmp_path: Path, **overrides: object) -> Path:
    raw = yaml.safe_load(default_contract_path().read_text(encoding="utf-8"))
    raw["secret_reference"] = {**raw["secret_reference"], **overrides}
    path = tmp_path / "capture_redaction.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return path


@pytest.mark.unit
@pytest.mark.parametrize(
    "overrides",
    [
        {"template": "secret://onex/elsewhere/{digest}"},
        {"template": "secret://onex/captured/fixed"},
        {"store_key_template": "CAPTURED"},
        {"value_group": ""},
        {"pattern": "("},
    ],
)
def test_secret_reference_block_is_fail_closed(
    tmp_path: Path, overrides: dict[str, object]
) -> None:
    with pytest.raises(MalformedRedactionContractError):
        load_contract(_write_variant(tmp_path, **overrides))


@pytest.mark.unit
def test_chunking_never_cuts_a_reference(tmp_path: Path) -> None:
    raw = yaml.safe_load(default_contract_path().read_text(encoding="utf-8"))
    policy = raw["topics"][CONTENT_TOPIC]["content_policy"]
    policy["chunk_chars"] = 100
    policy["max_content_chars"] = 250
    path = tmp_path / "capture_redaction.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    ref = _ref()
    # The first reference straddles the 100-character chunk boundary and the
    # second straddles the 250-character cap.
    text = "a" * 60 + " " + ref + " " + "b" * 70 + " " + ref + " " + "c" * 50
    plan = plan_content_chunks(text, CONTENT_TOPIC, contract_path=path)
    assert all(len(chunk) <= 100 for chunk in plan.chunks)
    joined = "".join(plan.chunks)
    assert joined == text[: len(joined)]
    assert plan.truncated is True
    assert joined.count(ref) == 1, "the cap kept part of the second reference"
    assert joined.count("secret:") == 1
    assert sum(chunk.count(ref) for chunk in plan.chunks) == 1
