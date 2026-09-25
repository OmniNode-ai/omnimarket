# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Full-content capture under the redaction contract (OMN-19550).

The operator ruled on 2026-09-25 that full content is captured: the complete
prompt, tool input, tool result and assistant reply. Before this ticket the
contract had one way to handle a content field that held a secret: hash the
WHOLE field. On a 60 KB tool result that throws away everything the ruling asks
for, because of one line. ``capture_scrubbed`` replaces only the matched span.

Every planted value below is built by concatenation and carries the token
``FAKE``, so no real-looking credential sits in this file as a literal and a
test can assert the value is gone by searching for one word.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_event_emit_effect.redaction import (
    EnumCaptureClass,
    EnumRedactionState,
    default_contract_path,
    load_contract,
    plan_content_chunks,
    redact_capture,
    scrub_text,
)
from omnimarket.nodes.node_event_emit_effect.spool.topic_resolver import (
    resolve_event_type,
)

CONTENT_TOPIC = "onex.evt.omniclaude.content-captured.v1"
CONTENT_EVENT = "content.captured"

_F = "FAKE"


def _alnum(n: int) -> str:
    """``n`` characters that satisfy an alphanumeric token shape and hold FAKE."""
    return (_F + "a1b2c3d4e5" * 20)[:n]


# name -> (surrounding text before, planted secret, surrounding text after)
PLANTED: dict[str, tuple[str, str, str]] = {
    "authorization_header": (
        "curl -H '",
        "Authorization: " + "Bearer " + _alnum(40),
        "' https://api.example.test/v1",
    ),
    "credential_header": ("GET /x HTTP/1.1\n", "x-api-key: " + _alnum(32), "\nHost: h"),
    "cookie_header": ("resp:\n", "Set-Cookie: " + "session=" + _alnum(30), "\nok"),
    "url_authority_credentials": (
        "dsn = ",
        "postgresql://svc:" + _alnum(16) + "@",
        "db.internal:5432/app",
    ),
    "bearer_token": ("token was ", "bearer " + _alnum(32), " in the log"),
    "github_token": ("gh auth uses ", "ghp" + "_" + _alnum(36), " for CI"),
    "github_fine_grained_token": (
        "fine grained: ",
        "github" + "_pat_" + _alnum(60),
        " end",
    ),
    "anthropic_key": ("ANTHROPIC ", "sk-" + "ant-api03-" + _alnum(40), " end"),
    "openai_key": ("OPENAI ", "sk-" + "proj-" + _alnum(40), " end"),
    "slack_token": ("slack ", "xox" + "b-1234-5678-" + _alnum(24), " end"),
    "linear_api_key": ("linear ", "lin" + "_api_" + _alnum(40), " end"),
    "google_api_key": ("maps ", "AI" + "za" + _alnum(35), " end"),
    "stripe_key": ("stripe ", "sk" + "_live_" + _alnum(24), " end"),
    "aws_access_key_id": ("aws ", "AK" + "IA" + "FAKEFAKEFAKEFAKE", " end"),
    "jwt": (
        "jwt ",
        "ey" + "J" + _alnum(20) + "." + _alnum(20) + "." + _alnum(20),
        " end",
    ),
    "json_secret_key": ('{"user": "svc", ', '"db_password": "' + _alnum(12) + '"', "}"),
    "camel_case_secret_key": ('{"a": 1, ', '"clientSecret": "' + _alnum(12) + '"', "}"),
    "labelled_secret_assignment": ("export ", "API_KEY=" + _alnum(20), "\nnext line"),
    "pem_private_key_block": (
        "key file:\n",
        "-----BEGIN RSA PRIVATE KEY-----\n"
        + _alnum(64)
        + "\n"
        + _alnum(64)
        + "\n-----END RSA PRIVATE KEY-----",
        "\nafter the key",
    ),
}


