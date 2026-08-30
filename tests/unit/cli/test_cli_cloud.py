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


def _ack() -> ModelCloudDelegationAck:
    return ModelCloudDelegationAck.model_validate(
        {
            "workflow_id": _WORKFLOW_ID,
            "envelope_id": str(uuid.uuid4()),
            "correlation_id": str(uuid.uuid4()),
            "workflow_type": "delegation-inference",
            "status": "published",
        }
    )


def _status(status: str) -> ModelCloudDelegationStatus:
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
) -> ModelCloudDelegationReceipt:
    return ModelCloudDelegationReceipt.model_validate(
        {
            "workflow_id": _WORKFLOW_ID,
            "tenant_id": str(uuid.uuid4()),
            "correlation_id": str(uuid.uuid4()),
            "workflow_type": "delegation-inference",
            "status": status,
            "submitted_at": "2026-08-29T15:00:00Z",
            "completed_at": "2026-08-29T15:00:08Z",
            "terminal_model_used": "gemini-2.5-flash-lite",
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
    """Stands in for ``TransportCloudDelegation`` at the constructor seam."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: Any,
        timeout_seconds: float = 30.0,
        submit_error: Exception | None = None,
        terminal_status: str = "completed",
        receipt: ModelCloudDelegationReceipt | None = None,
    ) -> None:
        self.base_url = base_url
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self._submit_error = submit_error
        self._terminal_status = terminal_status
        self._receipt = receipt if receipt is not None else _receipt()
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
        return _ack()

    def poll_until_terminal(
        self, workflow_id: str, *, attempts: int, interval_seconds: float
    ) -> ModelCloudDelegationStatus:
        return _status(self._terminal_status)

    def receipt(
        self, workflow_id: str, *, runner_identity: str
    ) -> ModelCloudDelegationReceipt:
        self.runner_identity = runner_identity
        return self._receipt


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
