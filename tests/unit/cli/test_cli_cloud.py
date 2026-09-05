# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``onex cloud`` command surface (OMN-16967).

The client is injected through ``ctx.obj["transport_factory"]`` — the command's
REAL wiring runs (option parsing, credential resolution, file writing, exit
codes); only the HTTP client is substituted. The client's own transport
behaviour is covered separately against ``httpx.MockTransport`` in
``tests/unit/cloud/test_transport_cloud_delegation.py``; duplicating it here would
prove nothing about the command.

The saved files are asserted as first-class output, not as a side effect: the
operator's stated reason for preferring a terminal client over a browser demo is
that a browser cannot keep what it generates.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from omnibase_core.enums.enum_core_error_code import EnumCoreErrorCode
from omnibase_core.errors.model_onex_error import ModelOnexError

from omnimarket.cli.cli_cloud import cloud_group
from omnimarket.cloud.model_cloud_delegation import (
    ModelCloudDelegationAck,
    ModelCloudDelegationReceipt,
    ModelCloudDelegationStatus,
)

pytestmark = pytest.mark.unit

_WORKFLOW_ID = "88ceab3f-37b3-4125-bc67-4c46a19eee5b"
_BASE_URL = "https://dev.api.omninode.ai"
_KEY = "onxk_testkey"

# Options this command reads from the environment. ``CliRunner`` inherits the
# real ``os.environ``, so a developer with ``ONEX_API_BASE_URL`` exported —
# pointing at a local gateway, say — would otherwise silently satisfy the very
# "no configured origin" state several of these tests exist to prove is a
# refusal. Cleared for every test so the environment is an input the tests set,
# never one they inherit.
_CLI_ENV_VARS = ("ONEX_API_BASE_URL", "ONEX_API_KEY_FILE")


@pytest.fixture(autouse=True)
def _isolate_cli_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _CLI_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def _ack(*, correlation_id: uuid.UUID) -> ModelCloudDelegationAck:
    return ModelCloudDelegationAck.model_validate(
        {
            "workflow_id": _WORKFLOW_ID,
            "envelope_id": str(uuid.uuid4()),
            "correlation_id": str(correlation_id),
            "workflow_type": "delegation-inference",
            "status": "published",
        }
    )


def _status(status: str) -> ModelCloudDelegationStatus:
    """Identity is a placeholder here; the fake gateway stamps it on the way out."""
    return ModelCloudDelegationStatus.model_validate(
        {
            "workflow_id": _WORKFLOW_ID,
            "workflow_type": "delegation-inference",
            "status": status,
            "envelope_id": str(uuid.uuid4()),
            "correlation_id": str(uuid.uuid4()),
            "command_topic": "onex.cmd.delegation.inference.v1",
            "submitted_at": "2026-08-29T15:00:00Z",
            "updated_at": "2026-08-29T15:00:08Z",
        }
    )


def _receipt(
    *,
    result_content: str | None = "A delegation receipt proves what ran.",
    status: str = "completed",
    terminal_model_used: str = "gemini-2.5-flash-lite",
) -> ModelCloudDelegationReceipt:
    """Receipt CONTENT. Its identity is a placeholder — see ``_FakeTransport``.

    ``workflow_id``/``correlation_id`` are stamped by the fake gateway on the
    way out, exactly as the real one does (``routers/workflows.py`` mints one
    correlation id per submission, writes it on the ``gateway_workflows`` row,
    and the receipt renderer copies it back off that row). A test that wants a
    receipt which does NOT bind to its submission asks for that explicitly.
    """
    return ModelCloudDelegationReceipt.model_validate(
        {
            "workflow_id": _WORKFLOW_ID,
            "tenant_id": str(uuid.uuid4()),
            "correlation_id": str(uuid.uuid4()),
            "workflow_type": "delegation-inference",
            "status": status,
            "submitted_at": "2026-08-29T15:00:00Z",
            "completed_at": "2026-08-29T15:00:08Z",
            "terminal_model_used": terminal_model_used,
            "terminal_total_tokens": 99,
            "terminal_latency_ms": 1083,
            "result_content": result_content,
            "event_count": 4,
            "projection_row_hash": "31266a6d",
            "terminal_event_hash": "9fe84da7",
            "verifier": "my-laptop",
        }
    )


