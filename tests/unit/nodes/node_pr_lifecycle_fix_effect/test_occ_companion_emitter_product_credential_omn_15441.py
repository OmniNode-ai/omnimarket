# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The PEER OCC producer gets the same OMN-15441 fixes (remediation round 1).

An earlier revision of OMN-15441 fixed ``HandlerOccCompanionEffect`` only and
left :class:`OccCompanionEmitter` — the LIVE producer, per
``reference_two_occ_producers_canonical_not_wired`` — carrying both defects,
while the emitter's own docstring claimed it "mirrors
``HandlerOccCompanionEffect``'s ``_apply_machine_minted_label`` byte-for-byte".
That is a documented parity invariant broken silently by a one-sided fix.

Two defects closed here:

* **Response shape.** ``POST /repos/{o}/{r}/issues/{n}/labels`` returns the
  issue's full label ARRAY; routing it through dict-only ``rest_json`` raised
  "unexpected JSON response type" after the POST had already committed —
  spurious swallowed WARNING noise that falsely reports a lost provenance
  marker. (The label itself always landed: OCC#5516 carries
  ``occ:machine-minted``, applied by ``onexbot-occ-writer[bot]`` at
  2026-07-29T22:26:20Z, in the run that logged "could not apply".)

* **The 403 root cause itself, pre-cutover.** ``_patch_evidence_source`` PATCHes
  the PRODUCT PR body — the one write this producer makes outside
  ``onex_change_control`` — and authenticated it with ``_resolve_github_token``.
  Under the default ``pat`` mode that is a cross-repo PAT, so it works today and
  the defect is latent. Under ``OMNI_OCC_GITHUB_AUTH_MODE=app`` (the planned
  OMN-14893 cutover) it becomes an ``onexbot-occ-writer`` installation token
  whose installation is ``repository_selection: selected`` over
  ``onex_change_control`` only (live: ``/orgs/OmniNode-ai/installations``,
  installation 148180820) — reproducing the exact OMN-15441 403 on the live
  path. These tests fail against ``app`` mode before the fix, so the cutover no
  longer has to rediscover it in production.
