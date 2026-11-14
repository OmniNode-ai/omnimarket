# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""GET /v1/workflows/contracts served from the runtime mount.

Behaviour ported from the hosted gateway's route-pinning tests: the discovery
route advertises the servable catalog verbatim (fenced types omitted, sorted),
requires an authenticated caller, and the catalog loader fails closed on a
catalog that could weaken the boundary. Every request goes through the mounted
ASGI app the contract's ``http_mount`` names.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from omnimarket.nodes.node_workflow_contract_catalog_effect.handlers import (
    http_dependencies,
)
from omnimarket.nodes.node_workflow_contract_catalog_effect.handlers.http_app import (
    create_app,
)
from omnimarket.nodes.node_workflow_contract_catalog_effect.handlers.overlay_catalog import (
    OverlayWorkflowContractCatalog,
    WorkflowCatalogError,
)
from tests.nodes.node_workflow_contract_catalog_effect.catalog_fixtures import (
    DELEGATION,
    DOGFOOD_STANDUP_PROMPT_CHARS,
    FENCED,
    TENANT_A_KEY,
    CountingCatalog,
    InlineDocuments,
    SeededIdentity,
    catalog_document,
    entry,
)

pytestmark = pytest.mark.unit

ROUTE = "/v1/workflows/contracts"
CONTRACT = http_dependencies.CONTRACT_PATH


def _catalog(document: dict[str, Any] | None) -> OverlayWorkflowContractCatalog:
    config = http_dependencies.load_http_route()
    documents = {} if document is None else {config.catalog_document: document}
    return OverlayWorkflowContractCatalog(
        documents=InlineDocuments(documents),
        document_name=config.catalog_document,
        command_topics=config.command_topics,
    )


@pytest.fixture
def identity() -> SeededIdentity:
    return SeededIdentity()


@pytest.fixture
def client(identity: SeededIdentity) -> TestClient:
    return TestClient(
        create_app(identity=identity, catalog=_catalog(catalog_document()))
    )


def _get(client: TestClient, key: str | None = TENANT_A_KEY) -> Any:
    return client.get(ROUTE, headers={} if key is None else {"x-api-key": key})


class TestContractDeclaresTheMount:
    def test_mount_prefix_is_the_route_path_and_never_a_parent(self) -> None:
        contract = yaml.safe_load(CONTRACT.read_text())
        assert contract["http_mount"]["path_prefix"] == ROUTE
        assert contract["http_route"]["path"] == ROUTE
        assert contract["http_route"]["methods"] == ["GET"]
        assert contract["http_mount"]["factory"].endswith("http_app:create_app")

    def test_contract_names_the_identity_protocol_and_no_environment_reads(
        self,
    ) -> None:
        contract = yaml.safe_load(CONTRACT.read_text())
        deps = contract["handler_dependencies"]
        assert (
            "omnimarket.protocols.protocol_http_tenant_identity.ProtocolHttpTenantIdentity"
            in deps
        )
        assert "environment" not in contract["http_route"]


class TestContractDiscovery:
    """A client can read the limits it is held to, before it submits."""

    def test_discovery_advertises_the_servable_contract_schema(
        self, client: TestClient
    ) -> None:
        resp = _get(client)
        assert resp.status_code == 200, resp.text
        contracts = {c["workflow_type"]: c for c in resp.json()["contracts"]}
        assert DELEGATION in contracts
        assert (
            contracts[DELEGATION]["payload_schema"]
            == catalog_document()["workflows"][0]["payload_schema"]
        )
        assert (
            contracts[DELEGATION]["contract_id"] == "node_delegation_orchestrator:0.6.0"
        )

    def test_entry_shape_is_exactly_type_contract_id_and_schema(
        self, client: TestClient
    ) -> None:
        body = _get(client).json()
        assert set(body) == {"contracts"}
        for item in body["contracts"]:
            assert set(item) == {"workflow_type", "contract_id", "payload_schema"}

    def test_discovery_omits_fenced_types(self, client: TestClient) -> None:
        advertised = {c["workflow_type"] for c in _get(client).json()["contracts"]}
        assert advertised == {DELEGATION, "alpha-flow"}
        assert FENCED not in advertised

    def test_discovery_is_sorted_by_workflow_type(self, client: TestClient) -> None:
        names = [c["workflow_type"] for c in _get(client).json()["contracts"]]
        assert names == sorted(names)

    def test_ceiling_admits_the_measured_standup_and_fields_agree(
        self, client: TestClient
    ) -> None:
        contracts = {c["workflow_type"]: c for c in _get(client).json()["contracts"]}
        props = contracts[DELEGATION]["payload_schema"]["properties"]
        assert props["prompt"]["maxLength"] >= DOGFOOD_STANDUP_PROMPT_CHARS
        assert props["prompt"]["maxLength"] == props["context_pack"]["maxLength"]

    def test_only_get_is_served(self, client: TestClient) -> None:
        assert (
            client.post(ROUTE, headers={"x-api-key": TENANT_A_KEY}).status_code == 405
        )

    def test_docs_surfaces_are_not_served(self, client: TestClient) -> None:
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert client.get(path).status_code == 404