class _FakeTransport:
    """Stands in for ``TransportCloudDelegation`` at the constructor seam.

    It behaves like a CORRECT gateway by default: the ack, the terminal status
    and the receipt all carry the one ``correlation_id`` this submission was
    given, because that is what the real gateway does. The four
    ``*_workflow_id`` / ``*_correlation_id`` knobs are how a test asks it to
    behave like a broken or confused one — answering with another run's
    envelope — which is the case the command must refuse rather than file.
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: Any,
        timeout_seconds: float = 30.0,
        submit_error: Exception | None = None,
        terminal_status: str = "completed",
        receipt: ModelCloudDelegationReceipt | None = None,
        receipt_workflow_id: uuid.UUID | None = None,
        receipt_correlation_id: uuid.UUID | None = None,
        status_workflow_id: uuid.UUID | None = None,
        status_correlation_id: uuid.UUID | None = None,
    ) -> None:
        self.base_url = base_url
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self._submit_error = submit_error
        self._terminal_status = terminal_status
        self._receipt = receipt if receipt is not None else _receipt()
        self._correlation_id = uuid.uuid4()
        self._receipt_workflow_id_override = receipt_workflow_id
        self._receipt_correlation_id = receipt_correlation_id
        self._status_workflow_id = status_workflow_id
        self._status_correlation_id = status_correlation_id
        self.submitted: dict[str, Any] | None = None

    def __enter__(self) -> _FakeTransport:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def submit(
        self, *, prompt: str, task_type: str, max_tokens: int | None
    ) -> ModelCloudDelegationAck:
        if self._submit_error is not None:
            raise self._submit_error
        self.submitted = {
            "prompt": prompt,
            "task_type": task_type,
            "max_tokens": max_tokens,
        }
        return _ack(correlation_id=self._correlation_id)

    def poll_until_terminal(
        self, workflow_id: str, *, attempts: int, interval_seconds: float
    ) -> ModelCloudDelegationStatus:
        return _status(self._terminal_status).model_copy(
            update={
                "workflow_id": self._status_workflow_id or uuid.UUID(workflow_id),
                "correlation_id": self._status_correlation_id or self._correlation_id,
            }
        )

    def receipt(
        self, workflow_id: str, *, runner_identity: str
    ) -> ModelCloudDelegationReceipt:
        self.runner_identity = runner_identity
        # Recorded because the workflow id is interpolated into the gateway URL
        # path by the real transport; the value that arrives here IS the path
        # segment, so normalisation has to be asserted on it, not inferred.
        self.receipt_workflow_id = workflow_id
        return self._receipt.model_copy(
            update={
                "workflow_id": self._receipt_workflow_id_override
                or uuid.UUID(workflow_id),
                "correlation_id": self._receipt_correlation_id or self._correlation_id,
            }
        )


def _factory(**overrides: Any) -> tuple[Any, list[_FakeTransport]]:
    made: list[_FakeTransport] = []

    def factory(**kwargs: Any) -> _FakeTransport:
        client = _FakeTransport(**{**kwargs, **overrides})
        made.append(client)
        return client

    return factory, made


def _logged_in(tmp_path: Path) -> Path:
    """Write a real credential store, so credential resolution is exercised."""
    home = tmp_path / "onex-home"
    from omnimarket.cloud.store_tenant_api_credential import StoreTenantApiCredential

    StoreTenantApiCredential(onex_home=home).save(
        base_url=_BASE_URL, api_key=_KEY, profile="default"
    )
    return home


# ---------------------------------------------------------------------------
# delegate — the customer's one command
# ---------------------------------------------------------------------------


def test_delegate_prints_the_result_and_saves_it_to_disk(tmp_path: Path) -> None:
    """AC1 + AC3 + AC4 in one path: output printed, files written, receipt saved."""
    home = _logged_in(tmp_path)
    factory, made = _factory()
    out = tmp_path / "runs"

    result = CliRunner().invoke(
        cloud_group,
        [
            "delegate",
            "Summarize what a delegation receipt proves.",
            "--task-type",
            "summarization",
            "--max-tokens",
            "512",
            "--output-dir",
            str(out),
            "--onex-home",
            str(home),
            "--poll-interval",
            "0",
        ],
        obj={"transport_factory": factory},
    )

    assert result.exit_code == 0, result.output
    assert "A delegation receipt proves what ran." in result.stdout

    run_dir = out / _WORKFLOW_ID
    assert (run_dir / "result.txt").read_text() == (
        "A delegation receipt proves what ran."
    )

    receipt_doc = json.loads((run_dir / "receipt.json").read_text())
    assert receipt_doc["terminal_model_used"] == "gemini-2.5-flash-lite"
    assert receipt_doc["projection_row_hash"] == "31266a6d"
    # ...and it is THIS run's receipt: the model name above is only the route
    # that answered if the receipt binds to the submission that was made.
    assert receipt_doc["workflow_id"] == _WORKFLOW_ID
    assert receipt_doc["correlation_id"] == str(made[0]._correlation_id)

    run_doc = json.loads((run_dir / "run.json").read_text())
    assert run_doc["workflow_id"] == _WORKFLOW_ID
    assert run_doc["task_type"] == "summarization"
    assert run_doc["max_tokens"] == 512
    assert run_doc["base_url"] == _BASE_URL

    # the paths are reported, so a customer knows where their work landed
    assert str(run_dir / "result.txt") in result.stderr
    assert made[0].submitted == {
        "prompt": "Summarize what a delegation receipt proves.",
        "task_type": "summarization",
        "max_tokens": 512,
    }


def test_delegate_uses_the_stored_credential_without_it_appearing_in_argv(
    tmp_path: Path,
) -> None:
    home = _logged_in(tmp_path)
    factory, made = _factory()

    result = CliRunner().invoke(
        cloud_group,
        [
            "delegate",
            "p",
            "--task-type",
            "summarization",
            "--output-dir",
            str(tmp_path / "runs"),
            "--onex-home",
            str(home),
            "--poll-interval",
            "0",
        ],
        obj={"transport_factory": factory},
    )

    assert result.exit_code == 0, result.output
    assert made[0].base_url == _BASE_URL
    assert made[0].api_key.get_secret_value() == _KEY


def test_delegate_refuses_when_no_credential_is_configured(tmp_path: Path) -> None:
    """No stored key and no key file is a refusal naming the fix — never a guess."""
    factory, _made = _factory()

    result = CliRunner().invoke(
        cloud_group,
        [
            "delegate",
            "p",
            "--task-type",
            "summarization",
            "--output-dir",
            str(tmp_path / "runs"),
            "--onex-home",
            str(tmp_path / "empty"),
        ],
        obj={"transport_factory": factory},
    )

    assert result.exit_code != 0
    assert "onex cloud login" in result.stderr


def test_delegate_refuses_a_key_file_with_no_base_url(tmp_path: Path) -> None:
    """The L7 defect class: no hardcoded origin, so an unset one must fail fast."""
    key_file = tmp_path / "key"
    key_file.write_text(_KEY)
    key_file.chmod(0o600)
    factory, _made = _factory()

    result = CliRunner().invoke(
        cloud_group,
        [
            "delegate",
            "p",
            "--task-type",
            "summarization",
            "--api-key-file",
            str(key_file),
            "--output-dir",
            str(tmp_path / "runs"),
            "--onex-home",
            str(tmp_path / "empty"),
        ],
        obj={"transport_factory": factory},
    )

    assert result.exit_code != 0
    assert "no default gateway" in result.stderr


def test_delegate_refuses_a_world_readable_key_file(tmp_path: Path) -> None:
    key_file = tmp_path / "key"
    key_file.write_text(_KEY)
    key_file.chmod(0o644)
    factory, _made = _factory()

    result = CliRunner().invoke(
        cloud_group,
        [
            "delegate",
            "p",
            "--task-type",
            "summarization",
            "--api-key-file",
            str(key_file),
            "--base-url",
            _BASE_URL,
            "--output-dir",
            str(tmp_path / "runs"),
        ],
        obj={"transport_factory": factory},
    )

    assert result.exit_code != 0
    assert "0600" in result.stderr


def test_delegate_surfaces_a_fenced_type_refusal_verbatim(tmp_path: Path) -> None:
    home = _logged_in(tmp_path)
    factory, _made = _factory(
        submit_error=ModelOnexError(
            "the gateway declares workflow type 'delegation-inference' but has "
            "it FENCED",
            error_code=EnumCoreErrorCode.UNSUPPORTED_OPERATION,
        )
    )

    result = CliRunner().invoke(
        cloud_group,
        [
            "delegate",
            "p",
            "--task-type",
            "summarization",
            "--output-dir",
            str(tmp_path / "runs"),
            "--onex-home",
            str(home),
        ],
        obj={"transport_factory": factory},
    )

    assert result.exit_code != 0
    assert "FENCED" in result.stderr


def test_delegate_surfaces_a_401_without_retrying(tmp_path: Path) -> None:
    home = _logged_in(tmp_path)
    factory, made = _factory(
        submit_error=ModelOnexError(
            "the gateway rejected the API key (401)",
            error_code=EnumCoreErrorCode.AUTHENTICATION_ERROR,
        )
    )

    result = CliRunner().invoke(
        cloud_group,
        [
            "delegate",
            "p",
            "--task-type",
            "summarization",
            "--output-dir",
            str(tmp_path / "runs"),
            "--onex-home",
            str(home),
        ],
        obj={"transport_factory": factory},
    )

    assert result.exit_code != 0
    assert "401" in result.stderr
    assert len(made) == 1


def test_a_quota_failed_run_exits_nonzero_and_still_saves_the_receipt(
    tmp_path: Path,
) -> None:
    """The quota-dead shape: accepted, terminal ``failed``, no content.

    This must NOT read as an empty success, and the receipt must survive —
    a failed run's receipt is precisely the evidence for why it failed.
    """
    home = _logged_in(tmp_path)
    out = tmp_path / "runs"
    factory, _made = _factory(
        terminal_status="failed",
        receipt=_receipt(result_content=None, status="failed"),
    )

    result = CliRunner().invoke(
        cloud_group,
        [
            "delegate",
            "p",
            "--task-type",
            "summarization",
            "--output-dir",
            str(out),
            "--onex-home",
            str(home),
            "--poll-interval",
            "0",
        ],
        obj={"transport_factory": factory},
    )

    assert result.exit_code != 0
    assert "no content" in result.stderr
    assert "NOT retried" in result.stderr

    run_dir = out / _WORKFLOW_ID
    assert (run_dir / "receipt.json").exists()
    # an empty result.txt would misrepresent "no content" as "an empty answer"
    assert not (run_dir / "result.txt").exists()


# ---------------------------------------------------------------------------
# the receipt has to be about the submission that was made (OMN-17938)
#
# Everything above proves the command COPIES the gateway's answer faithfully.
# None of it can fail when the answer is about a different run: the run
# directory is named from the ack, the fields inside come from the response,
# and nothing in between compares the two. These are the tests that can.
# ---------------------------------------------------------------------------

_OTHER_WORKFLOW_ID = uuid.UUID("11111111-2222-3333-4444-555555555555")


def _delegate(tmp_path: Path, home: Path, factory: Any, out: Path) -> Any:
    return CliRunner().invoke(
        cloud_group,
        [
            "delegate",
            "Summarize what a delegation receipt proves.",
            "--task-type",
            "summarization",
            "--output-dir",
            str(out),
            "--onex-home",
            str(home),
            "--poll-interval",
            "0",
        ],
        obj={"transport_factory": factory},
    )


def test_delegate_refuses_a_receipt_for_a_different_workflow(tmp_path: Path) -> None:
    """A receipt filed under another run's id is not evidence about this one."""
    home = _logged_in(tmp_path)
    out = tmp_path / "runs"
    factory, _made = _factory(receipt_workflow_id=_OTHER_WORKFLOW_ID)

    result = _delegate(tmp_path, home, factory, out)

    assert result.exit_code != 0
    assert str(_OTHER_WORKFLOW_ID) in result.stderr
    assert _WORKFLOW_ID in result.stderr
    # nothing is written: a mislabelled receipt on disk outlives the session
    # that could have explained it.
    assert not (out / _WORKFLOW_ID).exists()
    assert not (out / str(_OTHER_WORKFLOW_ID)).exists()


