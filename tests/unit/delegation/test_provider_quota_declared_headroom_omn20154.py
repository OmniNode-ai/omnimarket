# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20154 AC3: a declared provider quota defers a call before the limit.

The limit lives in ``configs/provider_quota_limits.v1.yaml``, the call count in
the durable ``provider_quota_state`` projection, and routing withholds a rung
whose declared window is spent. The provider's own refusal stays the backstop.
"""

from __future__ import annotations

import sqlite3
import textwrap
from collections import Counter
from collections.abc import Generator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import NAMESPACE_DNS, UUID, uuid5

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.events.provider_quota import (
    PROVIDER_WIDE_MODEL_SCOPE,
    EnumProviderQuotaOutcome,
    EnumProviderQuotaSource,
    credential_ref_for,
)
from omnimarket.inference import provider_quota_state
from omnimarket.inference.provider_quota_observation import build_quota_observation
from omnimarket.inference.provider_quota_policy import (
    declared_quota_limit_for,
    load_provider_quota_limits,
    load_provider_quota_policy,
)
from omnimarket.inference.provider_quota_state import (
    DECLARED_QUOTA_EXHAUSTED_DISPOSITION,
    ModelProviderQuotaBlock,
    ModelProviderQuotaSnapshot,
    ModelProviderQuotaUsage,
    SqliteProviderQuotaReader,
    StaticProviderQuotaReader,
    quota_block_for_backend,
    read_provider_quota_snapshot,
)
from omnimarket.models.delegation.model_provider_quota_limit import (
    ModelProviderQuotaLimit,
    ModelProviderQuotaLimits,
)
from omnimarket.models.delegation.wire.model_bifrost_delegation_config import (
    EnumQuotaScope,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
    ModelDelegationRequest,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
    handler_delegation_routing as routing,
)

pytestmark = pytest.mark.unit

_NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
_TENANT = UUID("00000000-0000-0000-0000-000000000154")
_GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
_MODEL = "some-model"
_OTHER_MODEL = "other-model"


def _limit(provider_id: str = "google-gemini") -> ModelProviderQuotaLimit:
    declared = declared_quota_limit_for(provider_id)
    assert declared is not None
    return declared


def _usage(
    *,
    model_scope: str = _MODEL,
    calls: int,
    started: datetime = _NOW - timedelta(hours=1),
    window_seconds: int | None = None,
    credential_ref: str = "unauthenticated",
    provider_id: str = "google-gemini",
) -> ModelProviderQuotaUsage:
    return ModelProviderQuotaUsage(
        credential_ref=credential_ref,
        provider_id=provider_id,
        model_scope=model_scope,
        window_seconds=window_seconds or _limit(provider_id).window_seconds,
        window_started_at=started,
        window_calls=calls,
    )


def _snapshot(*usage: ModelProviderQuotaUsage) -> ModelProviderQuotaSnapshot:
    return ModelProviderQuotaSnapshot(
        tenant_id=_TENANT, as_of=_NOW, readable=True, usage=usage
    )


def _block(
    snapshot: ModelProviderQuotaSnapshot, model: str = _MODEL
) -> ModelProviderQuotaBlock | None:
    return quota_block_for_backend(
        snapshot, endpoint_url=_GEMINI_URL, api_key_ref=None, model_name=model
    )


class TestTheDeclarationIsContractData:
    def test_the_packaged_declaration_names_a_policy_provider_with_its_source(
        self,
    ) -> None:
        policy_ids = {p.provider_id for p in load_provider_quota_policy().providers}
        limits = load_provider_quota_limits()
        assert limits, "no provider declares a quota"
        for entry in limits:
            assert entry.provider_id in policy_ids
            assert entry.source.strip()
            assert entry.as_of.year >= 2026

    def test_the_declaration_file_is_the_one_the_loader_reads(self) -> None:
        raw = yaml.safe_load(
            (
                Path(provider_quota_state.__file__).resolve().parents[1]
                / "configs"
                / "provider_quota_limits.v1.yaml"
            ).read_text()
        )
        parsed = ModelProviderQuotaLimits.model_validate(raw)
        assert parsed.limits == load_provider_quota_limits()

    def test_a_provider_declares_one_window(self) -> None:
        entry = {
            "provider_id": "p",
            "window_seconds": 60,
            "limit": 5,
            "unit": "calls",
            "scope": "model",
            "source": "s",
            "as_of": "2026-10-15",
        }
        with pytest.raises(ValidationError, match="declared twice"):
            ModelProviderQuotaLimits.model_validate(
                {"schema_version": "provider_quota_limits.v1", "limits": [entry, entry]}
            )

    @pytest.mark.parametrize(
        "override",
        [
            {"unit": "credits"},
            {"limit": 0},
            {"window_seconds": 0},
            {"source": ""},
            {"as_of": None},
            {"surprise": 1},
        ],
    )
    def test_a_malformed_entry_is_refused(self, override: dict[str, object]) -> None:
        entry: dict[str, object] = {
            "provider_id": "p",
            "window_seconds": 60,
            "limit": 5,
            "unit": "calls",
            "scope": "model",
            "source": "s",
            "as_of": "2026-10-15",
        } | override
        with pytest.raises(ValidationError):
            ModelProviderQuotaLimit.model_validate(entry)


class TestHeadroomIsReadFromTheCallCount:
    def test_a_spent_window_defers_the_call(self) -> None:
        limit = _limit()
        snapshot = _snapshot(_usage(calls=limit.limit))
        block = _block(snapshot)
        assert block is not None
        assert block.disposition == DECLARED_QUOTA_EXHAUSTED_DISPOSITION
        assert block.provider_id == "google-gemini"
        assert block.blocked_until == _NOW - timedelta(hours=1) + timedelta(
            seconds=limit.window_seconds
        )
        assert str(limit.limit) in block.reason

    def test_headroom_left_is_routable(self) -> None:
        assert _block(_snapshot(_usage(calls=_limit().limit - 1))) is None

    def test_no_recorded_usage_is_routable(self) -> None:
        assert _block(_snapshot()) is None

    def test_a_window_that_has_ended_is_routable(self) -> None:
        limit = _limit()
        ended = _NOW - timedelta(seconds=limit.window_seconds)
        assert _block(_snapshot(_usage(calls=limit.limit, started=ended))) is None

    def test_a_count_over_a_different_window_is_not_comparable(self) -> None:
        snapshot = _snapshot(_usage(calls=10_000, window_seconds=60))
        assert _block(snapshot) is None

    def test_a_model_scoped_limit_defers_only_that_model(self) -> None:
        assert _limit().scope is EnumQuotaScope.MODEL
        snapshot = _snapshot(_usage(calls=_limit().limit))
        assert _block(snapshot, _MODEL) is not None
        assert _block(snapshot, _OTHER_MODEL) is None

    def test_the_provider_wide_row_does_not_defer_a_model_scoped_limit(self) -> None:
        snapshot = _snapshot(
            _usage(model_scope=PROVIDER_WIDE_MODEL_SCOPE, calls=_limit().limit)
        )
        assert _block(snapshot) is None

    def test_another_credential_has_its_own_headroom(self) -> None:
        snapshot = _snapshot(_usage(calls=_limit().limit, credential_ref="key-a"))
        assert _block(snapshot) is None
        assert (
            quota_block_for_backend(
                snapshot,
                endpoint_url=_GEMINI_URL,
                api_key_ref="key-a",
                model_name=_MODEL,
            )
            is not None
        )

    def test_a_provider_that_declares_nothing_is_never_deferred(self) -> None:
        snapshot = _snapshot(
            _usage(
                provider_id="openrouter",
                window_seconds=60,
                calls=10_000,
            )
        )
        assert (
            quota_block_for_backend(
                snapshot,
                endpoint_url="https://openrouter.ai/api/v1/chat/completions",
                api_key_ref=None,
                model_name=_MODEL,
            )
            is None
        )

    def test_a_provider_wide_declaration_counts_the_pooled_row(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pooled = _limit().model_copy(update={"scope": EnumQuotaScope.PROVIDER})
        monkeypatch.setattr(
            provider_quota_state, "declared_quota_limit_for", lambda _id: pooled
        )
        snapshot = _snapshot(
            _usage(model_scope=PROVIDER_WIDE_MODEL_SCOPE, calls=pooled.limit)
        )
        assert _block(snapshot, _MODEL) is not None
        assert _block(snapshot, _OTHER_MODEL) is not None
        assert _block(_snapshot(_usage(calls=pooled.limit)), _MODEL) is None

    def test_an_unreadable_snapshot_still_fails_closed(self) -> None:
        unknown = ModelProviderQuotaSnapshot.unknown(as_of=_NOW, reason="db down")
        block = _block(unknown)
        assert block is not None
        assert block.disposition == provider_quota_state.UNKNOWN_QUOTA_STATE_DISPOSITION


class TestTheObservationCountsOverTheDeclaredWindow:
    def _observe(self, endpoint: str) -> int:
        observation = build_quota_observation(
            tenant_id=_TENANT,
            endpoint_url=endpoint,
            api_key_ref=None,
            model_name=_MODEL,
            succeeded=True,
            observed_at=_NOW,
            latency_ms=10,
            source=EnumProviderQuotaSource.RUNTIME_ORCHESTRATOR,
        )
        assert observation is not None
        assert observation.outcome is EnumProviderQuotaOutcome.CALL_OK
        return observation.window_seconds

    def test_a_declaring_provider_counts_over_its_declared_window(self) -> None:
        assert self._observe(_GEMINI_URL) == _limit().window_seconds

    def test_a_provider_with_no_declaration_keeps_the_default_window(self) -> None:
        assert self._observe("https://openrouter.ai/api/v1/chat/completions") == 60


class _UsageReader:
    def __init__(
        self,
        usage: Sequence[ModelProviderQuotaUsage] = (),
        *,
        usage_error: Exception | None = None,
    ) -> None:
        self._usage = tuple(usage)
        self._usage_error = usage_error

    def read_active_blocks(
        self, *, tenant_id: UUID, as_of: datetime
    ) -> Sequence[ModelProviderQuotaBlock]:
        return ()

    def read_window_usage(
        self, *, tenant_id: UUID, as_of: datetime
    ) -> Sequence[ModelProviderQuotaUsage]:
        if self._usage_error is not None:
            raise self._usage_error
        return self._usage


class TestTheSnapshotCarriesTheUsage:
    def test_the_snapshot_reads_usage_beside_blocks(self) -> None:
        usage = _usage(calls=_limit().limit)
        snapshot = read_provider_quota_snapshot(
            _UsageReader([usage]), tenant_id=_TENANT, now=_NOW
        )
        assert snapshot.readable
        assert snapshot.usage == (usage,)
        assert _block(snapshot) is not None

    def test_an_unreadable_usage_fails_the_snapshot_closed(self) -> None:
        snapshot = read_provider_quota_snapshot(
            _UsageReader(usage_error=ConnectionError("usage table down")),
            tenant_id=_TENANT,
            now=_NOW,
        )
        assert snapshot.readable is False
        assert "usage table down" in snapshot.unreadable_reason

    def test_the_static_reader_serves_only_windows_still_open(self) -> None:
        live = _usage(calls=3)
        ended = _usage(
            calls=3, started=_NOW - timedelta(seconds=_limit().window_seconds)
        )
        reader = StaticProviderQuotaReader(usage=(live, ended))
        assert reader.read_window_usage(tenant_id=_TENANT, as_of=_NOW) == (live,)


class TestTheLocalStoreReader:
    def _store(self, path: Path, *, columns: bool) -> None:
        conn = sqlite3.connect(path)
        extra = (
            ", window_seconds INTEGER, window_started_at TEXT, window_calls INTEGER"
            if columns
            else ""
        )
        conn.execute(
            "CREATE TABLE provider_quota_state (tenant_id TEXT, credential_ref TEXT, "
            "provider_id TEXT, model_scope TEXT, disposition TEXT, blocked_until TEXT, "
            f"blocked_indefinitely INTEGER, last_provider_code TEXT, block_reason TEXT{extra})"
        )
        if columns:
            conn.execute(
                "INSERT INTO provider_quota_state (tenant_id, credential_ref, "
                "provider_id, model_scope, blocked_indefinitely, window_seconds, "
                "window_started_at, window_calls) VALUES (?, ?, ?, ?, 0, ?, ?, ?)",
                (
                    str(_TENANT),
                    "unauthenticated",
                    "google-gemini",
                    _MODEL,
                    _limit().window_seconds,
                    (_NOW - timedelta(hours=1)).isoformat(),
                    _limit().limit,
                ),
            )
        conn.commit()
        conn.close()

    def test_a_store_with_the_window_columns_serves_the_count(
        self, tmp_path: Path
    ) -> None:
        self._store(tmp_path / "s.sqlite", columns=True)
        snapshot = read_provider_quota_snapshot(
            SqliteProviderQuotaReader(tmp_path / "s.sqlite"),
            tenant_id=_TENANT,
            now=_NOW,
        )
        assert snapshot.readable
        assert _block(snapshot) is not None

    def test_a_store_without_the_window_columns_holds_no_count(
        self, tmp_path: Path
    ) -> None:
        self._store(tmp_path / "s.sqlite", columns=False)
        snapshot = read_provider_quota_snapshot(
            SqliteProviderQuotaReader(tmp_path / "s.sqlite"),
            tenant_id=_TENANT,
            now=_NOW,
        )
        assert snapshot.readable
        assert snapshot.usage == ()

    def test_an_absent_store_holds_no_count(self, tmp_path: Path) -> None:
        reader = SqliteProviderQuotaReader(tmp_path / "absent.sqlite")
        assert reader.read_window_usage(tenant_id=_TENANT, as_of=_NOW) == ()


def test_the_postgres_usage_read_is_one_tenant_scoped_statement() -> None:
    sql = provider_quota_state.window_usage_sql()
    assert "%(tenant_id)s::uuid" in sql
    assert "%(as_of)s" in sql
    assert "window_calls" in sql
    assert "public.provider_quota_state" in sql
    with pytest.raises(ValueError, match="unsafe relation"):
        provider_quota_state.window_usage_sql("x; DROP TABLE y")


# ---- routing: a provider at zero declared headroom is not selected --------

_TIERS_YAML = textwrap.dedent(
    """\
    tiers:
      - name: local
        cost_per_1k_tokens: 0.0
        cost:
          cost_type: free_local
        models:
          - id: local-model
            backend_id: local-backend
            max_context_tokens: 65536
            use_for: [document]
        eval_before_accept: false
        max_retries: 0
      - name: cheap_frontier
        cost:
          cost_type: free_local
        models:
          - id: entry-model
            backend_id: entry-backend
            max_context_tokens: 65536
            use_for: [document]
        eval_before_accept: false
        max_retries: 0
    """
)

_BIFROST_YAML = textwrap.dedent(
    f"""\
    config_version: "1.0.0"
    schema_version: "bifrost_delegation.v1"
    backends:
      - backend_id: local-backend
        provider: local
        endpoint_url: "http://198.51.100.10:8000/v1/chat/completions"
        model_name: local-model
        tier: local
        capabilities: [document]
      - backend_id: entry-backend
        provider: gemini
        endpoint_url: "{_GEMINI_URL}"
        model_name: entry-model
        tier: frontier_api
        capabilities: [document]
    routing_rules:
      - rule_id: "7770b87c-9dc5-508d-9ee7-d7ac15acdfeb"
        priority: 10
        task_class: document
        task_class_contract_version: "1.0.0"
        backend_policy_version: "1.0.0"
        match_operation_types: [chat_completion]
        match_capabilities: [document]
        backend_ids: [local-backend, entry-backend]
        fallback_policy:
          action: escalate_to_next_tier
          max_retries: 1
          on_exhaust: return_error
        shadow_policy_id: "9f0bcb8c-c33e-5016-a33a-f41a54b04c2b"
    default_backends: [local-backend]
    """
)

_CONTRACT_YAML = textwrap.dedent(
    """\
    task_classes:
      document:
        gateway_exposure: public
        cloud_routing_policy: allowed
        pricing_ceiling_per_1k_tokens: 1.0
        definition_of_done:
          deterministic: [response_non_empty]
        escalation_policy:
          max_escalations: 1
          tier_order: [local, cheap_frontier]
          entry_share: {tier: cheap_frontier, share: 1.0}
    """
)

_KEYS = tuple(uuid5(NAMESPACE_DNS, f"omn-20154-headroom-{i}") for i in range(50))


def _reset() -> None:
    routing._config = None
    routing._config_spread_peers = None
    routing._config_spread_weights = None
    routing._get_task_class_contract.cache_clear()
    routing._load_bifrost_endpoints.cache_clear()


@pytest.fixture
def _bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[None, None, None]:
    for name, contents in (
        ("routing_tiers.yaml", _TIERS_YAML),
        ("bifrost_delegation.yaml", _BIFROST_YAML),
        ("task_class_contracts.v1.yaml", _CONTRACT_YAML),
    ):
        (tmp_path / name).write_text(contents)
    for env, filename in (
        ("DELEGATION_ROUTING_TIERS_PATH", "routing_tiers.yaml"),
        ("BIFROST_CONTRACT_PATH", "bifrost_delegation.yaml"),
        ("BIFROST_OVERLAY_PATH", "no-overlay.yaml"),
        ("TASK_CLASS_CONTRACT_PATH", "task_class_contracts.v1.yaml"),
    ):
        monkeypatch.setenv(env, str(tmp_path / filename))
    _reset()
    yield
    _reset()


def _picks(quota_state: ModelProviderQuotaSnapshot | None) -> Counter[str]:
    return Counter(
        routing.delta(
            ModelDelegationRequest(
                prompt="Summarize: ok.",
                task_type="document",
                correlation_id=key,
                emitted_at=_NOW,
            ),
            quota_state=quota_state,
        ).selected_backend_ref
        for key in _KEYS
    )


@pytest.mark.usefixtures("_bound")
class TestRoutingDefersBeforeTheLimit:
    def _spent(self) -> ModelProviderQuotaSnapshot:
        return _snapshot(_usage(model_scope="entry-model", calls=_limit().limit))

    def test_a_provider_with_headroom_is_still_selected(self) -> None:
        with_headroom = _snapshot(
            _usage(model_scope="entry-model", calls=_limit().limit - 1)
        )
        assert _picks(with_headroom) == Counter({"entry-backend": len(_KEYS)})

    def test_a_provider_at_zero_declared_headroom_is_not_selected(self) -> None:
        assert _picks(self._spent()) == Counter({"local-backend": len(_KEYS)})

    def test_the_withheld_rung_is_named_in_the_blocked_set(self) -> None:
        assert routing.quota_blocked_backend_refs(self._spent()) == frozenset(
            {"entry-backend"}
        )

    def test_the_rung_returns_when_its_window_ends(self) -> None:
        later = self._spent().model_copy(
            update={"as_of": _NOW + timedelta(seconds=_limit().window_seconds)}
        )
        assert _picks(later) == Counter({"entry-backend": len(_KEYS)})


def test_the_credential_ref_helper_keys_an_unreferenced_call_as_unauthenticated() -> (
    None
):
    assert credential_ref_for(None) == "unauthenticated"
