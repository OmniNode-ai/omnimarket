# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Publisher <-> consumer seam for the occ-companion-effect command (OMN-14941).

The occ-autobind born-path bug (OMN-13990) was a publisher payload that never
validated against its consumer model, so every command was silently DLQ'd and
the effect never fired. These tests pin the NEW publisher's seam the hard way:
the ACTUAL ``--dry-run`` CLI output (the exact JSON the workflow would put on
the wire) is fed to ``ModelOccCompanionEffectRequest.model_validate`` in the
SAME test, asserting:

* ``mode == "mutate"`` — the model defaults to ``dry_run`` (fail-safe), so an
  omitted mode is a silent never-mint (the optional-input-silent-skip trap);
* ``pr_number`` is an ``int`` (GHA env is a string; the publisher casts);
* runner/verifier take the model defaults and differ (OMN-12791);
* every legacy occ-autobind field is REJECTED (``extra='forbid'``).

Plus: publisher-side idempotency (an already-bound PR body skips the publish
loudly), the Kafka key/topic shape, and the OMN-14639 fail-loud flush.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path
from uuid import UUID

import pytest
import yaml
from click.testing import CliRunner
from pydantic import ValidationError

from omnimarket.nodes.node_occ_companion_effect.models.model_occ_companion_effect_request import (
    ModelOccCompanionEffectRequest,
)

_SCRIPT = (
    Path(__file__).resolve().parents[4]
    / "scripts"
    / "publish_occ_companion_effect_command.py"
)
_CONTRACT = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_occ_companion_effect"
    / "contract.yaml"
)

_LEGACY_AUTOBIND_FIELDS = (
    "block_reason",
    "ticket_id",
    "requested_at",
    "pr_head_sha",
    "event_id",
    "topic",
)


def _poisoned_fetch(url: str, token: str) -> object | None:
    raise AssertionError(
        "the publisher attempted a LIVE GitHub read during a unit test; inject "
        f"the resolution seam with _stub_resolution(module, ...) — url={url!r}"
    )