def test_delegate_refuses_a_receipt_carrying_another_runs_correlation_id(
    tmp_path: Path,
) -> None:
    """The relabelled-stale-answer shape, and the one the workflow id misses.

    Same workflow id, a correlation id from some earlier submission, and a
    different model on the receipt. Copied faithfully, this reports the route
    that answered SOMETHING ELSE as the route that answered this prompt.
    """
    home = _logged_in(tmp_path)
    out = tmp_path / "runs"
    stale_correlation_id = uuid.uuid4()
    factory, _made = _factory(
        receipt_correlation_id=stale_correlation_id,
        receipt=_receipt(terminal_model_used="a-route-that-answered-something-else"),
    )

    result = _delegate(tmp_path, home, factory, out)

    assert result.exit_code != 0
    assert str(stale_correlation_id) in result.stderr
    assert "a-route-that-answered-something-else" not in result.stdout
    assert not (out / _WORKFLOW_ID).exists()


def test_delegate_refuses_a_terminal_status_for_a_different_submission(
    tmp_path: Path,
) -> None:
    """``run.json``'s terminal_status comes from this envelope — same rule."""
    home = _logged_in(tmp_path)
    out = tmp_path / "runs"
    other_correlation_id = uuid.uuid4()
    factory, _made = _factory(status_correlation_id=other_correlation_id)

    result = _delegate(tmp_path, home, factory, out)

    assert result.exit_code != 0
    assert str(other_correlation_id) in result.stderr
    assert not (out / _WORKFLOW_ID).exists()


