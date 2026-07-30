# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Product-body stamp authenticates with a PRODUCT-scoped credential (OMN-15441).

RED-before this module: ``_write_sync`` threaded the single
onex_change_control-scoped OCC token into ``_patch_product_body``, so the one
call that targets the PRODUCT repo used a credential whose mint scope
(``repositories: onex_change_control``) can never reach it. Live failure —
``omnimarket#1958``, run 30496115784: OCC#5516 created, then
``PATCH /repos/OmniNode-ai/omnimarket/pulls/1958`` -> 403 "Resource not
accessible by integration".

These tests drive the real ``_write_sync`` write path with git/gh transport
stubbed, and assert on the token each REST call actually carried. That is the
seam the defect lived on: a test that only checked ``_patch_product_body`` in
isolation would have passed against the broken wiring, because the bug was the
argument ``_write_sync`` chose — not the patch function itself.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from pydantic import SecretStr

from omnimarket.github_api import GitHubApiError
from omnimarket.nodes.contract_topics import contract_secret_ref
from omnimarket.nodes.node_occ_companion_compute.models.model_occ_companion_request import (
    ModelObservedProbe,
    ModelOccCompanionRequest,
)
from omnimarket.nodes.node_occ_companion_effect.handlers.handler_occ_companion_effect import (
    _CONTRACT_PATH,
    HandlerOccCompanionEffect,
    _resolve_product_token,
)
from omnimarket.nodes.node_occ_companion_effect.models.model_occ_companion_effect_request import (
    ModelOccCompanionEffectRequest,
)
from omnimarket.nodes.node_occ_state_effect.handlers.handler_occ_state_effect import (
    HandlerOccStateEffect,
)
from omnimarket.nodes.node_occ_state_effect.models.model_occ_state_request import (
    ModelOccStateRequest,
)

_MOD = (
    "omnimarket.nodes.node_occ_companion_effect.handlers.handler_occ_companion_effect"
)

_OCC_TOKEN = "ghs_occ_scoped_onex_change_control_only"
_PRODUCT_TOKEN = "ghs_product_scoped_omnimarket_only"
_PRODUCT_PATCH_PATH = "/repos/OmniNode-ai/omnimarket/pulls/1958"


class _StubStateHandler(HandlerOccStateEffect):
    """RSD-2 read stub — returns a canned pass-1 request, performs no I/O."""

    def __init__(self, request: ModelOccCompanionRequest) -> None:
        self._request = request

    async def handle(self, request: ModelOccStateRequest) -> ModelOccCompanionRequest:
        return self._request


def _canned_request() -> ModelOccCompanionRequest:
    return ModelOccCompanionRequest(
        repo="OmniNode-ai/omnimarket",
        pr_number=1958,
        pr_head_sha="a1b2c3d4e5f60718293a4b5c6d7e8f9012345678",
        pr_title="fix(OMN-15430): shlex-aware shape guard tokenization",
        pr_body="Closes OMN-15430",
        run_timestamp="2026-07-29T22:30:00Z",
        product_probe=ModelObservedProbe(
            command="gh pr view 1958",
            stdout='{"number":1958,"state":"OPEN"}',
            exit_code=0,
        ),
    )