def _load_publisher() -> object:
    spec = importlib.util.spec_from_file_location(
        "publish_occ_companion_effect_command", _SCRIPT
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # OMN-15615: no unit test may reach api.github.com. Opt IN to a stub.
    # The real resolver is kept reachable so the transport-error test can
    # exercise the shipped function itself rather than a re-implementation.
    # getattr, not attribute access: the RED demonstration for OMN-15615 runs
    # this same harness against the PRE-FIX script, which has no resolver at
    # all. A harness AttributeError there would fail every test for a reason
    # unrelated to the defect and make the RED unreadable.
    module._live_github_get_json = getattr(module, "_github_get_json", _poisoned_fetch)
    module._github_get_json = _poisoned_fetch
    return module


def _required_pr_env(**overrides: str) -> dict[str, str]:
    env = {
        "PR_REPO": "OmniNode-ai/omnimarket",
        "PR_NUMBER": "42",
        "PR_HEAD_SHA": "a" * 40,
        "PR_BODY": "Closes OMN-14941",
    }
    env.update(overrides)
    return env


# --- OMN-15615 citation-resolution seam ------------------------------------
#
# The publisher resolves an Evidence-Source citation against the LIVE
# onex_change_control companion set for the product PR. Every test below
# injects that read; no test in this file touches the network. A test that
# forgets to inject would silently make a real API call, so `_load_publisher`
# installs a poisoned fetcher by default — an un-stubbed resolution raises.


class _FetchRecorder:
    """Stands in for the publisher's GitHub read. Records every URL."""

    def __init__(self, payload: object | None) -> None:
        self.payload = payload
        self.urls: list[str] = []
        self.tokens: list[str] = []

    def __call__(self, url: str, token: str) -> object | None:
        self.urls.append(url)
        self.tokens.append(token)
        return self.payload


def _companion_payload(
    *,
    number: int,
    state: str = "open",
    merged_at: str | None = None,
    head_sha: str = "d" * 40,
    merge_commit_sha: str | None = None,
) -> dict[str, object]:
    """One element of the GitHub ``GET /repos/{occ}/pulls?head=...`` payload."""
    return {
        "number": number,
        "state": state,
        "merged_at": merged_at,
        "head": {"sha": head_sha},
        "merge_commit_sha": merge_commit_sha,
    }


def _stub_resolution(module: object, payload: object | None) -> _FetchRecorder:
    fetcher = _FetchRecorder(payload)
    module._github_get_json = fetcher  # type: ignore[attr-defined]
    return fetcher


class _PublishRecorder:
    """Records the broker the publisher would actually publish to (no I/O)."""

    def __init__(self) -> None:
        self.brokers: list[str] = []

    def __call__(
        self,
        *,
        bootstrap_servers: str,
        username: str,
        password: str,
        repo: str,
        pr_number: int,
    ) -> str:
        self.brokers.append(bootstrap_servers)
        return f"cid-{pr_number}"


@pytest.mark.unit
class TestCrossBoundarySeam:
    """The mandated seam test: real publisher dry-run output -> real model."""

    def test_dry_run_output_validates_as_effect_request(self) -> None:
        module = _load_publisher()
        runner = CliRunner()
        result = runner.invoke(
            module.main,  # type: ignore[attr-defined]
            ["--dry-run"],
            env=_required_pr_env(),
        )
        assert result.exit_code == 0, result.output

        # The emitted JSON payload is the last block of the dry-run output —
        # parse the ACTUAL wire bytes, not a re-built dict.
        payload = json.loads(result.output[result.output.index("{") :])
        command = ModelOccCompanionEffectRequest.model_validate(payload)

        # The optional-input-silent-skip trap: mode MUST be explicit "mutate";
        # a model-default dry_run command would read+compute and never mint.
        assert command.mode == "mutate"
        # GHA env is a string; the wire value must already be an int.
        assert isinstance(payload["pr_number"], int)
        assert command.pr_number == 42
        assert command.repo == "OmniNode-ai/omnimarket"
        # correlation_id is a str uuid4 on the wire; the model coerces to UUID.
        assert isinstance(command.correlation_id, UUID)
        # occ_repo/runner/verifier are omitted -> model defaults apply, and
        # runner != verifier (OMN-12791).
        assert command.runner == "node_occ_companion_compute"
        assert command.verifier == "occ-evidence-source-autobind"
        assert command.runner != command.verifier

    def test_injected_legacy_autobind_fields_are_rejected(self) -> None:
        """extra='forbid' seam: each legacy occ-autobind field poisons the
        payload — the exact wrong-shape-silently-DLQd class (OMN-13990)."""
        module = _load_publisher()
        runner = CliRunner()
        result = runner.invoke(
            module.main,  # type: ignore[attr-defined]
            ["--dry-run"],
            env=_required_pr_env(),
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output[result.output.index("{") :])

        for legacy in _LEGACY_AUTOBIND_FIELDS:
            with pytest.raises(ValidationError):
                ModelOccCompanionEffectRequest.model_validate(
                    {**payload, legacy: "poison"}
                )

    def test_payload_has_exactly_the_command_fields(self) -> None:
        module = _load_publisher()
        payload = module.build_payload(  # type: ignore[attr-defined]
            "OmniNode-ai/omnimarket", 7, "00000000-0000-4000-8000-000000000000"
        )
        assert set(payload) == {"repo", "pr_number", "mode", "correlation_id"}
        assert payload["mode"] == "mutate"

    def test_topic_matches_the_contract_declared_command_topic(self) -> None:
        """The publisher's topic constant must be the exact topic the node's
        contract subscribes to — a mismatch is a publish into the void."""
        module = _load_publisher()
        contract = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
        topic = module.TOPIC  # type: ignore[attr-defined]
        assert topic == "onex.cmd.omnimarket.occ-companion-effect-requested.v1"
        assert topic == contract["runtime_dispatch"]["command_topic"]
        assert topic in contract["event_bus"]["subscribe_topics"]

    def test_non_integer_pr_number_fails_fast(self) -> None:
        module = _load_publisher()
        runner = CliRunner()
        result = runner.invoke(
            module.main,  # type: ignore[attr-defined]
            ["--dry-run"],
            env=_required_pr_env(PR_NUMBER="not-a-number"),
        )
        assert result.exit_code == 1, result.output
        assert "PR_NUMBER must be an integer" in result.output


@pytest.mark.unit
class TestPublisherSideIdempotency:
    """OMN-14941: an already-bound product PR skips the publish loudly."""

    def test_already_bound_body_skips_and_never_publishes(self) -> None:
        module = _load_publisher()
        _stub_resolution(module, [_companion_payload(number=4242)])
        recorder = _PublishRecorder()
        module.publish_occ_companion_effect_command = recorder  # type: ignore[attr-defined]
        runner = CliRunner()
        env = _required_pr_env(
            PR_BODY="Closes OMN-14941\n\nEvidence-Source: OCC#4242",
            RUNNER_IS_TRUSTED="true",
        )
        result = runner.invoke(module.main, ["--lane", "dev"], env=env)  # type: ignore[attr-defined]
        assert result.exit_code == 0, result.output
        assert "SKIP" in result.output
        assert "already" in result.output
        assert recorder.brokers == []

    def test_already_bound_body_skips_even_in_dry_run(self) -> None:
        """The skip is a semantic gate, not a transport branch — dry-run output
        must not advertise a payload that the live path would refuse to send."""
        module = _load_publisher()
        _stub_resolution(module, [_companion_payload(number=9)])
        runner = CliRunner()
        env = _required_pr_env(PR_BODY="Evidence-Source: OCC#9")
        result = runner.invoke(module.main, ["--dry-run"], env=env)  # type: ignore[attr-defined]
        assert result.exit_code == 0, result.output
        assert "SKIP" in result.output
        assert "{" not in result.output  # no payload emitted


# Bodies whose ONLY occurrence of the literal is prose. Every one of these
# used to trip the substring skip, so the publisher reported SUCCESS having
# published nothing and the product PR then hard-failed `verify / verify` for
# a stamp that was never minted (OMN-16710, observed live on omnimemory#450
# during the OMN-16708 canary).
_PROSE_ONLY_BODIES: tuple[tuple[str, str], ...] = (
    (
        "mid_line_backticked",
        "Closes OMN-16710\n\nDocuments how the receipt gate hard-fails when the "
        "body has no `Evidence-Source: OCC#<n>` line.\n",
    ),
    (
        "mid_line_prose",
        "This PR explains what Evidence-Source: OCC#7259 means for reviewers.\n",
    ),
    (
        "blockquoted_log_line",
        "Observed output:\n\n> Evidence-Source: OCC#7259\n\nCloses OMN-16710\n",
    ),
    (
        "list_item",
        "- the publisher stamps Evidence-Source: OCC#<n> back onto the product PR\n",
    ),
    (
        "no_digits_placeholder_at_line_start",
        "Evidence-Source: OCC#<n>\n",
    ),
    (
        "trailing_prose_on_the_stamp_line",
        "Evidence-Source: OCC#7259 is what the gate looks for.\n",
    ),
)


@pytest.mark.unit
class TestIdempotencyIsLineAnchored:
    """OMN-16710: the skip must key on a REAL stamp line, not a substring hit.

    The pre-fix gate was ``"Evidence-Source: OCC#" in pr_body``, which matched
    the literal anywhere — including prose that merely discusses OCC evidence.
    Tripping it is silent-green on a merge-gating path: nothing is published,
    no companion is minted, no stamp lands, and the job still exits 0. Same
    shape OMN-14639 hardened the *other* branch of this publisher against.
    """

    @pytest.mark.parametrize(("label", "body"), _PROSE_ONLY_BODIES, ids=lambda v: v)
    def test_prose_only_mention_still_publishes(self, label: str, body: str) -> None:
        module = _load_publisher()
        recorder = _PublishRecorder()
        module.publish_occ_companion_effect_command = recorder  # type: ignore[attr-defined]
        runner = CliRunner()
        env = _required_pr_env(PR_BODY=body, RUNNER_IS_TRUSTED="true")
        result = runner.invoke(module.main, ["--lane", "dev"], env=env)  # type: ignore[attr-defined]
        assert result.exit_code == 0, result.output
        assert "SKIP" not in result.output, (
            f"{label}: prose-only mention suppressed the publish — this is the "
            "OMN-16710 silent-green defect"
        )
        assert recorder.brokers != [], f"{label}: nothing was published"

    @pytest.mark.parametrize(
        "body",
        [
            "Evidence-Source: OCC#4242",
            "Closes OMN-14941\n\nEvidence-Source: OCC#4242\n",
            "  Evidence-Source: OCC#4242  ",  # canonical parser strips the line
            "evidence-source: occ#4242",  # canonical parser is IGNORECASE
            "Evidence-Source:   OCC#4242\r\n",  # CRLF + padded separator
        ],
        ids=[
            "bare",
            "with_surrounding_body",
            "indented",
            "lowercased",
            "crlf_padded",
        ],
    )
    def test_real_stamp_line_still_skips(self, body: str) -> None:
        """AC2 — the OMN-14941 behavior the gate exists for is preserved.

        OMN-15615: the stamp must now also RESOLVE, so the live companion set
        for this product PR is stubbed to contain the cited companion.
        """
        module = _load_publisher()
        _stub_resolution(module, [_companion_payload(number=4242)])
        recorder = _PublishRecorder()
        module.publish_occ_companion_effect_command = recorder  # type: ignore[attr-defined]
        runner = CliRunner()
        env = _required_pr_env(PR_BODY=body, RUNNER_IS_TRUSTED="true")
        result = runner.invoke(module.main, ["--lane", "dev"], env=env)  # type: ignore[attr-defined]
        assert result.exit_code == 0, result.output
        assert "SKIP" in result.output
        assert recorder.brokers == []

    def test_bare_sha_evidence_source_is_not_a_binding(self) -> None:
        """A commit-SHA Evidence-Source that resolves to NO merged companion is
        exactly the unbound state the companion effect exists to repair — it
        must NOT suppress the publish. (When it DOES resolve, it is a binding —
        see TestModeBShaFormBinding.)"""
        module = _load_publisher()
        _stub_resolution(module, [])
        recorder = _PublishRecorder()
        module.publish_occ_companion_effect_command = recorder  # type: ignore[attr-defined]
        runner = CliRunner()
        env = _required_pr_env(
            PR_BODY=f"Evidence-Source: {'b' * 40}\n", RUNNER_IS_TRUSTED="true"
        )
        result = runner.invoke(module.main, ["--lane", "dev"], env=env)  # type: ignore[attr-defined]
        assert result.exit_code == 0, result.output
        assert "SKIP" not in result.output
        assert recorder.brokers != []


@pytest.mark.unit
class TestIdempotencyAgreesWithCanonicalParser:
    """AC3 — pin the thin publisher against the authoritative compat parser.

    The publisher is a thin GHA-runner script and deliberately does not import
    the node package, so it carries its own copy of the binding predicate. A
    copy that drifts from ``omnibase_compat.contracts.pr_occ_stamp`` is the
    same class of defect as the substring match itself: the publisher would
    decide "already bound" on different evidence than the handler that does
    the minting. This test is the only thing keeping the two in agreement.
    """

    @pytest.mark.parametrize(
        "body",
        [
            "",
            "Closes OMN-16710",
            "Evidence-Source: OCC#4242",
            "Closes OMN-14941\n\nEvidence-Source: OCC#4242\n",
            "  Evidence-Source: OCC#4242  ",
            "evidence-source: occ#4242",
            "Evidence-Source:   OCC#4242\r\n",
            f"Evidence-Source: {'b' * 40}\n",
            "> Evidence-Source: OCC#7259\n",
            "- stamps Evidence-Source: OCC#<n> onto the product PR\n",
            "Evidence-Source: OCC#<n>\n",
            "Evidence-Source: OCC#7259 is what the gate looks for.\n",
            "See `Evidence-Source: OCC#7259` above.\n",
            "Evidence-Source: not-a-source\nEvidence-Source: OCC#77\n",
        ],
    )
    def test_publisher_binding_matches_compat_parser(self, body: str) -> None:
        from omnibase_compat.contracts.pr_occ_stamp import (
            EnumPrEvidenceSourceKind,
            parse_pr_occ_metadata_stamp,
        )

        module = _load_publisher()
        source = parse_pr_occ_metadata_stamp(body).evidence_source
        expected = (
            source.occ_pr_number
            if source is not None and source.kind is EnumPrEvidenceSourceKind.OCC_PR
            else None
        )
        assert module.product_pr_occ_binding(body) == expected, body  # type: ignore[attr-defined]

    def test_unbound_body_publishes_secret_free_on_shipped_dev_lane(self) -> None:
        """E2 acceptance shape: against the REAL shipped config/ci_bus_lanes.yaml,
        a trusted runner with NO KAFKA_BOOTSTRAP_SERVERS injected and --lane dev
        resolves and publishes to the committed concrete broker (OMN-14813 —
        the born path needs no secret)."""
        module = _load_publisher()
        recorder = _PublishRecorder()
        module.publish_occ_companion_effect_command = recorder  # type: ignore[attr-defined]
        runner = CliRunner()
        env = _required_pr_env(RUNNER_IS_TRUSTED="true")
        env.pop("KAFKA_BOOTSTRAP_SERVERS", None)
        result = runner.invoke(module.main, ["--lane", "dev"], env=env)  # type: ignore[attr-defined]
        assert result.exit_code == 0, result.output
        assert (
            recorder.brokers
            == [
                "omninode-pc.tail75df5e.ts.net:19092"  # onex-allow-test-fixture OMN-16156 reason="asserts the real committed dev-lane broker resolves correctly from config"
            ]
        )

    def test_missing_runner_is_trusted_flag_fails_fast(self) -> None:
        """The RUNNER_IS_TRUSTED wiring-gap fail-fast carries over (OMN-14451)."""
        module = _load_publisher()
        runner = CliRunner()
        env = _required_pr_env()
        env.pop("RUNNER_IS_TRUSTED", None)
        result = runner.invoke(module.main, ["--lane", "dev"], env=env)  # type: ignore[attr-defined]
        assert result.exit_code == 1, result.output
        assert "RUNNER_IS_TRUSTED" in result.output


class _FakeProducer:
    """Minimal confluent_kafka.Producer stand-in (mirrors the autobind tests):
    ``flush()`` returns the count of messages still queued when the window
    elapses; the delivery callback deliberately never fires."""

    instances: list[_FakeProducer] = []

    def __init__(self, remaining: int, config: dict[str, object] | None = None) -> None:
        self._remaining = remaining
        self.produced: list[dict[str, object]] = []
        _FakeProducer.instances.append(self)

    def produce(self, **kwargs: object) -> None:
        self.produced.append(kwargs)

    def flush(self, timeout: float | None = None) -> int:
        return self._remaining


def _install_fake_confluent_kafka(
    monkeypatch: pytest.MonkeyPatch, remaining: int
) -> None:
    _FakeProducer.instances = []
    fake_mod = types.ModuleType("confluent_kafka")
    fake_mod.Producer = lambda config=None: _FakeProducer(remaining, config)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "confluent_kafka", fake_mod)