def _content_record(content: str, **extra: Any) -> dict[str, Any]:
    return {
        "session_id": "s-19550",
        "turn_id": "s-19550:turn-3",
        "correlation_id": "s-19550",
        "content_kind": "tool_response",
        "tool_name": "Bash",
        "tool_use_id": "toolu_19550",
        "command": "cat notes.txt",
        "chunk_index": 0,
        "chunk_count": 1,
        "content": content,
        **extra,
    }


# ---------------------------------------------------------------------------
# AC1 -- the topic is routed through the transform and governed
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_content_captured_routes_through_redact_capture_on_session_id() -> None:
    registry_path = (
        default_contract_path().parents[2]
        / "node_emit_daemon"
        / "registries"
        / "topics.yaml"
    )
    events = yaml.safe_load(registry_path.read_text(encoding="utf-8"))["events"]
    entry = events[CONTENT_EVENT]
    assert entry["partition_key_field"] == "session_id"
    assert [(r["topic"], r.get("transform")) for r in entry["fan_out"]] == [
        (CONTENT_TOPIC, "redact_capture")
    ]
    targets = resolve_event_type(CONTENT_EVENT)
    assert [(t.topic, t.transform_name) for t in targets] == [
        (CONTENT_TOPIC, "redact_capture")
    ]


@pytest.mark.unit
def test_content_captured_topic_is_governed_with_scrubbed_content_fields() -> None:
    policy = load_contract().topics[CONTENT_TOPIC]
    assert policy.fields["content"] is EnumCaptureClass.CAPTURE_SCRUBBED
    assert policy.fields["command"] is EnumCaptureClass.CAPTURE_SCRUBBED
    assert policy.fields["headers"] is EnumCaptureClass.NEVER_CAPTURE
    assert policy.fields["authorization"] is EnumCaptureClass.NEVER_CAPTURE
    for join_key in ("session_id", "turn_id", "correlation_id", "tool_use_id"):
        assert policy.fields[join_key] is EnumCaptureClass.CAPTURE_VERBATIM


@pytest.mark.unit
def test_content_captured_join_keys_survive_verbatim() -> None:
    out = redact_capture(_content_record("plain text"), CONTENT_TOPIC)
    for key in ("session_id", "turn_id", "correlation_id", "tool_use_id"):
        assert out[key] == _content_record("x")[key]
    assert out["content"] == "plain text"
    assert out["redaction_state"] == EnumRedactionState.REDACTED.value


# ---------------------------------------------------------------------------
# AC2 -- span scrub: the secret goes, the text around it stays
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("name", sorted(PLANTED))
def test_scrubbed_removes_each_planted_secret_and_keeps_its_context(name: str) -> None:
    before, secret, after = PLANTED[name]
    out = redact_capture(_content_record(before + secret + after), CONTENT_TOPIC)

    content = out["content"]
    assert isinstance(content, str)
    assert _F not in content, f"{name}: planted value survived: {content!r}"
    assert "[REDACTED:" in content
    # The context is kept: this is the whole difference from capture_hashed.
    assert content.startswith(before.rstrip("'").rstrip()[:4])
    assert after.strip()[-3:] in content
    assert out["redaction_state"] == EnumRedactionState.SECRET_DETECTED.value


@pytest.mark.unit
def test_scrubbed_names_the_pattern_that_fired_and_never_the_value() -> None:
    text = "before " + PLANTED["github_token"][1] + " after"
    scrubbed, hits = scrub_text(text)
    assert scrubbed == "before [REDACTED:github_token] after"
    assert hits == {"github_token": 1}


@pytest.mark.unit
def test_scrubbed_is_idempotent_so_the_per_chunk_fan_out_changes_nothing() -> None:
    corpus = "\n".join(b + s + a for b, s, a in PLANTED.values())
    once, first_hits = scrub_text(corpus)
    twice, second_hits = scrub_text(once)
    assert first_hits
    assert twice == once
    assert second_hits == {}