class _RestRecorder:
    """Records (method, path, token) for every REST call the handler makes."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def rest_json(self, method: str, path: str, *, token: str, body=None):  # type: ignore[no-untyped-def]
        self.calls.append((method, path, token))
        if path.endswith("/pulls") and method == "POST":
            return {"number": 5516, "html_url": "https://github.test/occ/5516"}
        if path == "/repos/OmniNode-ai/onex_change_control":
            return {"default_branch": "dev"}
        return {}

    def rest_json_array(self, method: str, path: str, *, token: str, body=None):  # type: ignore[no-untyped-def]
        self.calls.append((method, path, token))
        return []

    def token_for(self, method: str, path: str) -> str | None:
        for call_method, call_path, call_token in self.calls:
            if call_method == method and call_path == path:
                return call_token
        return None


def _run_write_path(recorder: _RestRecorder, monkeypatch) -> object:  # type: ignore[no-untyped-def]
    """Drive the real ``_write_sync`` with git transport + lease stubbed out."""
    handler = HandlerOccCompanionEffect(
        state_handler=_StubStateHandler(_canned_request())
    )
    with (
        patch(f"{_MOD}._resolve_github_token", return_value=_OCC_TOKEN),
        patch(f"{_MOD}.acquire_occ_companion_lease", return_value=True),
        patch(f"{_MOD}.release_occ_companion_lease", return_value=None),
        patch(f"{_MOD}.rest_json", side_effect=recorder.rest_json),
        patch(f"{_MOD}.rest_json_array", side_effect=recorder.rest_json_array),
        patch.object(HandlerOccCompanionEffect, "_clone_and_branch", return_value=None),
        patch.object(HandlerOccCompanionEffect, "_write_files", return_value=None),
        patch.object(HandlerOccCompanionEffect, "_commit_all", return_value=None),
        patch.object(HandlerOccCompanionEffect, "_push", return_value=None),
        patch.object(
            HandlerOccCompanionEffect, "_assert_append_only", return_value=None
        ),
        patch.object(
            HandlerOccCompanionEffect,
            "_assert_contracts_yamlfmt_stable",
            return_value=None,
        ),
        patch.object(
            HandlerOccCompanionEffect,
            "_head_sha",
            return_value="0" * 40,
        ),
        patch.object(
            HandlerOccCompanionEffect,
            "_observe_occ_probe",
            return_value=ModelObservedProbe(
                command="gh pr view 5516", stdout="{}", exit_code=0
            ),
        ),
    ):
        import asyncio

        return asyncio.run(
            handler.handle(
                ModelOccCompanionEffectRequest(
                    repo="OmniNode-ai/omnimarket", pr_number=1958, mode="mutate"
                )
            )
        )


@pytest.mark.unit
class TestProductBodyPatchCredential:
    def test_product_patch_uses_product_token_not_occ_token(self, monkeypatch) -> None:
        """The one product-repo write carries the product-scoped credential.

        RED before the fix: this asserted ``_OCC_TOKEN``, i.e. the exact 403.
        """
        monkeypatch.setenv("OMNI_OCC_PRODUCT_TOKEN", _PRODUCT_TOKEN)
        recorder = _RestRecorder()
        _run_write_path(recorder, monkeypatch)

        product_patch_token = recorder.token_for("PATCH", _PRODUCT_PATCH_PATH)
        assert product_patch_token is not None, (
            "handler never PATCHed the product PR body — the stamp is the "
            "behavior under test"
        )
        assert product_patch_token == _PRODUCT_TOKEN, (
            "product-body stamp must use the product-repo-scoped credential; "
            "the onex_change_control-scoped OCC token 403s here (OMN-15441)"
        )

    def test_occ_repo_writes_still_use_the_occ_token(self, monkeypatch) -> None:
        """Credential separation is one-directional — OCC calls are unchanged.

        Guards the obvious over-correction: swapping BOTH halves onto the
        product token would break the OCC PR create instead.
        """
        monkeypatch.setenv("OMNI_OCC_PRODUCT_TOKEN", _PRODUCT_TOKEN)
        recorder = _RestRecorder()
        _run_write_path(recorder, monkeypatch)

        occ_calls = [c for c in recorder.calls if "onex_change_control" in c[1]]
        assert occ_calls, "expected at least one onex_change_control REST call"
        for method, path, token in occ_calls:
            assert token == _OCC_TOKEN, (
                f"{method} {path} must keep the OCC-scoped credential; the "
                "product token has no access to onex_change_control"
            )

    def test_absent_product_token_falls_back_to_occ_token(self, monkeypatch) -> None:
        """The bus-runtime path (one cross-repo PAT for both halves) still works."""
        monkeypatch.delenv("OMNI_OCC_PRODUCT_TOKEN", raising=False)
        recorder = _RestRecorder()
        _run_write_path(recorder, monkeypatch)

        assert recorder.token_for("PATCH", _PRODUCT_PATCH_PATH) == _OCC_TOKEN


@pytest.mark.unit
class TestResolveProductToken:
    def test_dedicated_when_env_var_set(self, monkeypatch) -> None:
        monkeypatch.setenv("OMNI_OCC_PRODUCT_TOKEN", _PRODUCT_TOKEN)
        assert _resolve_product_token(_OCC_TOKEN) == (_PRODUCT_TOKEN, True)

    def test_fallback_when_env_var_absent(self, monkeypatch) -> None:
        monkeypatch.delenv("OMNI_OCC_PRODUCT_TOKEN", raising=False)
        assert _resolve_product_token(_OCC_TOKEN) == (_OCC_TOKEN, False)

    def test_whitespace_only_is_treated_as_absent(self, monkeypatch) -> None:
        """An empty `${{ steps.*.outputs.token }}` expansion must not win.

        A skipped mint step expands to the empty string, not an unset var.
        Treating that as a real credential would send an empty Bearer header
        and produce a 401 that reads nothing like a scope problem.
        """
        monkeypatch.setenv("OMNI_OCC_PRODUCT_TOKEN", "   ")
        assert _resolve_product_token(_OCC_TOKEN) == (_OCC_TOKEN, False)


@pytest.mark.unit
class TestProductTokenSeamIsContractEnforced:
    """The ``secrets:`` declaration must be load-bearing, not decorative.

    An earlier revision of this fix read ``os.environ[...]`` directly while the
    contract declared ``OMNI_OCC_PRODUCT_TOKEN`` under ``secrets:``. That is the
    ``feedback_no_invisible_env_config_in_contract_overlays`` class: the
    declaration looked like a seam but nothing read it, so renaming the contract
    key would have silently changed nothing, and a credential held in the secret
    store rather than process env would have been invisible.
    """

    def test_contract_declares_the_product_token_secret(self) -> None:
        """The ref must resolve from the contract, or the handler fails loudly."""
        assert (
            contract_secret_ref(_CONTRACT_PATH, "OMNI_OCC_PRODUCT_TOKEN")
            == "OMNI_OCC_PRODUCT_TOKEN"
        )

    def test_product_token_is_resolved_through_the_contract_ref(
        self, monkeypatch
    ) -> None:
        """Resolution goes through the contract-declared ref, not a bare env read."""
        monkeypatch.delenv("OMNI_OCC_PRODUCT_TOKEN", raising=False)
        seen: dict[str, object] = {}

        def _fake_resolve(ref, *, required=True, env_var_fallback=None, store=None):
            seen["ref"] = ref
            seen["required"] = required
            seen["env_var_fallback"] = env_var_fallback
            return SecretStr(_PRODUCT_TOKEN)

        with patch(f"{_MOD}.resolve_api_key", side_effect=_fake_resolve):
            assert _resolve_product_token(_OCC_TOKEN) == (_PRODUCT_TOKEN, True)

        # The ref comes from the contract, and the fallback is deliberate
        # (required=False) rather than an accidental swallow.
        assert seen["ref"] == contract_secret_ref(
            _CONTRACT_PATH, "OMNI_OCC_PRODUCT_TOKEN"
        )
        assert seen["required"] is False
        assert seen["env_var_fallback"] == "OMNI_OCC_PRODUCT_TOKEN"

    def test_a_store_held_token_resolves_with_no_env_var_set(self, monkeypatch) -> None:
        """RED under the old raw-``os.environ`` read.

        The ``.201`` effects lane provisions credentials through the secret
        store, not process env. With the env var absent, the old implementation
        returned the OCC token — the credential that 403s — while reporting
        ``dedicated=False``, i.e. a silent downgrade to the broken path.
        """
        monkeypatch.delenv("OMNI_OCC_PRODUCT_TOKEN", raising=False)

        class _StoreOnlySecretStore:
            async def get_secret(self, ref: str) -> str | None:
                return _PRODUCT_TOKEN if ref == "OMNI_OCC_PRODUCT_TOKEN" else None

        with patch(
            "omnimarket.inference.secret_store_resolver._default_secret_store",
            return_value=_StoreOnlySecretStore(),
        ):
            assert _resolve_product_token(_OCC_TOKEN) == (_PRODUCT_TOKEN, True)


@pytest.mark.unit
class TestProductPatch403IsSelfDiagnosing:
    """A 403 must name the credential and the missing grant, not echo GitHub."""

    def _patch_raising_403(self, *, dedicated: bool) -> GitHubApiError:
        handler = HandlerOccCompanionEffect(
            state_handler=_StubStateHandler(_canned_request())
        )
        with (
            patch(
                f"{_MOD}.rest_json",
                side_effect=GitHubApiError(
                    '{"message":"Resource not accessible by integration"}',
                    status_code=403,
                ),
            ),
            pytest.raises(GitHubApiError) as excinfo,
        ):
            handler._patch_product_body(
                "OmniNode-ai",
                "omnimarket",
                1958,
                "new body\n\nEvidence-Source: OCC#5516",
                "old body",
                "ghs_whatever",
                product_token_dedicated=dedicated,
            )
        return excinfo.value

    def test_fallback_403_names_the_scope_mismatch(self) -> None:
        message = str(self._patch_raising_403(dedicated=False))
        assert "OmniNode-ai/omnimarket#1958" in message
        assert "OMNI_OCC_PRODUCT_TOKEN" in message
        assert "onex_change_control-scoped" in message
        assert "pull_requests: write" in message

    def test_dedicated_403_names_the_dedicated_credential(self) -> None:
        message = str(self._patch_raising_403(dedicated=True))
        assert "dedicated OMNI_OCC_PRODUCT_TOKEN credential" in message

    def test_non_403_errors_propagate_untouched(self) -> None:
        """Only 403 gets the scope narrative; a 422 must not be mislabeled."""
        handler = HandlerOccCompanionEffect(
            state_handler=_StubStateHandler(_canned_request())
        )
        original = GitHubApiError('{"message":"Validation failed"}', status_code=422)
        with (
            patch(f"{_MOD}.rest_json", side_effect=original),
            pytest.raises(GitHubApiError) as excinfo,
        ):
            handler._patch_product_body(
                "OmniNode-ai", "omnimarket", 1958, "new", "old", "tok"
            )
        assert excinfo.value is original
