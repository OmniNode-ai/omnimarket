# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""No token or key value reaches stdout, stderr, a log record or the result.

The signer is a seam that returns a canary token and the key reader returns a canary key, so the values in motion are
known. Every output is captured and searched. The assertions compare a list of leaking outputs to the empty list, so a
failure names the output and never prints the canary.
"""

from __future__ import annotations

import asyncio
import logging

import pytest
from pydantic import SecretStr

from omnimarket.nodes.node_dev_proof_tenant_token_effect.handlers import (
    HandlerDevProofTenantToken,
    ModelDevProofTokenCommand,
    ModelDevProofTokenResult,
)
from omnimarket.nodes.node_dev_proof_tenant_token_effect.models.model_dev_proof_tenant_token import (
    EnumDevProofTokenOutcome,
    EnumDevProofTokenStatus,
)

from .fakes import SIGNING_FOLDER, SIGNING_NAME, FakeSeeder, overlay

pytestmark = pytest.mark.unit

_CANARY_TOKEN = "canary-token-7f3a91c2e5b04d68"
_CANARY_KEY = "canary-key-b81d6e0a94f273c5"


class _Reader:
    async def read(self, folder: str, name: str) -> SecretStr | None:
        if (folder, name) == (SIGNING_FOLDER, SIGNING_NAME):
            return SecretStr(_CANARY_KEY)
        return None


class _Signer:
    def sign(self, claims: object, key: SecretStr, algorithm: str) -> SecretStr:
        return SecretStr(_CANARY_TOKEN)


class _LeakySigner:
    """A signer whose library error echoes the key and a token, as a careless library might."""

    def sign(self, claims: object, key: SecretStr, algorithm: str) -> SecretStr:
        raise ValueError(f"bad key {key.get_secret_value()} for {_CANARY_TOKEN}")


class _LeakySeeder(FakeSeeder):
    async def seed(self, name: str, value: SecretStr) -> EnumDevProofTokenStatus:
        raise RuntimeError(f"store refused {value.get_secret_value()}")


def _leaks(*outputs: tuple[str, str]) -> list[str]:
    return [
        label for label, text in outputs if _CANARY_TOKEN in text or _CANARY_KEY in text
    ]


def _outputs(
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    result: ModelDevProofTokenResult,
) -> list[tuple[str, str]]:
    captured = capsys.readouterr()
    dump = result.model_dump_json()
    return [
        ("stdout", captured.out),
        ("stderr", captured.err),
        ("log", "\n".join(r.getMessage() + str(r.args) for r in caplog.records)),
        ("result-json", dump),
        ("result-repr", repr(result)),
        ("result-str", str(result)),
    ]


def test_dev_proof_token_mint_never_prints_the_detector_sees_a_leak(
    capsys: pytest.CaptureFixture[str],
) -> None:
    print(_CANARY_TOKEN)
    captured = capsys.readouterr()
    assert _leaks(("stdout", captured.out)) == ["stdout"]


def test_dev_proof_token_mint_never_prints_on_a_mint(
    capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    seeder = FakeSeeder()
    handler = HandlerDevProofTenantToken(
        overlay=overlay(), key_reader=_Reader(), signer=_Signer(), seeder=seeder
    )
    result = asyncio.run(
        handler.handle(ModelDevProofTokenCommand(lane="dev", write=True))
    )
    assert result.outcome is EnumDevProofTokenOutcome.MINTED
    # The canary did travel: the seed node's seam received it, so the absence below is not an empty run.
    assert set(seeder.store.values()) == {_CANARY_TOKEN}
    assert _leaks(*_outputs(capsys, caplog, result)) == []


def test_dev_proof_token_mint_never_prints_names_and_status_only(
    capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    handler = HandlerDevProofTenantToken(
        overlay=overlay(), key_reader=_Reader(), signer=_Signer(), seeder=FakeSeeder()
    )
    result = asyncio.run(
        handler.handle(ModelDevProofTokenCommand(lane="dev", write=True))
    )
    assert result.signing_key_name == SIGNING_NAME
    assert [(t.secret_name, t.status) for t in result.tokens] == [
        ("TEST_TOKEN_H051", EnumDevProofTokenStatus.WRITTEN),
    ]
    assert any("token_statuses=['written']" in r.getMessage() for r in caplog.records)
    assert not any("TEST_TOKEN_H051" in r.getMessage() for r in caplog.records)


def test_dev_proof_token_mint_never_prints_on_a_refusal(
    capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    handler = HandlerDevProofTenantToken(
        overlay=overlay(), key_reader=_Reader(), signer=_Signer(), seeder=FakeSeeder()
    )
    result = asyncio.run(
        handler.handle(ModelDevProofTokenCommand(lane="prod", write=True))
    )
    assert result.outcome is EnumDevProofTokenOutcome.REFUSED_NOT_DEV
    assert _leaks(*_outputs(capsys, caplog, result)) == []


def test_dev_proof_token_mint_never_prints_when_signing_fails(
    capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    handler = HandlerDevProofTenantToken(
        overlay=overlay(),
        key_reader=_Reader(),
        signer=_LeakySigner(),
        seeder=FakeSeeder(),
    )
    result = asyncio.run(
        handler.handle(ModelDevProofTokenCommand(lane="dev", write=True))
    )
    assert result.outcome is EnumDevProofTokenOutcome.SIGNING_FAILED
    assert _leaks(*_outputs(capsys, caplog, result)) == []


def test_dev_proof_token_mint_never_prints_when_the_seed_node_fails(
    capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    handler = HandlerDevProofTenantToken(
        overlay=overlay(),
        key_reader=_Reader(),
        signer=_Signer(),
        seeder=_LeakySeeder(),
    )
    result = asyncio.run(
        handler.handle(ModelDevProofTokenCommand(lane="dev", write=True))
    )
    assert result.outcome is EnumDevProofTokenOutcome.SEED_FAILED
    assert _leaks(*_outputs(capsys, caplog, result)) == []


def test_dev_proof_token_mint_never_prints_when_the_seed_node_reports_failed(
    capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    handler = HandlerDevProofTenantToken(
        overlay=overlay(),
        key_reader=_Reader(),
        signer=_Signer(),
        seeder=FakeSeeder(fail_with=EnumDevProofTokenStatus.FAILED),
    )
    result = asyncio.run(
        handler.handle(ModelDevProofTokenCommand(lane="dev", write=True))
    )
    assert result.outcome is EnumDevProofTokenOutcome.SEED_FAILED
    assert _leaks(*_outputs(capsys, caplog, result)) == []
