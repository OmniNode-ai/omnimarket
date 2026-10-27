# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Routing decisions come from overlays, never from omnimarket code (OMN-20601).

Ruling 2026-10-10T17:54:30Z. Two of the three checks live here:

* ``inventory``: tests/fixtures/routing_sources/inventory.yaml lists every routing
  selection source in the tree. A path-config or node-overlay resolution site, or a
  packaged config, that is in neither the sources nor the not-routing lists fails.
* ``overlay_precedence``: for each source the deployment's overlay (or pin, or
  environment) wins over the code default, exercised through the production resolver.

The third, ``routing_in_code_refused``, is in
tests/unit/nodes/node_deployment_fact_gate_effect/test_routing_in_code_omn20601.py:
the deployment-fact gate node refuses a new code path that chooses a route, judged
against the base revision.

Run by the ``Routing sources`` step of the ci.yml ``lint`` job and by the
``routing-sources-omn20601`` pre-commit hook.
"""

from __future__ import annotations

import ast
import importlib
import re
import textwrap
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src" / "omnimarket"
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "routing_sources"
INVENTORY_PATH = FIXTURES / "inventory.yaml"

# The helpers every resolution site calls; the module that defines them is the
# mechanism, so its internal calls are not sites.
_RESOLUTION_HELPERS = frozenset(
    {
        "resolve_path_config",
        "resolve_required_path_config",
        "resolve_optional_path_config",
        "find_node_overlay",
        "resolve_overlay_file",
        "load_node_overlay",
        "resolve_setting",
    }
)
_HELPER_MODULE = "src/omnimarket/handlers/node_overlay_reader.py"

# --- tree scans ----------------------------------------------------------------


def _non_test_python(root: Path) -> Iterator[Path]:
    for path in sorted(root.rglob("*.py")):
        parts = path.relative_to(root).parts
        if "tests" in parts or path.name.startswith(("test_", "conftest")):
            continue
        yield path


def _rel(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _call_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _resolution_sites(src_root: Path, repo_root: Path) -> set[str]:
    sites: set[str] = set()
    for path in _non_test_python(src_root):
        rel = _rel(path, repo_root)
        if rel == _HELPER_MODULE:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and _call_name(node) in _RESOLUTION_HELPERS:
                first = ast.unparse(node.args[0]) if node.args else ""
                sites.add(f"{rel}:{_call_name(node)}({first})")
    return sites


def _packaged_configs(inventory: dict[str, Any], repo_root: Path) -> set[str]:
    scope = inventory["scan_scope"]
    found = {
        _rel(p, repo_root)
        for p in (repo_root / scope["configs"]).iterdir()
        if p.is_file() and p.suffix in {".yaml", ".yml", ".json"}
    }
    found.update(scope["extra_files"])
    for directory in scope["extra_dirs"]:
        found.update(
            _rel(p, repo_root)
            for p in (repo_root / directory).iterdir()
            if p.is_file() and p.suffix in {".yaml", ".yml", ".json"}
        )
    return found


def _inventory_gaps(
    inventory: dict[str, Any], sites: set[str], configs: set[str]
) -> dict[str, set[str]]:
    """What the tree has that the inventory lacks, and what the inventory has that the tree lacks."""
    listed_sites = {
        site for source in inventory["sources"] for site in source["resolution_sites"]
    } | {entry["site"] for entry in inventory["not_routing_resolution_sites"]}
    listed_configs = {
        path for source in inventory["sources"] for path in source["packaged_defaults"]
    } | {entry["path"] for entry in inventory["not_routing_configs"]}
    return {
        "sites_missing": sites - listed_sites,
        "sites_stale": listed_sites - sites,
        "configs_missing": configs - listed_configs,
        "configs_stale": listed_configs - configs,
    }


def _load_inventory() -> dict[str, Any]:
    loaded = yaml.safe_load(INVENTORY_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


# --- AC1: the inventory --------------------------------------------------------


def test_routing_sources_inventory_matches_the_tree() -> None:
    inventory = _load_inventory()
    sites = _resolution_sites(SRC_ROOT, REPO_ROOT)
    configs = _packaged_configs(inventory, REPO_ROOT)
    assert sites, "the scan found no resolution site; the scanner is blind"
    assert configs, "the scan found no packaged config; the scanner is blind"
    gaps = _inventory_gaps(inventory, sites, configs)
    assert gaps == {key: set() for key in gaps}, (
        "routing source inventory (tests/fixtures/routing_sources/inventory.yaml) "
        f"disagrees with the tree: {gaps}. List a new resolution site or packaged "
        "config as a routing source or under its not_routing list with a reason; "
        "remove an entry the tree no longer has."
    )


def test_routing_sources_inventory_detects_an_unlisted_source(tmp_path: Path) -> None:
    """Positive control: a resolver call and a config the inventory does not list are reported."""
    inventory = _load_inventory()
    fake_src = tmp_path / "src" / "omnimarket"
    (fake_src / "routing").mkdir(parents=True)
    (fake_src / "routing" / "new_route_source.py").write_text(
        'from x import resolve_path_config\n\nPATH, _ = resolve_path_config("NEW_ROUTE_KEY", None)\n',
        encoding="utf-8",
    )
    sites = _resolution_sites(fake_src, tmp_path)
    assert sites == {
        "src/omnimarket/routing/new_route_source.py:resolve_path_config('NEW_ROUTE_KEY')"
    }
    gaps = _inventory_gaps(
        inventory, sites, {"src/omnimarket/configs/new_routes.v1.yaml"}
    )
    assert gaps["sites_missing"] == sites
    assert gaps["configs_missing"] == {"src/omnimarket/configs/new_routes.v1.yaml"}


def test_routing_sources_inventory_entries_are_well_formed() -> None:
    inventory = _load_inventory()
    ids = [source["id"] for source in inventory["sources"]]
    assert len(ids) == len(set(ids)), f"duplicate source ids in {ids}"
    for source in inventory["sources"]:
        for key in ("selects", "overlay", "resolver", "precedence"):
            assert source.get(key), f"source {source['id']} lacks {key}"
        module_name, _, attribute = source["resolver"].partition(":")
        target: Any = importlib.import_module(module_name)
        for part in attribute.split("."):
            target = getattr(target, part)
        assert callable(target), f"resolver {source['resolver']} is not callable"
        for path in source["packaged_defaults"]:
            assert (REPO_ROOT / path).is_file(), f"{source['id']}: {path} is absent"
    for entry in (
        *inventory["not_routing_resolution_sites"],
        *inventory["not_routing_configs"],
    ):
        assert entry["reason"].strip(), f"not-routing entry without a reason: {entry}"


def test_routing_sources_inventory_is_enforced_by_the_gate() -> None:
    """The named lint step and the hook run the inventory cases, on every path a new source can arrive by."""
    module = "tests/test_routing_sources_omn20601.py"
    ci = yaml.safe_load((REPO_ROOT / ".github/workflows/ci.yml").read_text("utf-8"))
    steps = [
        step["run"]
        for step in ci["jobs"]["lint"]["steps"]
        if module in step.get("run", "")
    ]
    hooks = [
        hook
        for repo in yaml.safe_load(
            (REPO_ROOT / ".pre-commit-config.yaml").read_text("utf-8")
        )["repos"]
        for hook in repo["hooks"]
        if hook["id"] == "routing-sources-omn20601"
    ]
    assert len(steps) == 1, "the lint job runs the inventory cases in one named step"
    assert len(hooks) == 1, "one pre-commit hook runs the inventory cases"
    for command in (steps[0], hooks[0]["entry"]):
        for flag in (" -k ", " -m "):
            assert flag not in command, (
                f"{command!r} deselects cases, so the inventory may not run"
            )
    inventory = _load_inventory()
    scope = inventory["scan_scope"]
    arrivals = [
        "src/omnimarket/routing/new_route_source.py",
        f"{scope['configs']}/new_routes.v1.yaml",
        *scope["extra_files"],
        *(f"{directory}/new_model.yaml" for directory in scope["extra_dirs"]),
        _rel(INVENTORY_PATH, REPO_ROOT),
    ]
    files = re.compile(hooks[0]["files"])
    unwatched = [path for path in arrivals if not files.search(path)]
    assert not unwatched, f"the hook does not run on {unwatched}"


# --- AC2: the overlay wins over every code default -----------------------------

_BIFROST_ONE_TIER = textwrap.dedent(
    """\
    config_version: "2.0.0"
    schema_version: "bifrost_delegation.v1"
    backends:
      - backend_id: local-coder
        provider: local
        endpoint_url: "http://platform-default.test:8000/v1/chat/completions"
        model_name: platform-default-model
        tier: local
        timeout_ms: 30000
        max_tokens: 8192
        capabilities: [code_generation]
    routing_rules:
      - rule_id: "c0ffee00-0011-4000-8000-000000000001"
        priority: 10
        task_class: code_generation
        task_class_contract_version: "1.0.0"
        backend_policy_version: "2.0.0"
        match_operation_types: [chat_completion]
        match_capabilities: [code_generation]
        backend_ids: [local-coder]
        fallback_policy:
          action: escalate_to_next_tier
          max_retries: 1
          on_exhaust: return_error
        shadow_policy_id: "c0ffee00-0012-4000-8000-000000000001"
    default_backends:
      - local-coder
    circuit_breaker:
      failure_threshold: 5
      window_seconds: 30
    failover:
      max_attempts: 3
      backoff_base_ms: 500
    shadow_mode:
      enabled: false
      policy_version: "test"
      log_sample_rate: 1.0
      comparison_logging_enabled: true
      max_shadow_latency_ms: 5.0
    """
)

Probe = Callable[[pytest.MonkeyPatch, Path], None]


def _probe_routing_tiers_ladder(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from omnimarket.handlers.node_overlay_reader import OVERLAY_ROOTS_ENV
    from omnimarket.routing.routing_tiers_path import (
        ROUTING_TIERS_OVERLAY_NODE,
        ROUTING_TIERS_PACKAGED_DEFAULT_PATH,
        ROUTING_TIERS_PATH_ENV_KEY,
        resolve_routing_tiers_path,
    )

    monkeypatch.delenv(ROUTING_TIERS_PATH_ENV_KEY, raising=False)
    monkeypatch.delenv(OVERLAY_ROOTS_ENV, raising=False)
    assert resolve_routing_tiers_path() == ROUTING_TIERS_PACKAGED_DEFAULT_PATH

    overlay = tmp_path / "roots" / ROUTING_TIERS_OVERLAY_NODE / "overlay.yaml"
    overlay.parent.mkdir(parents=True)
    overlay.write_text("tiers: []\n", encoding="utf-8")
    monkeypatch.setenv(OVERLAY_ROOTS_ENV, str(tmp_path / "roots"))
    assert resolve_routing_tiers_path() == overlay

    pinned = tmp_path / "pinned.yaml"
    pinned.write_text("tiers: []\n", encoding="utf-8")
    monkeypatch.setenv(ROUTING_TIERS_PATH_ENV_KEY, str(pinned))
    assert resolve_routing_tiers_path() == pinned


def _probe_harness_routing_overlay(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from omnimarket.models.delegation.model_delegation_routing_overlay import (
        EMPTY_DELEGATION_ROUTING_OVERLAY,
    )
    from omnimarket.routing.routing_tiers_path import (
        DELEGATION_ROUTING_OVERLAY_PATH_ENV_KEY,
        load_delegation_routing_overlay,
        load_harness_tiers,
    )

    monkeypatch.delenv(DELEGATION_ROUTING_OVERLAY_PATH_ENV_KEY, raising=False)
    assert load_delegation_routing_overlay() is EMPTY_DELEGATION_ROUTING_OVERLAY
    assert load_harness_tiers() == ()

    path = tmp_path / "harness_overlay.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "delegation_routing_overlay.v1",
                "harness_backends": [
                    {
                        "backend_id": "harness-probe",
                        "provider": "example-provider",
                        "kind": "harness",
                        "harness": "claude",
                        "surface": "internal",
                        "tenant_scope": "house",
                        "model_name": "example-model",
                        "tier": "harness",
                        "explicit_pin_only": True,
                    }
                ],
                "harness_tiers": [
                    {
                        "name": "harness_probe",
                        "backend_id": "harness-probe",
                        "use_for": ["sample_task"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(DELEGATION_ROUTING_OVERLAY_PATH_ENV_KEY, str(path))
    assert [tier.name for tier in load_harness_tiers()] == ["harness_probe"]


def _probe_bifrost_backend_contract(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from omnimarket.adapters.llm.bifrost.config_loader_bifrost_delegation import (
        load_bifrost_delegation_config,
    )
    from omnimarket.routing import delegation_backend_resolution as resolution

    contract = tmp_path / "bifrost_delegation.yaml"
    contract.write_text(_BIFROST_ONE_TIER, encoding="utf-8")
    overlay = tmp_path / "bifrost_overrides.yaml"
    overlay.write_text(
        "backends:\n"
        "  - backend_id: local-coder\n"
        '    endpoint_url: "http://overlay.test:8000/v1/chat/completions"\n'
        "    model_name: overlay-model\n",
        encoding="utf-8",
    )

    # The overlay's values replace the contract's, field by field.
    default_only = load_bifrost_delegation_config(contract, None)
    assert default_only.backends[0].model_name == "platform-default-model"
    merged = load_bifrost_delegation_config(contract, overlay)
    assert merged.backends[0].model_name == "overlay-model"
    assert merged.backends[0].endpoint_url == (
        "http://overlay.test:8000/v1/chat/completions"
    )

    # The bound overlay is the one read, not the machine-local default.
    monkeypatch.delenv("BIFROST_CONTRACT_PATH", raising=False)
    monkeypatch.setenv("BIFROST_OVERLAY_PATH", str(overlay))
    resolved_contract, resolved_overlay = resolution._resolve_effective_bifrost_paths(
        None, None
    )
    assert resolved_overlay == overlay
    assert resolved_overlay != resolution._OVERLAY_PATH
    assert resolved_contract == resolution._BIFROST_CONFIG_PATH

    # A bound contract never merges the machine-local default overlay.
    monkeypatch.delenv("BIFROST_OVERLAY_PATH", raising=False)
    monkeypatch.setenv("BIFROST_CONTRACT_PATH", str(contract))
    assert resolution._resolve_effective_bifrost_paths(None, None) == (contract, None)


def _probe_task_class_contract(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
        handler_delegation_routing as routing,
    )
    from omnimarket.routing.task_class_contract_path import (
        TASK_CLASS_CONTRACT_PACKAGED_DEFAULT_PATH,
        TASK_CLASS_CONTRACT_PATH_ENV_KEY,
    )

    monkeypatch.delenv(TASK_CLASS_CONTRACT_PATH_ENV_KEY, raising=False)
    routing._get_task_class_contract.cache_clear()
    try:
        packaged = routing._get_task_class_contract()
        assert packaged is not None
        assert packaged["default_task_model_ref"] != "pinned-probe-model"

        pinned = tmp_path / "task_class_contracts.yaml"
        pinned.write_text(
            "default_task_model_ref: pinned-probe-model\n", encoding="utf-8"
        )
        monkeypatch.setenv(TASK_CLASS_CONTRACT_PATH_ENV_KEY, str(pinned))
        routing._get_task_class_contract.cache_clear()
        assert routing._get_task_class_contract() == {
            "default_task_model_ref": "pinned-probe-model"
        }
        assert pinned != TASK_CLASS_CONTRACT_PACKAGED_DEFAULT_PATH
    finally:
        routing._get_task_class_contract.cache_clear()


def _probe_tenant_routing_overlay(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
        ModelDelegationRequest,
    )
    from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
        handler_delegation_routing as routing,
    )
    from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter
    from omnimarket.routing.customer_key_terminus import EnumDelegationSurface
    from omnimarket.routing.tenant_overlay_resolver import (
        TENANT_OVERLAY_TABLE,
        resolve_tenant_overlay,
    )

    contract = tmp_path / "bifrost_delegation.yaml"
    contract.write_text(_BIFROST_ONE_TIER, encoding="utf-8")
    monkeypatch.setenv("BIFROST_CONTRACT_PATH", str(contract))
    monkeypatch.delenv("BIFROST_OVERLAY_PATH", raising=False)
    routing._load_bifrost_endpoints.cache_clear()
    try:
        request = ModelDelegationRequest.model_validate(
            {
                "correlation_id": uuid4(),
                "task_type": "code_generation",
                "prompt": "x" * 100,
                "emitted_at": datetime.now(tz=UTC),
                "tenant_id": "acme-corp",
            }
        )
        platform = routing.delta(request, surface=EnumDelegationSurface.CUSTOMER_LOCAL)
        assert platform.selected_backend_ref == "local-coder"

        db = InmemoryDatabaseAdapter()
        db.upsert(
            TENANT_OVERLAY_TABLE,
            "tenant_id,task_type",
            {
                "tenant_id": "acme-corp",
                "task_type": "code_generation",
                "backend_id": "tenant-own-backend",
                "provider": "fixture-provider",
                "endpoint_url": "https://tenant.example.com/v1/chat/completions",
                "model_name": "tenant-model",
                "secret_ref": "tenant.acme-corp.api_key",
                "timeout_ms": None,
                "max_tokens": None,
            },
        )
        overlay = resolve_tenant_overlay(
            db, tenant_id="acme-corp", task_type="code_generation"
        )
        assert overlay is not None
        chosen = routing.delta(request, tenant_overlay=overlay)
        assert chosen.selected_backend_ref == "tenant-own-backend"
        assert chosen.selected_model == "tenant-model"
    finally:
        routing._load_bifrost_endpoints.cache_clear()


def _probe_build_loop_model_policy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from omnimarket.nodes.node_build_loop_orchestrator.handlers.model_policy_loader import (
        ModelPolicyLoader,
    )

    policy = yaml.safe_load(
        (SRC_ROOT / "model_policy.yaml").read_text(encoding="utf-8")
    )["policies"]["google"]
    assert policy["endpoint_ref"] == "env:LLM_GOOGLE_URL"
    monkeypatch.delenv("LLM_GOOGLE_URL", raising=False)
    with pytest.raises(RuntimeError, match="LLM_GOOGLE_URL"):
        ModelPolicyLoader().resolve("google")
    monkeypatch.setenv("LLM_GOOGLE_URL", "https://deployment.test/v1/")
    assert ModelPolicyLoader().resolve("google") == "https://deployment.test/v1"


def _probe_inference_bridge_endpoints(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from omnimarket.inference import bridge_config_loader as bridge

    local_keys = {key for key, _url, _model in bridge._MODEL_KEY_REGISTRY}
    for _key, url_env, model_env in bridge._MODEL_KEY_REGISTRY:
        monkeypatch.delenv(url_env, raising=False)
        monkeypatch.delenv(model_env, raising=False)
    assert local_keys.isdisjoint(bridge._build_static_model_configs())

    key, url_env, model_env = bridge._MODEL_KEY_REGISTRY[0]
    monkeypatch.setenv(url_env, "https://deployment.test/v1")
    monkeypatch.setenv(model_env, "deployment-model")
    configured = bridge._build_static_model_configs()[key]
    assert configured["base_url"] == "https://deployment.test/v1"
    assert configured["model_id"] == "deployment-model"


def _adr_segmentation_bridge_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from omnimarket.nodes.node_adr_segmentation_llm_effect.handlers import (
        handler_segmentation as segmentation,
    )

    config = tmp_path / "measured-route.yaml"
    config.write_text("segmentation_model_key: measured-route\n", encoding="utf-8")
    monkeypatch.setattr(segmentation, "_SEGMENTATION_CONFIG_ROOT", tmp_path)
    handler = segmentation.HandlerSegmentation.from_contract(
        {"config": {"segmentation_model_key": {"default_ref": config.name}}}
    )
    assert handler._model_key == "measured-route"
    injected = segmentation.HandlerSegmentation(segmentation_model_key="explicit-route")
    assert injected._model_key == "explicit-route"
    config.write_text("segmentation_model_key: ''\n", encoding="utf-8")
    with pytest.raises(ValueError, match="must declare segmentation_model_key"):
        segmentation._segmentation_model_key(config.name)


PROBES: dict[str, Probe] = {
    "adr_segmentation_bridge_key": _adr_segmentation_bridge_key,
    "routing_tiers_ladder": _probe_routing_tiers_ladder,
    "harness_routing_overlay": _probe_harness_routing_overlay,
    "bifrost_backend_contract": _probe_bifrost_backend_contract,
    "task_class_contract": _probe_task_class_contract,
    "tenant_routing_overlay": _probe_tenant_routing_overlay,
    "build_loop_model_policy": _probe_build_loop_model_policy,
    "inference_bridge_endpoints": _probe_inference_bridge_endpoints,
}


def _existing_test_exists(reference: str) -> bool:
    path_text, _, function = reference.partition("::")
    path = REPO_ROOT / path_text
    if not path.is_file():
        return False
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == function
        for node in ast.walk(tree)
    )


def test_overlay_precedence_every_source_is_bound_to_a_proof() -> None:
    inventory = _load_inventory()
    bound_probes: list[str] = []
    for source in inventory["sources"]:
        precedence = source["precedence"]
        assert set(precedence) in ({"probe"}, {"existing_test"}), (
            f"{source['id']}: precedence names exactly one of probe or existing_test"
        )
        if "probe" in precedence:
            assert precedence["probe"] in PROBES, f"{source['id']}: no such probe"
            bound_probes.append(precedence["probe"])
        else:
            assert _existing_test_exists(precedence["existing_test"]), (
                f"{source['id']}: {precedence['existing_test']} does not exist"
            )
    assert sorted(bound_probes) == sorted(PROBES), (
        "every probe in this module is bound to exactly one source"
    )


@pytest.mark.usefixtures("stub_provider_quota_reader")
@pytest.mark.parametrize("source_id", sorted(PROBES))
def test_overlay_precedence_overlay_wins_over_the_code_default(
    source_id: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    PROBES[source_id](monkeypatch, tmp_path)


def test_overlay_precedence_proofs_are_wired_in_the_gate() -> None:
    """The named step and the hook run this module and every existing test the inventory cites."""
    cited = sorted(
        {
            source["precedence"]["existing_test"].partition("::")[0]
            for source in _load_inventory()["sources"]
            if "existing_test" in source["precedence"]
        }
    )
    assert cited, "no existing test is cited; the wiring check would be vacuous"
    cited.append(
        "tests/unit/nodes/node_deployment_fact_gate_effect/test_routing_in_code_omn20601.py"
    )
    ci = yaml.safe_load((REPO_ROOT / ".github/workflows/ci.yml").read_text("utf-8"))
    steps = [
        step
        for step in ci["jobs"]["lint"]["steps"]
        if "tests/test_routing_sources_omn20601.py" in step.get("run", "")
    ]
    assert len(steps) == 1, "the lint job runs this module in exactly one named step"
    hooks = [
        hook
        for repo in yaml.safe_load(
            (REPO_ROOT / ".pre-commit-config.yaml").read_text("utf-8")
        )["repos"]
        for hook in repo["hooks"]
        if hook["id"] == "routing-sources-omn20601"
    ]
    assert len(hooks) == 1, "one pre-commit hook runs this module"
    assert hooks[0]["pass_filenames"] is False
    for command in (steps[0]["run"], hooks[0]["entry"]):
        for path in cited:
            assert path in command, f"{path} is not run by {command!r}"