@pytest.mark.unit
class TestKafkaWireShape:
    """Key/topic/value shape + the OMN-14639 fail-loud flush."""

    def test_produce_uses_companion_effect_key_and_canonical_topic(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        module = _load_publisher()
        _install_fake_confluent_kafka(monkeypatch, remaining=0)

        correlation_id = module.publish_occ_companion_effect_command(  # type: ignore[attr-defined]
            bootstrap_servers="10.0.0.9:19092",
            username="",
            password="",
            repo="OmniNode-ai/omnimarket",
            pr_number=42,
        )
        (producer,) = _FakeProducer.instances
        (produced,) = producer.produced
        assert produced["topic"] == module.TOPIC  # type: ignore[attr-defined]
        assert produced["key"] == b"occ-companion-effect/OmniNode-ai/omnimarket/42"
        wire = json.loads(bytes(produced["value"]).decode("utf-8"))  # type: ignore[arg-type]
        command = ModelOccCompanionEffectRequest.model_validate(wire)
        assert command.mode == "mutate"
        assert str(command.correlation_id) == correlation_id

    def test_undelivered_message_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """OMN-14639: flush leaves 1 message queued => must raise, not return."""
        module = _load_publisher()
        _install_fake_confluent_kafka(monkeypatch, remaining=1)

        with pytest.raises(RuntimeError, match="undelivered"):
            module.publish_occ_companion_effect_command(  # type: ignore[attr-defined]
                bootstrap_servers="10.0.0.9:19092",
                username="",
                password="",
                repo="OmniNode-ai/omnimarket",
                pr_number=42,
            )


# --------------------------------------------------------------------------
# OMN-15615 — the idempotency guard must RESOLVE the companion it names.
#
# Two opposite failure modes came out of one predicate, and both fired on
# omniclaude#1969 within 70 minutes:
#
#   Mode A  a body that merely CARRIES the token (in prose, then — after
#           OMN-16710 — still inside a fenced block) suppressed its own mint.
#           The workflow reported SUCCESS having published nothing: 34 min of
#           red on #1969, 54 min on omnibase_core#1540.
#   Mode B  a body bound by the SHA form was invisible to the OCC#-only
#           predicate, so the publisher re-published AFTER its own companion
#           merged, producing the duplicate OCC#5795 whose entire diff was
#           in-place rewrites of OCC#5793's merged receipts.
#
# The fix is the same in both directions: parse both legal forms out of the
# canonical (non-fenced, non-quoted) body, then RESOLVE the citation against
# the live OCC companion set for THIS product PR. Text is never the answer.
# --------------------------------------------------------------------------


# omniclaude#1969's body at the moment the vacuous SKIP fired (runs
# 30682490364 / 30683162844, both SUCCESS, neither carrying an
# `occ-companion-effect command:` publish line). RECONSTRUCTED, and labelled
# as such: GitHub exposes no PR-body revision history through the REST API, so
# the verbatim first revision is not retrievable. What IS verbatim is the
# incident's own description of it (recorded on OMN-15615 and quoted in the
# surviving #1969 body): the token appeared only inside a quoted example while
# the PR corrected #1968's prose. The fixture reproduces exactly that shape —
# the ONLY occurrence of a column-0 stamp line is inside a fence.
_FENCED_ONLY_BODY = """\
Closes OMN-15606.

Corrects #1968's prose. The emitter logged:

```
SKIP: OmniNode-ai/omniclaude#1969 body already carries 'Evidence-Source: OCC#'
Evidence-Source: OCC#5793
```

No companion exists for this PR yet.
"""

_TILDE_FENCED_ONLY_BODY = """\
Closes OMN-15606.

~~~text
Evidence-Source: OCC#5793
~~~
"""

_UNTERMINATED_FENCE_BODY = """\
Closes OMN-15606.

```
Evidence-Source: OCC#5793
"""


@pytest.mark.unit
class TestModeAFencedStampIsNotABinding:
    """AC1 — a stamp that exists only inside a fenced block MUST publish.

    RED against the pre-OMN-15615 script: `product_pr_occ_binding` parsed the
    raw body, so the fenced column-0 line returned 5793 and the publisher
    exited 0 having published nothing — a green check attesting only that a
    script ran. This drives the REAL artifact (the shipped CLI), not a
    re-implementation.
    """

    @pytest.mark.parametrize(
        ("label", "body"),
        [
            ("backtick_fence", _FENCED_ONLY_BODY),
            ("tilde_fence", _TILDE_FENCED_ONLY_BODY),
            ("unterminated_fence", _UNTERMINATED_FENCE_BODY),
        ],
        ids=lambda v: v if isinstance(v, str) and len(v) < 30 else "body",
    )
    def test_fenced_only_stamp_still_publishes(self, label: str, body: str) -> None:
        module = _load_publisher()
        recorder = _PublishRecorder()
        module.publish_occ_companion_effect_command = recorder  # type: ignore[attr-defined]
        runner = CliRunner()
        env = _required_pr_env(PR_BODY=body, RUNNER_IS_TRUSTED="true")
        result = runner.invoke(module.main, ["--lane", "dev"], env=env)  # type: ignore[attr-defined]
        assert result.exit_code == 0, result.output
        assert "SKIP" not in result.output, (
            f"{label}: a stamp quoted inside a fence suppressed the mint — this "
            "is Mode A of OMN-15615 (vacuous SUCCESS, no companion)"
        )
        assert recorder.brokers != [], f"{label}: nothing was published"

    def test_fenced_only_stamp_does_not_even_reach_resolution(self) -> None:
        """The fence is stripped BEFORE resolution, so no citation exists to
        resolve. The poisoned fetcher proves no read was attempted."""
        module = _load_publisher()
        assert module.product_pr_evidence_citation(_FENCED_ONLY_BODY) is None  # type: ignore[attr-defined]


@pytest.mark.unit
class TestStripAgreesWithCanonicalHelper:
    """AC5 — reuse the canonical notion of a non-canonical region.

    The thin GHA script cannot import omnibase_core (it runs on a sparse
    checkout with click/pyyaml/confluent-kafka and nothing else), so it carries
    a mirror. This test is what makes the mirror a REUSE rather than a fourth
    private parser: it pins the copy byte-for-byte against
    ``validator_receipt_gate.strip_noncanonical_regions`` (OMN-14682), the
    helper whose own docstring names this exact trap.
    """

    @pytest.mark.parametrize(
        "body",
        [
            "",
            "Closes OMN-15615",
            _FENCED_ONLY_BODY,
            _TILDE_FENCED_ONLY_BODY,
            _UNTERMINATED_FENCE_BODY,
            "> Evidence-Source: OCC#5793\n",
            "  > quoted with leading space\n",
            "```\nEvidence-Source: OCC#1\n```\nEvidence-Source: OCC#2\n",
            "~~~\n```\nstill inside the tilde fence\n~~~\nout\n",
            "````\nfour backticks\n````\n",
            "Evidence-Source: OCC#7\n",
            "text\r\nmore text\r\n",
        ],
    )
    def test_publisher_strip_matches_core_helper(self, body: str) -> None:
        from omnibase_core.validation.validator_receipt_gate import (
            strip_noncanonical_regions,
        )

        module = _load_publisher()
        assert module.strip_noncanonical_regions(body) == strip_noncanonical_regions(  # type: ignore[attr-defined]
            body
        ), body

    def test_strip_is_idempotent(self) -> None:
        module = _load_publisher()
        once = module.strip_noncanonical_regions(_FENCED_ONLY_BODY)  # type: ignore[attr-defined]
        assert module.strip_noncanonical_regions(once) == once  # type: ignore[attr-defined]


@pytest.mark.unit
class TestSkipRequiresArtifactResolution:
    """AC2 — the skip is keyed on a companion that RESOLVES, never on text."""

    def test_citation_naming_another_prs_companion_publishes(self) -> None:
        """A body citing OCC#7259 while THIS PR's companion set is empty is a
        citation that resolves to nothing. It must publish."""
        module = _load_publisher()
        fetcher = _stub_resolution(module, [])
        recorder = _PublishRecorder()
        module.publish_occ_companion_effect_command = recorder  # type: ignore[attr-defined]
        runner = CliRunner()
        env = _required_pr_env(
            PR_BODY="Evidence-Source: OCC#7259\n", RUNNER_IS_TRUSTED="true"
        )
        result = runner.invoke(module.main, ["--lane", "dev"], env=env)  # type: ignore[attr-defined]
        assert result.exit_code == 0, result.output
        assert "SKIP" not in result.output
        assert "publish_reason: no_companion_exists_for_this_pr" in result.output
        assert recorder.brokers != []
        assert fetcher.urls, "resolution was never attempted"

    def test_citation_naming_a_companion_of_a_different_pr_publishes(self) -> None:
        """This PR HAS a companion (OCC#5810), but the body cites OCC#7259.
        The cited number is not in this PR's companion set -> publish."""
        module = _load_publisher()
        _stub_resolution(module, [_companion_payload(number=5810)])
        recorder = _PublishRecorder()
        module.publish_occ_companion_effect_command = recorder  # type: ignore[attr-defined]
        runner = CliRunner()
        env = _required_pr_env(
            PR_BODY="Evidence-Source: OCC#7259\n", RUNNER_IS_TRUSTED="true"
        )
        result = runner.invoke(module.main, ["--lane", "dev"], env=env)  # type: ignore[attr-defined]
        assert result.exit_code == 0, result.output
        assert "publish_reason: citation_is_not_a_companion_of_this_pr" in result.output
        assert recorder.brokers != []

    def test_closed_unmerged_companion_publishes(self) -> None:
        """The OMN-15214 incident state: a companion CLOSED without merging is
        destroyed evidence, not a binding. Re-mint."""
        module = _load_publisher()
        _stub_resolution(
            module,
            [_companion_payload(number=5793, state="closed", merged_at=None)],
        )
        recorder = _PublishRecorder()
        module.publish_occ_companion_effect_command = recorder  # type: ignore[attr-defined]
        runner = CliRunner()
        env = _required_pr_env(
            PR_BODY="Evidence-Source: OCC#5793\n", RUNNER_IS_TRUSTED="true"
        )
        result = runner.invoke(module.main, ["--lane", "dev"], env=env)  # type: ignore[attr-defined]
        assert result.exit_code == 0, result.output
        assert "publish_reason: cited_companion_closed_unmerged" in result.output
        assert recorder.brokers != []

    def test_resolution_queries_this_prs_deterministic_companion_branch(self) -> None:
        """The 'for THIS product PR' half of AC2 is carried by the branch
        filter — mirrors handler_occ_companion_compute's branch construction."""
        module = _load_publisher()
        fetcher = _stub_resolution(module, [])
        recorder = _PublishRecorder()
        module.publish_occ_companion_effect_command = recorder  # type: ignore[attr-defined]
        runner = CliRunner()
        env = _required_pr_env(
            PR_REPO="OmniNode-ai/omniclaude",
            PR_NUMBER="1969",
            PR_BODY="Evidence-Source: OCC#5793\n",
            RUNNER_IS_TRUSTED="true",
        )
        runner.invoke(module.main, ["--lane", "dev"], env=env)  # type: ignore[attr-defined]
        assert len(fetcher.urls) == 1
        url = fetcher.urls[0]
        assert "OmniNode-ai/onex_change_control/pulls" in url
        # Live-verified: OCC#5793's headRefName for omniclaude#1969.
        assert (
            "head=OmniNode-ai:auto/omninode-ai-omniclaude-pr-1969-occ-autobind" in url
        )
        assert "state=all" in url

    def test_companion_branch_mirrors_the_compute_handler(self) -> None:
        module = _load_publisher()
        assert (
            module.companion_branch("OmniNode-ai/omniclaude", 1969)  # type: ignore[attr-defined]
            == "auto/omninode-ai-omniclaude-pr-1969-occ-autobind"
        )


@pytest.mark.unit
class TestResolutionFailsClosedByPublishing:
    """AC3 — never skip on an answer the publisher could not obtain.

    A redundant command is a cheap already-bound no-op at the handler; a missed
    mint cost 34 minutes of red on omniclaude#1969 and 54 on
    omnibase_core#1540. The asymmetry is the whole design.
    """

    def test_api_failure_publishes(self) -> None:
        """The explicit API-failure path AC3 names: the fetcher returns None
        for every transport error, non-200, and rate limit."""
        module = _load_publisher()
        _stub_resolution(module, None)
        recorder = _PublishRecorder()
        module.publish_occ_companion_effect_command = recorder  # type: ignore[attr-defined]
        runner = CliRunner()
        env = _required_pr_env(
            PR_BODY="Evidence-Source: OCC#5793\n", RUNNER_IS_TRUSTED="true"
        )
        result = runner.invoke(module.main, ["--lane", "dev"], env=env)  # type: ignore[attr-defined]
        assert result.exit_code == 0, result.output
        assert "SKIP" not in result.output
        assert "publish_reason: resolution_unavailable" in result.output
        assert recorder.brokers != []

    @pytest.mark.parametrize(
        "payload",
        [
            {"message": "Not Found"},
            ["not-a-dict"],
            [{"number": "5793", "state": "open"}],
            [{"state": "open"}],
            "",
        ],
        ids=["object", "list_of_str", "number_not_int", "no_number", "empty_string"],
    )
    def test_malformed_payload_publishes(self, payload: object) -> None:
        module = _load_publisher()
        _stub_resolution(module, payload)
        recorder = _PublishRecorder()
        module.publish_occ_companion_effect_command = recorder  # type: ignore[attr-defined]
        runner = CliRunner()
        env = _required_pr_env(
            PR_BODY="Evidence-Source: OCC#5793\n", RUNNER_IS_TRUSTED="true"
        )
        result = runner.invoke(module.main, ["--lane", "dev"], env=env)  # type: ignore[attr-defined]
        assert result.exit_code == 0, result.output
        assert "SKIP" not in result.output
        assert recorder.brokers != []

    def test_empty_pr_body_publishes_without_resolving(self) -> None:
        module = _load_publisher()  # fetcher stays poisoned
        recorder = _PublishRecorder()
        module.publish_occ_companion_effect_command = recorder  # type: ignore[attr-defined]
        runner = CliRunner()
        env = _required_pr_env(PR_BODY="", RUNNER_IS_TRUSTED="true")
        result = runner.invoke(module.main, ["--lane", "dev"], env=env)  # type: ignore[attr-defined]
        assert result.exit_code == 0, result.output
        assert "publish_reason: no_evidence_source_citation" in result.output
        assert recorder.brokers != []

    def test_malformed_stamp_value_publishes_without_resolving(self) -> None:
        """An ambiguous/malformed stamp is indeterminate, not a binding."""
        module = _load_publisher()  # fetcher stays poisoned
        recorder = _PublishRecorder()
        module.publish_occ_companion_effect_command = recorder  # type: ignore[attr-defined]
        runner = CliRunner()
        env = _required_pr_env(
            PR_BODY="Evidence-Source: OCC#<n>\n", RUNNER_IS_TRUSTED="true"
        )
        result = runner.invoke(module.main, ["--lane", "dev"], env=env)  # type: ignore[attr-defined]
        assert result.exit_code == 0, result.output
        assert "publish_reason: no_evidence_source_citation" in result.output
        assert recorder.brokers != []

    def test_direct_resolver_returns_none_on_transport_error(self) -> None:
        """The real ``_github_get_json`` swallows every failure into None."""
        import http.client
        import urllib.error
        import urllib.request

        module = _load_publisher()

        def _url_error(*_args: object, **_kwargs: object) -> object:
            raise urllib.error.URLError("connection refused")

        def _incomplete_read(*_args: object, **_kwargs: object) -> object:
            raise http.client.IncompleteRead(b"{")

        original = urllib.request.urlopen
        try:
            for boom in (_url_error, _incomplete_read):
                urllib.request.urlopen = boom  # type: ignore[assignment]
                assert (
                    module._live_github_get_json("https://api.github.com/x", "")  # type: ignore[attr-defined]
                    is None
                )
        finally:
            urllib.request.urlopen = original  # type: ignore[assignment]


# Live values, read back 2026-08-29 from the incident artifacts:
#   gh pr view 5793 --repo OmniNode-ai/onex_change_control
#     -> headRefName auto/omninode-ai-omniclaude-pr-1969-occ-autobind
#        mergeCommit.oid 64174b874aba54bf5c171fb4a087717cc1575004
#        mergedAt 2026-08-01T04:49:37Z
#   gh pr view 1969 --repo OmniNode-ai/omniclaude --json body
#     -> "Evidence-Source: 64174b874aba54bf5c171fb4a087717cc1575004"
_OCC_5793_MERGE_SHA = "64174b874aba54bf5c171fb4a087717cc1575004"
_OCC_5793_HEAD_SHA = "d4750c8e38ac5121fd770a0edadb65efd18b24a7"


@pytest.mark.unit
class TestModeBShaFormBinding:
    """AC4 — a SHA-form stamp that resolves to a merged companion is a BINDING.

    This is the assertion that stops the duplicate-companion path. On
    2026-08-01 the OCC#-only predicate could not see #1969's SHA stamp, so the
    publisher re-published at 04:51:35Z — two minutes after OCC#5793 merged —
    and minted OCC#5795 on the same head branch, whose entire diff was in-place
    rewrites of #5793's merged receipts (four `receipt_file_mutated`
    violations). Falsifier: narrow the citation parser back to the OCC# form
    only and this test goes red.
    """

    def test_sha_stamp_of_a_merged_companion_skips(self) -> None:
        module = _load_publisher()
        _stub_resolution(
            module,
            [
                _companion_payload(
                    number=5793,
                    state="closed",
                    merged_at="2026-08-01T04:49:37Z",
                    head_sha=_OCC_5793_HEAD_SHA,
                    merge_commit_sha=_OCC_5793_MERGE_SHA,
                )
            ],
        )
        recorder = _PublishRecorder()
        module.publish_occ_companion_effect_command = recorder  # type: ignore[attr-defined]
        runner = CliRunner()
        env = _required_pr_env(
            PR_REPO="OmniNode-ai/omniclaude",
            PR_NUMBER="1969",
            PR_BODY=f"Evidence-Source: {_OCC_5793_MERGE_SHA}\n",
            RUNNER_IS_TRUSTED="true",
        )
        result = runner.invoke(module.main, ["--lane", "dev"], env=env)  # type: ignore[attr-defined]
        assert result.exit_code == 0, result.output
        assert "skipped_bound_to: OCC#5793" in result.output
        assert recorder.brokers == [], "the duplicate OCC#5795 path is still open"

    def test_head_sha_form_also_binds(self) -> None:
        module = _load_publisher()
        _stub_resolution(
            module,
            [
                _companion_payload(
                    number=5793,
                    state="closed",
                    merged_at="2026-08-01T04:49:37Z",
                    head_sha=_OCC_5793_HEAD_SHA,
                    merge_commit_sha=_OCC_5793_MERGE_SHA,
                )
            ],
        )
        recorder = _PublishRecorder()
        module.publish_occ_companion_effect_command = recorder  # type: ignore[attr-defined]
        runner = CliRunner()
        env = _required_pr_env(
            PR_BODY=f"Evidence-Source: {_OCC_5793_HEAD_SHA[:12]}\n",
            RUNNER_IS_TRUSTED="true",
        )
        result = runner.invoke(module.main, ["--lane", "dev"], env=env)  # type: ignore[attr-defined]
        assert result.exit_code == 0, result.output
        assert "skipped_bound_to: OCC#5793" in result.output
        assert recorder.brokers == []

    def test_sha_of_an_unmerged_companion_publishes(self) -> None:
        """An OPEN companion's head SHA is not durable evidence — the mint is
        still owed, so the command still goes out."""
        module = _load_publisher()
        _stub_resolution(
            module,
            [
                _companion_payload(
                    number=5793, state="open", head_sha=_OCC_5793_HEAD_SHA
                )
            ],
        )
        recorder = _PublishRecorder()
        module.publish_occ_companion_effect_command = recorder  # type: ignore[attr-defined]
        runner = CliRunner()
        env = _required_pr_env(
            PR_BODY=f"Evidence-Source: {_OCC_5793_HEAD_SHA}\n",
            RUNNER_IS_TRUSTED="true",
        )
        result = runner.invoke(module.main, ["--lane", "dev"], env=env)  # type: ignore[attr-defined]
        assert result.exit_code == 0, result.output
        assert (
            "publish_reason: sha_is_not_a_merged_companion_of_this_pr" in result.output
        )
        assert recorder.brokers != []

    def test_citation_parser_recognises_both_legal_forms(self) -> None:
        module = _load_publisher()
        assert module.product_pr_evidence_citation("Evidence-Source: OCC#5793\n") == (  # type: ignore[attr-defined]
            "occ_pr",
            "5793",
        )
        assert module.product_pr_evidence_citation(  # type: ignore[attr-defined]
            f"Evidence-Source: {_OCC_5793_MERGE_SHA}\n"
        ) == ("sha", _OCC_5793_MERGE_SHA)


@pytest.mark.unit
class TestVerdictMarkerIsAlwaysEmitted:
    """AC7 — SUCCESS stops being vacuous.

    Every exit-0 path prints exactly one machine-readable verdict marker, and a
    skip marker NAMES the resolved companion. The caller workflow greps for a
    marker and fails the job when none is present, so "green publisher, nothing
    happened" is no longer representable.
    """

    _MARKERS = ("skipped_bound_to:", "published_correlation_id:", "publish_declined:")

    def _markers_in(self, output: str) -> list[str]:
        return [m for m in self._MARKERS if m in output]

    def test_skip_names_the_resolved_companion(self) -> None:
        module = _load_publisher()
        _stub_resolution(module, [_companion_payload(number=4242)])
        recorder = _PublishRecorder()
        module.publish_occ_companion_effect_command = recorder  # type: ignore[attr-defined]
        runner = CliRunner()
        env = _required_pr_env(
            PR_BODY="Evidence-Source: OCC#4242\n", RUNNER_IS_TRUSTED="true"
        )
        result = runner.invoke(module.main, ["--lane", "dev"], env=env)  # type: ignore[attr-defined]
        assert self._markers_in(result.output) == ["skipped_bound_to:"]
        assert "skipped_bound_to: OCC#4242" in result.output

    def test_publish_emits_the_correlation_id(self) -> None:
        module = _load_publisher()
        recorder = _PublishRecorder()
        module.publish_occ_companion_effect_command = recorder  # type: ignore[attr-defined]
        runner = CliRunner()
        env = _required_pr_env(RUNNER_IS_TRUSTED="true")
        result = runner.invoke(module.main, ["--lane", "dev"], env=env)  # type: ignore[attr-defined]
        assert result.exit_code == 0, result.output
        assert "published_correlation_id: cid-42" in result.output
        assert self._markers_in(result.output) == ["published_correlation_id:"]

    @pytest.mark.parametrize(
        ("argv", "env_overrides"),
        [
            (["--dry-run"], {}),
            (["--lane", "dev"], {"RUNNER_IS_TRUSTED": "false"}),
            ([], {"RUNNER_IS_TRUSTED": "false"}),
            (["--lane", "nope"], {"RUNNER_IS_TRUSTED": "false"}),
        ],
        ids=["dry_run", "untrusted_dev", "untrusted_no_lane", "untrusted_unknown_lane"],
    )
    def test_every_green_exit_carries_a_marker(
        self, argv: list[str], env_overrides: dict[str, str]
    ) -> None:
        module = _load_publisher()
        recorder = _PublishRecorder()
        module.publish_occ_companion_effect_command = recorder  # type: ignore[attr-defined]
        runner = CliRunner()
        env = _required_pr_env(**env_overrides)
        env.pop("KAFKA_BOOTSTRAP_SERVERS", None)
        result = runner.invoke(module.main, argv, env=env)  # type: ignore[attr-defined]
        assert result.exit_code == 0, result.output
        assert len(self._markers_in(result.output)) == 1, result.output