class TestAuthenticationRequired:
    """The allowlist is tenant-facing, not public: no anonymous surface."""

    def test_no_credential_is_401_absent(self, client: TestClient) -> None:
        resp = _get(client, None)
        assert resp.status_code == 401
        assert resp.json() == {
            "detail": "Unauthorized",
            "code": "auth.credential.absent",
        }

    def test_unresolvable_credential_is_401_invalid(self, client: TestClient) -> None:
        resp = _get(client, "not-a-key")
        assert resp.status_code == 401
        assert resp.json() == {
            "detail": "Unauthorized",
            "code": "auth.credential.invalid",
        }

    def test_empty_credential_header_counts_as_absent(self, client: TestClient) -> None:
        resp = _get(client, "")
        assert resp.status_code == 401
        assert resp.json()["code"] == "auth.credential.absent"

    def test_bearer_credential_that_does_not_resolve_is_invalid(
        self, client: TestClient
    ) -> None:
        resp = client.get(ROUTE, headers={"Authorization": "Bearer x.y.z"})
        assert resp.status_code == 401
        assert resp.json()["code"] == "auth.credential.invalid"


class TestFailsClosedBeforeTheCatalogIsRead:
    """AC2: refuse an unresolved caller, and an unbound resolver, with no catalog read."""

    def test_unauthenticated_request_reads_no_catalog(
        self, identity: SeededIdentity
    ) -> None:
        catalog = CountingCatalog(())
        client = TestClient(create_app(identity=identity, catalog=catalog))
        assert _get(client, None).status_code == 401
        assert _get(client, "bad").status_code == 401
        assert catalog.reads == 0
        assert _get(client).status_code == 200
        assert catalog.reads == 1

    def test_unbound_resolver_refuses_every_request_with_no_catalog_read(self) -> None:
        catalog = CountingCatalog(())
        client = TestClient(create_app(identity=None, catalog=catalog))
        for key in (None, "bad", TENANT_A_KEY):
            resp = _get(client, key)
            assert resp.status_code == 503, resp.text
            assert resp.json() == {"detail": "OIDC authentication is not configured"}
        assert catalog.reads == 0

    def test_production_factory_binds_no_resolver_and_refuses(self) -> None:
        http_dependencies.bind_tenant_identity(None)
        client = TestClient(create_app(catalog=CountingCatalog(())))
        assert _get(client).status_code == 503

    def test_a_bound_resolver_is_used_by_the_production_factory(self) -> None:
        http_dependencies.bind_tenant_identity(SeededIdentity())
        try:
            client = TestClient(create_app(catalog=CountingCatalog(())))
            assert _get(client).status_code == 200
        finally:
            http_dependencies.bind_tenant_identity(None)

    def test_unavailable_catalog_is_503_never_an_empty_200(
        self, identity: SeededIdentity
    ) -> None:
        for document in (None, {"workflows": []}):
            client = TestClient(
                create_app(identity=identity, catalog=_catalog(document))
            )
            resp = _get(client)
            assert resp.status_code == 503, resp.text
            assert "contracts" not in resp.json()