def test_delegate_still_saves_a_bound_receipt_that_reports_failure(
    tmp_path: Path,
) -> None:
    """Positive control for the three refusals above.

    A terminal ``failed`` run binds to its submission perfectly well; the
    refusal is about identity, never about the disposition. Without this, a
    check that rejected everything would look identical to a correct one.
    """
    home = _logged_in(tmp_path)
    out = tmp_path / "runs"
    factory, _made = _factory(
        terminal_status="failed",
        receipt=_receipt(result_content=None, status="failed"),
    )

    result = _delegate(tmp_path, home, factory, out)

    assert result.exit_code != 0
    assert "NOT retried" in result.stderr
    assert (out / _WORKFLOW_ID / "receipt.json").exists()


# ---------------------------------------------------------------------------
# receipt — retrieve a run that outlived its poll window (AC4)
# ---------------------------------------------------------------------------


def test_receipt_fetches_and_saves_an_existing_run(tmp_path: Path) -> None:
    home = _logged_in(tmp_path)
    out = tmp_path / "runs"
    factory, _made = _factory()

    result = CliRunner().invoke(
        cloud_group,
        [
            "receipt",
            _WORKFLOW_ID,
            "--output-dir",
            str(out),
            "--onex-home",
            str(home),
        ],
        obj={"transport_factory": factory},
    )

    assert result.exit_code == 0, result.output
    assert (out / _WORKFLOW_ID / "receipt.json").exists()
    assert "A delegation receipt proves what ran." in result.stdout


