# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The subprocess verifier binding and the endpoint routing of the default verifier.

``verifier_endpoint: subprocess:<checkout>`` runs the verifier node in a subprocess of
the checkout with the key in the child's environment (never its argv); any other
endpoint keeps the HTTP binding. An absent key or checkout fails closed, named, and the
handler refuses.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_agent_review_gate_accept_effect.adapters import (
    adapter_gate_accept_ports as ports,
)
from omnimarket.nodes.node_agent_review_gate_accept_effect.adapters.adapter_gate_accept_ports import (
    EndpointReceiptVerifier,
    PortNotBoundError,
    SubprocessReceiptVerifier,
)
from omnimarket.nodes.node_agent_review_gate_accept_effect.models.model_gate_accept import (
    ModelReceiptVerifyOutcome,
    ModelReceiptVerifyRequest,
)

pytestmark = pytest.mark.unit

HEAD = "c" * 40


def request(endpoint: str) -> ModelReceiptVerifyRequest:
    return ModelReceiptVerifyRequest(
        receipt={"id": "r-1", "head_sha": HEAD},
        repository="OmniNode-ai/example",
        pull_request=9,
        head_sha=HEAD,
        verifier_endpoint=endpoint,
    )


def checkout(tmp_path: Path) -> Path:
    path = tmp_path / "omniintelligence"
    path.mkdir()
    (path / "pyproject.toml").write_text("", encoding="utf-8")
    return path


class Recorded:
    def __init__(self, stdout: str, returncode: int = 0) -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self, argv: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append({"argv": argv, **kwargs})
        return subprocess.CompletedProcess(argv, self.returncode, self.stdout, "boom\n")


def test_an_accepted_verdict_carries_the_receipt_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = Recorded(
        json.dumps({"decision": "accepted", "receipt_id": "r-1", "detail": ""})
    )
    monkeypatch.setattr(ports.subprocess, "run", run)
    where = checkout(tmp_path)
    outcome = SubprocessReceiptVerifier(
        environ={"LOCAL_LLM_SHARED_SECRET": "k-test", "VIRTUAL_ENV": "/v"}
    ).verify(request(f"subprocess:{where}"))
    assert outcome == ModelReceiptVerifyOutcome(accepted=True, receipt_id="r-1")
    call = run.calls[0]
    assert call["cwd"] == where
    assert "k-test" not in " ".join(call["argv"])
    assert call["env"]["LOCAL_LLM_SHARED_SECRET"] == "k-test"
    assert "VIRTUAL_ENV" not in call["env"]
    asked = json.loads(call["input"])
    assert asked["context"] == {
        "repo": "OmniNode-ai/example",
        "pr": 9,
        "head_sha": HEAD,
    }


def test_a_refused_verdict_carries_its_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = Recorded(json.dumps({"decision": "refused", "reason": "forged_signature"}))
    monkeypatch.setattr(ports.subprocess, "run", run)
    outcome = SubprocessReceiptVerifier(
        environ={"LOCAL_LLM_SHARED_SECRET": "k"}
    ).verify(request(f"subprocess:{checkout(tmp_path)}"))
    assert outcome == ModelReceiptVerifyOutcome(
        accepted=False, reason="forged_signature"
    )


def test_a_child_that_fails_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ports.subprocess, "run", Recorded("", returncode=1))
    with pytest.raises(RuntimeError):
        SubprocessReceiptVerifier(environ={"LOCAL_LLM_SHARED_SECRET": "k"}).verify(
            request(f"subprocess:{checkout(tmp_path)}")
        )


def test_an_absent_key_is_unbound(tmp_path: Path) -> None:
    with pytest.raises(PortNotBoundError) as caught:
        SubprocessReceiptVerifier(environ={}).verify(
            request(f"subprocess:{checkout(tmp_path)}")
        )
    assert "LOCAL_LLM_SHARED_SECRET" in str(caught.value)


def test_an_absent_checkout_is_unbound(tmp_path: Path) -> None:
    with pytest.raises(PortNotBoundError):
        SubprocessReceiptVerifier(environ={"LOCAL_LLM_SHARED_SECRET": "k"}).verify(
            request(f"subprocess:{tmp_path / 'absent'}")
        )


def test_the_endpoint_picks_the_binding() -> None:
    picked: list[str] = []

    class Fake:
        def __init__(self, name: str) -> None:
            self.name = name

        def verify(self, req: ModelReceiptVerifyRequest) -> ModelReceiptVerifyOutcome:
            picked.append(self.name)
            return ModelReceiptVerifyOutcome(accepted=False, reason=self.name)

    verifier = EndpointReceiptVerifier(
        http=Fake("http"),
        subprocess_verifier=Fake("subprocess"),
    )
    verifier.verify(request("subprocess:../omniintelligence"))
    verifier.verify(request("http://verifier.example.test/verify"))
    assert picked == ["subprocess", "http"]