"""

from __future__ import annotations

import json
import logging
import urllib.error
from typing import Any
from unittest.mock import patch

import pytest

from omnimarket.events.occ_autoauthor import OCC_MACHINE_MINTED_LABEL
from omnimarket.github_api import GitHubApiError
from omnimarket.nodes.contract_topics import contract_secret_ref
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_companion_emitter import (
    _CONTRACT_PATH,
    OccCompanionEmitter,
    _resolve_product_token,
)

_MOD = "omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_companion_emitter"

_OCC_TOKEN = "ghs_occ_scoped_onex_change_control_only"
_PRODUCT_TOKEN = "ghs_product_scoped_omnimarket_only"

_LABELS_ARRAY_RESPONSE: list[dict[str, Any]] = [
    {"id": 1, "name": OCC_MACHINE_MINTED_LABEL},
    {"id": 2, "name": "evidence"},
]

_EXISTING_BODY = "Human prose that must survive verbatim.\n"


class _FakeResponse:
    def __init__(self, payload: object) -> None:
        self._raw = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._raw

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None


# ---------------------------------------------------------------------------
# F4 — the product-body PATCH must use the PRODUCT credential
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestEvidenceSourcePatchCredential:
    """Drives the real ``_patch_evidence_source``, asserting the token carried."""

    def _run_patch(self, *, product_token_set: bool, monkeypatch) -> dict[str, Any]:
        if product_token_set:
            monkeypatch.setenv("OMNI_OCC_PRODUCT_TOKEN", _PRODUCT_TOKEN)
        else:
            monkeypatch.delenv("OMNI_OCC_PRODUCT_TOKEN", raising=False)

        captured: dict[str, Any] = {}

        def _fake_rest(method: str, path: str, *, body=None, token=None) -> dict:
            captured["method"] = method
            captured["path"] = path
            captured["token"] = token
            captured["body"] = body
            return {}

        emitter = OccCompanionEmitter()
        with (
            patch(f"{_MOD}.rest_json", side_effect=_fake_rest),
            patch(f"{_MOD}._resolve_github_token", return_value=_OCC_TOKEN),
        ):
            emitter._patch_evidence_source(
                repo="OmniNode-ai/omnimarket",
                pr_number=1958,
                occ_pr_number=5516,
                tickets=["OMN-15441"],
                existing_body=_EXISTING_BODY,
            )
        return captured

    def test_product_patch_uses_the_product_token(self, monkeypatch) -> None:
        """RED before the fix: this carried ``_OCC_TOKEN``.

        That is the live OMN-15441 403 — the onex_change_control-scoped
        credential arriving at a product-repo endpoint.
        """
        captured = self._run_patch(product_token_set=True, monkeypatch=monkeypatch)

        assert captured["token"] == _PRODUCT_TOKEN, (
            "the Evidence-Source stamp must use the product-repo-scoped "
            "credential; the onex_change_control-scoped OCC token 403s here"
        )
        assert captured["method"] == "PATCH"
        assert captured["path"] == "/repos/OmniNode-ai/omnimarket/pulls/1958"

    def test_absent_product_token_falls_back_to_the_occ_token(
        self, monkeypatch
    ) -> None:
        """The single-cross-repo-PAT bus-runtime path must keep working."""
        captured = self._run_patch(product_token_set=False, monkeypatch=monkeypatch)
        assert captured["token"] == _OCC_TOKEN

    def test_the_rendered_body_is_unchanged_by_the_credential_split(
        self, monkeypatch
    ) -> None:
        """Credential selection must not perturb the bytes written."""
        captured = self._run_patch(product_token_set=True, monkeypatch=monkeypatch)
        new_body = captured["body"]["body"]
        assert _EXISTING_BODY.strip() in new_body  # prose preserved verbatim
        assert "Evidence-Source: OCC#5516" in new_body
        assert "OMN-15441" in new_body

    def test_idempotent_no_op_makes_no_request_at_all(self, monkeypatch) -> None:
        """An already-canonical body must not spend a write (or a token)."""
        monkeypatch.setenv("OMNI_OCC_PRODUCT_TOKEN", _PRODUCT_TOKEN)
        calls: list[str] = []

        emitter = OccCompanionEmitter()

        def _fake_rest(method: str, path: str, *, body=None, token=None) -> dict:
            calls.append(path)
            return {}

        # Render once to obtain the canonical body, then feed it back in.
        first = self._run_patch(product_token_set=True, monkeypatch=monkeypatch)
        canonical = first["body"]["body"]

        with (
            patch(f"{_MOD}.rest_json", side_effect=_fake_rest),
            patch(f"{_MOD}._resolve_github_token", return_value=_OCC_TOKEN),
        ):
            emitter._patch_evidence_source(
                repo="OmniNode-ai/omnimarket",
                pr_number=1958,
                occ_pr_number=5516,
                tickets=["OMN-15441"],
                existing_body=canonical,
            )
        assert calls == []


@pytest.mark.unit
class TestEvidenceSourcePatch403IsSelfDiagnosing:
    """A 403 must name the credential used and the grant it lacks."""

    def _patch_raising_403(self, *, dedicated: bool, monkeypatch) -> GitHubApiError:
        if dedicated:
            monkeypatch.setenv("OMNI_OCC_PRODUCT_TOKEN", _PRODUCT_TOKEN)
        else:
            monkeypatch.delenv("OMNI_OCC_PRODUCT_TOKEN", raising=False)

        def _boom(method: str, path: str, *, body=None, token=None) -> dict:
            raise GitHubApiError(
                '{"message":"Resource not accessible by integration"}',
                status_code=403,
            )

        emitter = OccCompanionEmitter()
        with (
            patch(f"{_MOD}.rest_json", side_effect=_boom),
            patch(f"{_MOD}._resolve_github_token", return_value=_OCC_TOKEN),
            pytest.raises(GitHubApiError) as excinfo,
        ):
            emitter._patch_evidence_source(
                repo="OmniNode-ai/omnimarket",
                pr_number=1958,
                occ_pr_number=5516,
                tickets=["OMN-15441"],
                existing_body=_EXISTING_BODY,
            )
        return excinfo.value

    def test_fallback_403_names_the_scope_mismatch(self, monkeypatch) -> None:
        message = str(self._patch_raising_403(dedicated=False, monkeypatch=monkeypatch))
        assert "OMNI_OCC_PRODUCT_TOKEN" in message
        assert "onex_change_control" in message
        assert "OmniNode-ai/omnimarket#1958" in message

    def test_dedicated_403_names_the_dedicated_credential(self, monkeypatch) -> None:
        message = str(self._patch_raising_403(dedicated=True, monkeypatch=monkeypatch))
        assert "dedicated OMNI_OCC_PRODUCT_TOKEN credential" in message

    def test_non_403_errors_propagate_untouched(self, monkeypatch) -> None:
        """A 422 is not a scope problem — do not relabel it as one."""
        monkeypatch.setenv("OMNI_OCC_PRODUCT_TOKEN", _PRODUCT_TOKEN)

        def _boom(method: str, path: str, *, body=None, token=None) -> dict:
            raise GitHubApiError("validation failed", status_code=422)

        emitter = OccCompanionEmitter()
        with (
            patch(f"{_MOD}.rest_json", side_effect=_boom),
            patch(f"{_MOD}._resolve_github_token", return_value=_OCC_TOKEN),
            pytest.raises(GitHubApiError, match="validation failed") as excinfo,
        ):
            emitter._patch_evidence_source(
                repo="OmniNode-ai/omnimarket",
                pr_number=1958,
                occ_pr_number=5516,
                tickets=["OMN-15441"],
                existing_body=_EXISTING_BODY,
            )
        assert excinfo.value.status_code == 422
        assert "OMNI_OCC_PRODUCT_TOKEN" not in str(excinfo.value)


# ---------------------------------------------------------------------------
# F3 — the contract seam, on this producer too
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestEmitterProductTokenSeam:
    def test_contract_declares_the_product_token_secret(self) -> None:
        assert (
            contract_secret_ref(_CONTRACT_PATH, "OMNI_OCC_PRODUCT_TOKEN")
            == "OMNI_OCC_PRODUCT_TOKEN"
        )

    def test_a_store_held_token_resolves_with_no_env_var_set(self, monkeypatch) -> None:
        """The ``.201`` lane provisions via the secret store, not process env."""
        monkeypatch.delenv("OMNI_OCC_PRODUCT_TOKEN", raising=False)

        class _StoreOnlySecretStore:
            async def get_secret(self, ref: str) -> str | None:
                return _PRODUCT_TOKEN if ref == "OMNI_OCC_PRODUCT_TOKEN" else None

        with patch(
            "omnimarket.inference.secret_store_resolver._default_secret_store",
            return_value=_StoreOnlySecretStore(),
        ):
            assert _resolve_product_token(_OCC_TOKEN) == (_PRODUCT_TOKEN, True)

    def test_whitespace_only_is_treated_as_absent(self, monkeypatch) -> None:
        monkeypatch.setenv("OMNI_OCC_PRODUCT_TOKEN", "   ")
        assert _resolve_product_token(_OCC_TOKEN) == (_OCC_TOKEN, False)


# ---------------------------------------------------------------------------
# F2 — label POST decodes the ARRAY on this producer too
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestEmitterLabelDecodesArrayResponse:
    def test_label_apply_succeeds_and_logs_no_warning(self, caplog) -> None:
        """RED before the fix: ``rest_json`` raised and the handler warned."""

        def _fake_urlopen(req, timeout=None):  # type: ignore[no-untyped-def]
            return _FakeResponse(_LABELS_ARRAY_RESPONSE)

        with (
            caplog.at_level(logging.WARNING, logger=_MOD),
            patch("urllib.request.urlopen", side_effect=_fake_urlopen),
        ):
            OccCompanionEmitter._apply_machine_minted_label(
                "OmniNode-ai", "onex_change_control", 5516, "tok"
            )

        assert "could not apply" not in caplog.text

    def test_still_swallows_a_real_api_failure(self, caplog) -> None:
        """The best-effort contract is preserved — a 500 must not abort a mint."""

        def _boom(req, timeout=None):  # type: ignore[no-untyped-def]
            raise urllib.error.HTTPError(
                req.full_url,
                500,
                "Server Error",
                {},
                None,  # type: ignore[arg-type]
            )

        with (
            caplog.at_level(logging.WARNING, logger=_MOD),
            patch("urllib.request.urlopen", side_effect=_boom),
        ):
            OccCompanionEmitter._apply_machine_minted_label(
                "OmniNode-ai", "onex_change_control", 5516, "tok"
            )

        assert "could not apply" in caplog.text

    def test_both_producers_route_the_label_post_through_the_same_helper(self) -> None:
        """Pins the documented byte-for-byte parity invariant mechanically.

        The emitter's docstring asserts parity with the effect handler's
        ``_apply_machine_minted_label``. A one-sided fix broke that claim
        silently once already; this makes the next divergence a test failure
        rather than a stale comment.
        """
        import inspect

        from omnimarket.nodes.node_occ_companion_effect.handlers.handler_occ_companion_effect import (
            HandlerOccCompanionEffect,
        )

        emitter_src = inspect.getsource(OccCompanionEmitter._apply_machine_minted_label)
        effect_src = inspect.getsource(
            HandlerOccCompanionEffect._apply_machine_minted_label
        )

        for src, who in ((emitter_src, "emitter"), (effect_src, "effect handler")):
            assert "rest_json_array(" in src, (
                f"{who} must POST the label through rest_json_array — the "
                "endpoint returns a label ARRAY and rest_json rejects it"
            )
            # Guard the exact regression: a bare ``rest_json(`` call.
            assert "\n            rest_json(" not in src, (
                f"{who} still routes the label POST through dict-only rest_json"
            )