# ---------------------------------------------------------------------------
# login / status / logout
# ---------------------------------------------------------------------------


def test_login_reads_the_key_from_stdin_and_never_prints_it(tmp_path: Path) -> None:
    home = tmp_path / "onex-home"

    result = CliRunner().invoke(
        cloud_group,
        ["login", "--base-url", _BASE_URL, "--api-key-stdin", "--onex-home", str(home)],
        input=_KEY,
    )

    assert result.exit_code == 0, result.output
    assert _KEY not in result.stdout
    assert json.loads((home / "credentials.json").read_text()) == {
        "default-cloud-api-key": _KEY
    }


def test_login_has_no_option_that_takes_the_key_as_a_value() -> None:
    """A flag value lands in the process table, the shell history and exec logs."""
    login = cloud_group.commands["login"]
    option_names = {name for param in login.params for name in param.opts}

    assert "--api-key" not in option_names
    assert "--api-key-stdin" in option_names


def test_status_reports_the_endpoint_and_never_the_key(tmp_path: Path) -> None:
    home = _logged_in(tmp_path)

    result = CliRunner().invoke(cloud_group, ["status", "--onex-home", str(home)])

    assert result.exit_code == 0, result.output
    assert _BASE_URL in result.stdout
    assert _KEY not in result.stdout