@pytest.mark.unit
def test_every_declared_secret_pattern_has_a_planted_positive_control() -> None:
    """A pattern with no fixture is a pattern nobody has seen fire."""
    # Matched pattern by pattern, not through scrub_text: the scrub applies
    # patterns in order, so a broader earlier one (json_secret_key) removes a
    # value a narrower later one (camel_case_secret_key) would also have
    # caught. Both must still be shown to fire on something.
    never_fired = [
        name
        for name, pattern in load_contract().secret_patterns
        if not any(pattern.search(secret) for _, secret, _ in PLANTED.values())
    ]
    assert not never_fired, f"patterns with no positive control: {never_fired}"


@pytest.mark.unit
def test_scrub_marker_matches_no_pattern() -> None:
    contract = load_contract()
    assert contract.scrub_marker is not None
    for name, _pattern in contract.secret_patterns:
        marker = contract.scrub_marker.format(name=name)
        for _, other in contract.secret_patterns:
            assert not other.search(marker), (name, other.pattern)


@pytest.mark.unit
def test_benign_code_and_prose_cross_the_scrub_unchanged() -> None:
    benign = (
        "def handle(request):\n"
        "    return {'rows': 3, 'tool_name': 'Bash'}\n"
        "The tokenizer splits text; see docs/secrets-policy.md for the rule.\n"
        "git log --oneline -5 && uv run pytest tests -q\n"
    )
    scrubbed, hits = scrub_text(benign)
    assert scrubbed == benign
    assert hits == {}


@pytest.mark.unit
def test_scrubbed_applies_to_string_leaves_of_a_structured_value() -> None:
    secret = PLANTED["github_token"][1]
    record = _content_record("x", command={"argv": ["gh", "--token", secret]})
    out = redact_capture(record, CONTENT_TOPIC)
    assert out["command"] == {"argv": ["gh", "--token", "[REDACTED:github_token]"]}


@pytest.mark.unit
def test_auth_header_fields_are_dropped_whatever_they_hold() -> None:
    record = _content_record(
        "ok", headers={"Accept": "json"}, authorization="Basic abc"
    )
    out = redact_capture(record, CONTENT_TOPIC)
    assert "headers" not in out
    assert "authorization" not in out


@pytest.mark.unit
def test_an_unclassified_field_on_the_content_topic_is_still_hashed() -> None:
    out = redact_capture(_content_record("ok", surprise="value"), CONTENT_TOPIC)
    assert str(out["surprise"]).startswith("sha256:")


@pytest.mark.unit
def test_producer_redaction_counts_survive_verbatim() -> None:
    hits = {"github_token": 2, "labelled_secret_assignment": 1}
    out = redact_capture(_content_record("ok", producer_redaction=hits), CONTENT_TOPIC)
    assert out["producer_redaction"] == hits


# ---------------------------------------------------------------------------
# AC3 -- an always-hashed output class still beats the scrubbed class
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("tool_name", "command"),
    [
        ("Bash", "kubectl get secret onex-api-oidc -n onex-dev -o json"),
        ("Bash", "aws ssm send-command --document-name AWS-RunShellScript"),
        ("Bash", "env | grep -i POSTGRES"),
        ("Read", "/repo/.env"),
    ],
    ids=["kubectl_secret", "aws_ssm", "env_dump", "dotenv_read"],
)
def test_content_output_class_hashes_command_and_content_whole(
    tool_name: str, command: str
) -> None:
    # No secret-shaped text at all: the class hashes for what the call IS.
    record = _content_record(
        "NAME  TYPE  DATA\nplain output", tool_name=tool_name, command=command
    )
    out = redact_capture(record, CONTENT_TOPIC)
    assert str(out["content"]).startswith("sha256:")
    assert str(out["command"]).startswith("sha256:")
    assert "plain output" not in json.dumps(out)
    # The join keys are not content and still cross.
    assert out["tool_use_id"] == "toolu_19550"


