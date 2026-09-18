# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The receipt says WHERE the key came from, and names only the ref (OMN-18695).

A resolver that reads the store is not observable from the outside: a run that
resolved from the store and a run that resolved from a leftover environment
variable produce identical output. The receipt carries the answer so the
property can be checked after the fact instead of asserted in a commit message.

What it records is the SOURCE and the REFERENCE. The value never appears, and
there is no field it could appear in.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

import pytest

from omnimarket.enums.enum_secret_source import EnumSecretSource
from omnimarket.inference.local_byok_credential_adapter import (
    LocalByokCredentialStore,
)
from omnimarket.inference.secret_store_resolver import resolve_api_key_with_source
from omnimarket.models.delegation.wire.model_delegate_skill_response import (
    ModelDelegateSkillResponse,
)
from omnimarket.nodes.node_llm_delegation_call_effect.models.model_llm_delegation_call_result import (
    ModelLlmDelegationCallResult,
)

pytestmark = pytest.mark.unit

_REF = "llm.openrouter.api_key"
_VALUE = "sk-customer-key"


@pytest.fixture(autouse=True)
def local_store_at_tmp(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    db_path = tmp_path / "delegation.sqlite"
    monkeypatch.setattr(
        "omnimarket.inference.local_byok_credential_adapter.default_evidence_db_path",
        lambda: db_path,
    )
    monkeypatch.delenv("ONEX_SECRET_RESOLVER_CONFIG_PATH", raising=False)
    from omnimarket.inference.secret_store_resolver import (
        clear_secret_store_resolver_cache,
    )

    clear_secret_store_resolver_cache()
    return db_path


class TestResolverReportsItsSource:
    def test_a_store_resolution_reports_store(self) -> None:
        asyncio.run(LocalByokCredentialStore().set_secret(_REF, _VALUE))

        resolved, source = resolve_api_key_with_source(_REF)

        assert resolved is not None
        assert resolved.get_secret_value() == _VALUE
        assert source is EnumSecretSource.LOCAL_STORE

    def test_an_environment_resolution_reports_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-provider ref still reads env, and says so rather than claiming store."""
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_notaproviderkey0000000000")

        _, source = resolve_api_key_with_source(
            "GITHUB_TOKEN", env_var_fallback="GITHUB_TOKEN"
        )

        assert source is EnumSecretSource.ENVIRONMENT

    def test_an_unauthenticated_backend_reports_no_source(self) -> None:
        resolved, source = resolve_api_key_with_source(None)

        assert resolved is None
        assert source is None


class TestCallResultCarriesTheSource:
    def test_fields_default_to_absent(self) -> None:
        result = ModelLlmDelegationCallResult(request_id="r", success=True)

        assert result.secret_source is None
        assert result.secret_ref is None

    def test_records_the_source_and_the_reference(self) -> None:
        result = ModelLlmDelegationCallResult(
            request_id="r",
            success=True,
            secret_source=EnumSecretSource.LOCAL_STORE,
            secret_ref=_REF,
        )

        assert result.secret_source is EnumSecretSource.LOCAL_STORE
        assert result.secret_ref == _REF


class TestReceiptCarriesTheSource:
    def _response(self, **overrides: object) -> ModelDelegateSkillResponse:
        fields: dict[str, object] = {
            "status": "completed",
            "correlation_id": uuid4(),
            "task_type": "summarization",
        }
        fields.update(overrides)
        return ModelDelegateSkillResponse(**fields)  # type: ignore[arg-type]

    def test_receipt_records_store_and_the_reference_name(self) -> None:
        response = self._response(
            secret_source=EnumSecretSource.LOCAL_STORE, secret_ref=_REF
        )

        dumped = response.model_dump(mode="json")

        assert dumped["secret_source"] == "store"
        assert dumped["secret_ref"] == _REF

    def test_the_receipt_has_no_field_that_could_hold_a_value(self) -> None:
        assert "secret_value" not in ModelDelegateSkillResponse.model_fields
        assert "api_key" not in ModelDelegateSkillResponse.model_fields

    def test_the_value_never_appears_in_a_serialized_receipt(self) -> None:
        response = self._response(
            secret_source=EnumSecretSource.LOCAL_STORE,
            secret_ref=_REF,
            response="an answer",
        )

        assert _VALUE not in response.model_dump_json()

    def test_absent_source_is_omitted_rather_than_rendered_null(self) -> None:
        """An unauthenticated local backend records nothing, not a false 'store'."""
        dumped = self._response().model_dump(mode="json")

        assert "secret_source" not in dumped
        assert "secret_ref" not in dumped