def test_logout_removes_the_stored_key(tmp_path: Path) -> None:
    home = _logged_in(tmp_path)

    result = CliRunner().invoke(cloud_group, ["logout", "--onex-home", str(home)])

    assert result.exit_code == 0, result.output
    assert json.loads((home / "credentials.json").read_text()) == {}


def test_delegate_offers_only_the_gateway_declared_task_types() -> None:
    """A closed choice turns a server-side 400 into a shell completion."""
    delegate = cloud_group.commands["delegate"]
    task_type = next(p for p in delegate.params if "--task-type" in p.opts)

    choices = set(task_type.type.choices)  # type: ignore[attr-defined]
    assert "summarization" in choices
    assert "code_generation" in choices
    assert "chat" not in choices


# ---------------------------------------------------------------------------
# malformed invocation (OMN-17937) — every wrong invocation names its own fault
#
# Two separate properties live here, and they fail for different reasons.
#
# The workflow id is the sharp one. All three models in
# ``omnimarket.cloud.model_cloud_delegation`` type it ``uuid.UUID``; the CLI was
# the only surface that widened it back to ``str``, and that string reached two
# sinks unvalidated — the gateway URL path (f-string interpolated in
# ``TransportCloudDelegation.receipt``) and the local output path
# (``output_dir / workflow_id``, then ``mkdir(parents=True)`` and a write). So
# these assert ORDER and CONTAINMENT, not just an exit code: the refusal has to
# land before credential resolution, before the network, and before any
# directory is created.
#
# The rest are a ratchet over behaviour click already gets right. That is the
# point: nothing asserted it, so a later catch-all subcommand, a widened
# ``Choice`` or a dropped ``IntRange`` would degrade a customer-facing error
# message silently. Gate row 12 / R-DELEG-25.
# ---------------------------------------------------------------------------


def _assert_named_failure(result: Any) -> str:
    """Every operator error exits non-zero, is named, and shows no traceback."""
    assert result.exit_code != 0
    text = result.stderr or result.output
    assert "Error:" in text, text
    assert "Traceback" not in text, text
    # click raises SystemExit for a usage error and ClickException is caught by
    # the runner; any OTHER exception type reaching here is an unhandled crash.
    assert result.exception is None or isinstance(result.exception, SystemExit), (
        result.exception
    )
    return text