class TestCatalogFailsClosedOnLoad:
    """The catalog loader refuses any catalog that could weaken the boundary."""

    async def _load(self, document: dict[str, Any] | None) -> Any:
        return await _catalog(document).servable_contracts()

    @pytest.mark.parametrize(
        "document",
        [None, {}, {"workflows": []}, {"other": 1}, {"workflows": [1]}],
    )
    async def test_missing_empty_or_malformed_catalog_raises(
        self, document: dict[str, Any] | None
    ) -> None:
        with pytest.raises(WorkflowCatalogError):
            await self._load(document)

    async def test_unknown_topic_constant_raises(self) -> None:
        with pytest.raises(WorkflowCatalogError):
            await self._load(
                {"workflows": [entry("a-flow", constant="NOT_A_CONSTANT")]}
            )

    async def test_duplicate_workflow_type_raises(self) -> None:
        with pytest.raises(WorkflowCatalogError):
            await self._load({"workflows": [entry("a-flow"), entry("a-flow")]})

    async def test_duplicate_across_fence_boundary_still_raises(self) -> None:
        with pytest.raises(WorkflowCatalogError):
            await self._load(
                {"workflows": [entry("a-flow"), entry("a-flow", enabled=False)]}
            )

    async def test_all_entries_fenced_raises(self) -> None:
        with pytest.raises(WorkflowCatalogError):
            await self._load({"workflows": [entry("a-flow", enabled=False)]})

    async def test_fenced_entry_is_still_validated(self) -> None:
        with pytest.raises(WorkflowCatalogError):
            await self._load(
                {
                    "workflows": [
                        entry("a-flow"),
                        entry("b-flow", constant="NOT_A_CONSTANT", enabled=False),
                    ]
                }
            )

    async def test_enabled_defaults_true(self) -> None:
        contracts = await self._load({"workflows": [entry("a-flow")]})
        assert [c.workflow_type for c in contracts] == ["a-flow"]

    @pytest.mark.parametrize(
        "mutate",
        [
            lambda e: e.update(event_type="delegation-request"),
            lambda e: e.update(workflow_type="Bad_Type"),
            lambda e: e.update(workflow_type="x; drop table"),
            lambda e: e.update(contract_id="bare_name"),
            lambda e: e.update(command_topic_constant="lower_case"),
            lambda e: e.update(unexpected="field"),
            lambda e: e["payload_schema"].update(additionalProperties=True),
            lambda e: e["payload_schema"].pop("additionalProperties"),
            lambda e: e["payload_schema"].update(type="array"),
            lambda e: e["payload_schema"].update(properties={}),
        ],
    )
    async def test_malformed_entry_raises(self, mutate: Any) -> None:
        bad = entry("a-flow")
        mutate(bad)
        with pytest.raises(WorkflowCatalogError):
            await self._load({"workflows": [bad]})

    async def test_non_command_topic_binding_raises(self) -> None:
        config = http_dependencies.load_http_route()
        topics = dict(config.command_topics) | {
            "DELEGATION_REQUEST_TOPIC": "onex.evt.omnibase-infra.delegation-completed.v1"
        }
        catalog = OverlayWorkflowContractCatalog(
            documents=InlineDocuments({config.catalog_document: catalog_document()}),
            document_name=config.catalog_document,
            command_topics=topics,
        )
        with pytest.raises(WorkflowCatalogError):
            await catalog.servable_contracts()

    async def test_non_canonical_topic_binding_raises(self) -> None:
        config = http_dependencies.load_http_route()
        topics = dict(config.command_topics) | {"DELEGATION_REQUEST_TOPIC": "free.form"}
        catalog = OverlayWorkflowContractCatalog(
            documents=InlineDocuments({config.catalog_document: catalog_document()}),
            document_name=config.catalog_document,
            command_topics=topics,
        )
        with pytest.raises(WorkflowCatalogError):
            await catalog.servable_contracts()

    async def test_schema_is_returned_verbatim(self) -> None:
        document = catalog_document()
        contracts = await self._load(copy.deepcopy(document))
        by_type = {c.workflow_type: c for c in contracts}
        assert (
            by_type[DELEGATION].payload_schema
            == document["workflows"][0]["payload_schema"]
        )


class TestContractTopicsAreDeclaredInTheContract:
    def test_command_topics_are_all_canonical_commands(self) -> None:
        config = http_dependencies.load_http_route()
        assert config.command_topics
        assert all(
            topic.startswith("onex.cmd.") for topic in config.command_topics.values()
        )