# ---------------------------------------------------------------------------
# AC4 -- the size policy
# ---------------------------------------------------------------------------


def _policy() -> Any:
    return load_contract().topics[CONTENT_TOPIC].content_policy


@pytest.mark.unit
def test_content_size_policy_is_declared_in_the_contract() -> None:
    policy = _policy()
    assert policy is not None
    assert 0 < policy.chunk_chars <= 65536
    assert policy.max_content_chars >= policy.chunk_chars
    assert policy.max_content_chars % policy.chunk_chars == 0


@pytest.mark.unit
def test_content_size_policy_splits_into_ordered_chunks_sharing_one_hash() -> None:
    policy = _policy()
    text = "".join(chr(ord("a") + i % 26) for i in range(policy.chunk_chars * 2 + 17))
    plan = plan_content_chunks(text, CONTENT_TOPIC)

    assert len(plan.chunks) == 3
    assert "".join(plan.chunks) == text
    assert all(len(c) <= policy.chunk_chars for c in plan.chunks)
    assert plan.original_chars == len(text)
    assert plan.truncated is False
    assert plan.content_sha256 == hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.mark.unit
def test_content_size_policy_marks_content_over_the_cap_truncated() -> None:
    policy = _policy()
    text = "x" * (policy.max_content_chars + 5)
    plan = plan_content_chunks(text, CONTENT_TOPIC)

    assert plan.truncated is True
    assert plan.original_chars == len(text)
    kept = "".join(plan.chunks)
    assert len(kept) == policy.max_content_chars
    # The hash covers what was kept, so a reader can verify a reassembly.
    assert plan.content_sha256 == hashlib.sha256(kept.encode("utf-8")).hexdigest()


@pytest.mark.unit
def test_content_size_policy_empty_content_is_one_empty_chunk() -> None:
    plan = plan_content_chunks("", CONTENT_TOPIC)
    assert plan.chunks == ("",)
    assert plan.original_chars == 0
    assert plan.truncated is False


@pytest.mark.unit
def test_content_size_policy_cuts_an_oversize_record_at_the_fan_out() -> None:
    """A producer that did not chunk cannot put an oversize record on the bus."""
    policy = _policy()
    record = _content_record("y" * (policy.chunk_chars + 100), truncated=False)
    out = redact_capture(record, CONTENT_TOPIC)
    assert len(str(out["content"])) == policy.chunk_chars
    assert out["truncated"] is True


@pytest.mark.unit
def test_content_size_policy_on_a_topic_without_one_is_refused() -> None:
    with pytest.raises(ValueError, match="content_policy"):
        plan_content_chunks("x", "onex.evt.omniclaude.tool-executed.v1")


# ---------------------------------------------------------------------------
# every pattern stays linear on full content
# ---------------------------------------------------------------------------

_ADVERSARIAL = {
    "alnum_run": "y" * 400_000,
    "quoted_alnum_run": '"' + "a" * 400_000,
    "quoted_lower_run": '"' + "b" * 400_000 + '"',
    "word_run": "ab1 " * 100_000,
    "scheme_like_run": "http" * 100_000,
}


@pytest.mark.unit
@pytest.mark.parametrize("name", sorted(_ADVERSARIAL))
def test_scrubbed_every_pattern_is_linear_on_a_long_input(name: str) -> None:
    """Full content made a backtracking pattern a stall (OMN-19550).

    The unbounded url_authority_credentials took 1.2 s on 40,000 characters and
    grows quadratically, so one 2 MiB tool result would have taken about 50
    minutes. 400,000 characters of each shape must scan in well under a second
    per pattern; a quadratic pattern takes minutes here.
    """
    text = _ADVERSARIAL[name]
    for pattern_name, pattern in load_contract().secret_patterns:
        started = time.perf_counter()
        pattern.search(text)
        elapsed = time.perf_counter() - started
        assert elapsed < 1.0, f"{pattern_name} took {elapsed:.2f}s on {name}"