def test_receipt_refuses_a_non_uuid_workflow_id_before_resolving_a_credential(
    tmp_path: Path,
) -> None:
    """The id is a URL path segment, so it is validated first — not after auth.

    Proven by the message: an empty ``--onex-home`` would otherwise produce the
    'no credential configured' refusal, and that refusal arriving instead of a
    named argument error is exactly the ordering defect.
    """
    factory, made = _factory()
    output_dir = tmp_path / "runs"

    result = CliRunner().invoke(
        cloud_group,
        [
            "receipt",
            "not-a-uuid",
            "--output-dir",
            str(output_dir),
            "--onex-home",
            str(tmp_path / "empty"),
        ],
        obj={"transport_factory": factory},
    )

    text = _assert_named_failure(result)
    assert "WORKFLOW_ID" in text, text
    assert "UUID" in text, text
    assert "onex cloud login" not in text, text
    assert made == []
    assert not output_dir.exists()


def test_receipt_refuses_a_traversing_workflow_id_and_writes_nothing_outside(
    tmp_path: Path,
) -> None:
    """``output_dir / workflow_id`` is a real traversal sink, so prove containment.

    A credential IS configured and the transport IS faked here, deliberately:
    with the id unvalidated the command runs to completion and writes its
    receipt outside the directory the operator named.
    """
    home = _logged_in(tmp_path)
    factory, _made = _factory()
    output_dir = tmp_path / "sandbox" / "runs"
    escaped = tmp_path / "sandbox" / "escaped"

    result = CliRunner().invoke(
        cloud_group,
        [
            "receipt",
            "../escaped",
            "--output-dir",
            str(output_dir),
            "--onex-home",
            str(home),
        ],
        obj={"transport_factory": factory},
    )

    _assert_named_failure(result)
    assert not escaped.exists(), f"wrote outside --output-dir: {escaped}"


def test_receipt_normalises_a_non_canonical_workflow_id_on_both_sinks(
    tmp_path: Path,
) -> None:
    """A legal but non-canonical spelling must not vary the URL or the path.

    ``uuid.UUID`` accepts braced and upper-case forms; both denote the same
    workflow, so both have to reach the gateway and the disk in the one
    canonical hyphenated lower-case spelling.
    """
    home = _logged_in(tmp_path)
    factory, made = _factory()
    output_dir = tmp_path / "runs"

    result = CliRunner().invoke(
        cloud_group,
        [
            "receipt",
            "{" + _WORKFLOW_ID.upper() + "}",
            "--output-dir",
            str(output_dir),
            "--onex-home",
            str(home),
        ],
        obj={"transport_factory": factory},
    )

    assert result.exit_code == 0, result.output
    assert made[0].receipt_workflow_id == _WORKFLOW_ID
    assert (output_dir / _WORKFLOW_ID / "receipt.json").is_file()


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        pytest.param(
            ["frobnicate"],
            "No such command",
            id="unknown_subcommand",
        ),
        pytest.param(
            ["delegate", "p", "--task-type", "chat"],
            "is not one of",
            id="task_type_outside_the_gateway_taxonomy",
        ),
        pytest.param(
            ["delegate", "p", "--task-type", "summarization", "--max-tokens", "0"],
            "is not in the range",
            id="max_tokens_below_the_declared_floor",
        ),
        pytest.param(
            ["delegate", "p", "--task-type", "summarization", "--max-tokens", "abc"],
            "is not a valid integer range",
            id="max_tokens_not_an_integer",
        ),
        pytest.param(
            ["delegate", "p", "--task-type", "summarization", "--bogus", "1"],
            "No such option",
            id="unknown_option",
        ),
        pytest.param(
            ["delegate", "p"],
            "Missing option",
            id="required_task_type_omitted",
        ),
    ],
)
def test_a_malformed_invocation_fails_with_a_named_error(
    argv: list[str], expected: str
) -> None:
    """Never a traceback and never a silent success — each fault names itself."""
    result = CliRunner().invoke(cloud_group, argv, obj={})

    text = _assert_named_failure(result)
    assert expected in text, text
