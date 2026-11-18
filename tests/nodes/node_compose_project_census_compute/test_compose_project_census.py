# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for the compose project census node."""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import get_type_hints

import pytest
import yaml
from pydantic import BaseModel, ValidationError

pytestmark = pytest.mark.unit

PACKAGE = "omnimarket.nodes.node_compose_project_census_compute"
MODELS = PACKAGE + ".models.model_compose_project_census"
HANDLER = PACKAGE + ".handlers.handler_compose_project_census"


def _models():
    return importlib.import_module(MODELS)


def _handler():
    return importlib.import_module(HANDLER).HandlerComposeProjectCensus()


def _policy():
    m = _models()
    return m.ModelComposeCensusPolicy(
        worktree_segments=("worktrees", "scratch"),
        hosts=(
            m.ModelComposeCensusHost(
                name="host-a", aliases=("node-a",), clone_roots=("/srv/clones",)
            ),
        ),
        projects=(
            m.ModelComposeCensusProject(
                project="web",
                hosts=("host-a",),
                compose_file="web/docker-compose.yml",
                images=(
                    m.ModelComposeCensusImage(
                        service="web", image="registry.example.com/web:1.0.0"
                    ),
                ),
            ),
        ),
        standalone=(
            m.ModelComposeCensusStandalone(
                hosts=("host-a",),
                name_prefix="standalone-",
                declared_by="standalone-policy",
            ),
        ),
    )


def _container(
    name="web-1",
    project="web",
    image="registry.example.com/web:1.0.0",
    working_dir="/srv/clones/web",
    service="web",
    config_files="/srv/clones/web/docker-compose.yml",
):
    m = _models()
    labels = {
        "com.docker.compose.project": project,
        "com.docker.compose.project.working_dir": working_dir,
        "com.docker.compose.service": service,
        "com.docker.compose.project.config_files": config_files,
    }
    return m.ModelComposeCensusContainer(
        name=name, state="running", image=image, labels=labels
    )


def _request(containers, host="host-a", absent_paths=()):
    m = _models()
    return m.ModelComposeCensusRequest(
        host=host,
        containers=tuple(containers),
        absent_paths=tuple(absent_paths),
        policy=_policy(),
    )


def test_golden_clean() -> None:
    verdict = _handler().handle(_request([_container()]))
    assert verdict.clean is True
    assert verdict.findings == ()


def test_compose_dir_outside_clone_worktree() -> None:
    c = _container(working_dir="/srv/clones/worktrees/web")
    verdict = _handler().handle(_request([c]))
    assert any(f.kind.value == "compose_dir_outside_clone" for f in verdict.findings)


def test_compose_dir_outside_clone_scratch() -> None:
    c = _container(working_dir="/srv/scratch/web")
    verdict = _handler().handle(_request([c]))
    assert any(f.kind.value == "compose_dir_outside_clone" for f in verdict.findings)


def test_path_prefix_trap() -> None:
    c = _container(working_dir="/srv/clones-extra/web")
    verdict = _handler().handle(_request([c]))
    assert any(f.kind.value == "compose_dir_outside_clone" for f in verdict.findings)


def test_compose_file_missing() -> None:
    c = _container()
    verdict = _handler().handle(_request([c], absent_paths=("/srv/clones/web",)))
    assert any(f.kind.value == "compose_file_missing" for f in verdict.findings)


def test_project_undeclared() -> None:
    c = _container(project="ghost")
    verdict = _handler().handle(_request([c]))
    assert any(f.kind.value == "project_undeclared" for f in verdict.findings)


def test_compose_file_differs() -> None:
    c = _container(config_files="/srv/clones/web/other.yml")
    verdict = _handler().handle(_request([c]))
    assert any(f.kind.value == "compose_file_differs" for f in verdict.findings)


def test_compose_file_differs_second_match() -> None:
    c = _container(
        config_files="/srv/clones/web/other.yml,/srv/clones/web/docker-compose.yml"
    )
    verdict = _handler().handle(_request([c]))
    assert not any(f.kind.value == "compose_file_differs" for f in verdict.findings)


def test_version_mismatch() -> None:
    c = _container(image="registry.example.com/web:2.0.0")
    verdict = _handler().handle(_request([c]))
    assert any(f.kind.value == "version_mismatch" for f in verdict.findings)


def test_container_undeclared() -> None:
    m = _models()
    c = m.ModelComposeCensusContainer(
        name="mystery", state="running", image="img", labels={}
    )
    verdict = _handler().handle(_request([c]))
    assert any(f.kind.value == "container_undeclared" for f in verdict.findings)


def test_standalone_prefix_no_finding() -> None:
    m = _models()
    c = m.ModelComposeCensusContainer(
        name="standalone-pg", state="running", image="pg", labels={}
    )
    verdict = _handler().handle(_request([c]))
    assert not any(f.kind.value == "container_undeclared" for f in verdict.findings)


def test_unknown_host_raises() -> None:
    m = _models()
    req = _request([_container()], host="ghost-host")
    with pytest.raises(m.ComposeCensusHostUndeclaredError):
        _handler().handle(req)


def test_policy_rejects_undeclared_host_in_project() -> None:
    m = _models()
    with pytest.raises(ValidationError):
        m.ModelComposeCensusPolicy(
            worktree_segments=("worktrees",),
            hosts=(
                m.ModelComposeCensusHost(
                    name="host-a", aliases=("node-a",), clone_roots=("/srv/clones",)
                ),
            ),
            projects=(
                m.ModelComposeCensusProject(
                    project="web", hosts=("ghost",), compose_file="f.yml"
                ),
            ),
        )


def test_from_envelope_row_missing_labels() -> None:
    m = _models()
    with pytest.raises(ValueError, match="Labels"):
        m.ModelComposeCensusContainer.from_envelope_row({"Names": "x", "Image": "y"})


def test_policy_rejects_extra_keys() -> None:
    m = _models()
    with pytest.raises(ValidationError):
        m.ModelComposeCensusPolicy(
            worktree_segments=("worktrees",),
            hosts=(
                m.ModelComposeCensusHost(
                    name="host-a", aliases=("node-a",), clone_roots=("/srv/clones",)
                ),
            ),
            projects=(),
            standalone=(),
            bogus=1,
        )


def test_determinism() -> None:
    c1 = _container(name="a")
    c2 = _container(name="b", project="ghost")
    v1 = _handler().handle(_request([c1, c2]))
    v2 = _handler().handle(_request([c2, c1]))
    assert v1.findings == v2.findings


def test_contract_golden_chain() -> None:
    package = importlib.import_module(PACKAGE)
    assert package.__file__ is not None
    root = Path(package.__file__).parent
    contract = yaml.safe_load((root / "contract.yaml").read_text())
    metadata = yaml.safe_load((root / "metadata.yaml").read_text())
    assert contract["name"] == "node_compose_project_census_compute"
    assert contract["metadata"]["related_tickets"] == ["OMN-19998"]
    assert contract["event_bus"] == {"subscribe_topics": [], "publish_topics": []}
    assert metadata["name"] == "node_compose_project_census_compute"
    binding = contract["handler"]
    handler_class = getattr(
        importlib.import_module(binding["module"]), binding["class"]
    )

    def model(ref: str) -> type[BaseModel]:
        module, _, name = ref.rpartition(".")
        result = getattr(importlib.import_module(module), name)
        assert issubclass(result, BaseModel)
        assert result.model_config == {"frozen": True, "extra": "forbid"}
        return result

    request_class = model(binding["input_model"])
    result_class = model(binding["output_model"])
    hints = get_type_hints(handler_class.handle)
    assert hints["request"] is request_class
    assert hints["return"] is result_class


# --- placement under a declared deploy root, home expansion, mixed trees ---------------------------------


def _policy_with(**extra):
    m = _models()
    base = _policy()
    return m.ModelComposeCensusPolicy(
        worktree_segments=base.worktree_segments,
        hosts=(
            m.ModelComposeCensusHost(
                name="host-a",
                aliases=("node-a",),
                clone_roots=("/srv/clones", "~/clones"),
            ),
        ),
        projects=base.projects,
        standalone=base.standalone,
        **extra,
    )


def _request_with(containers, policy, home_dir="", host="host-a"):
    m = _models()
    return m.ModelComposeCensusRequest(
        host=host, containers=tuple(containers), policy=policy, home_dir=home_dir
    )


def test_deployed_root_counts_as_a_clone() -> None:
    c = _container(
        working_dir="/srv/releases/1.2.3/docker",
        config_files="/srv/clones/web/docker-compose.yml",
    )
    without = _handler().handle(_request_with([c], _policy_with()))
    with_root = _handler().handle(
        _request_with([c], _policy_with(deployed_roots=("/srv/releases",)))
    )
    assert {f.kind.value for f in without.findings} >= {"compose_dir_outside_clone"}
    assert "compose_dir_outside_clone" not in {f.kind.value for f in with_root.findings}


def test_installer_tree_is_not_compared_to_the_clone_path() -> None:
    c = _container(
        working_dir="/srv/releases/1.2.3/docker",
        config_files="/srv/releases/1.2.3/docker/docker-compose.yml",
    )
    deployed = _handler().handle(
        _request_with([c], _policy_with(deployed_roots=("/srv/releases",)))
    )
    elsewhere = _handler().handle(_request_with([c], _policy_with()))
    assert deployed.clean is True
    assert "compose_file_differs" in {f.kind.value for f in elsewhere.findings}


def test_transient_container_is_not_undeclared_and_a_bad_pattern_is_refused() -> None:
    m = _models()
    bare = m.ModelComposeCensusContainer(
        name="p21-c13-1", state="running", image="x", labels={}
    )
    named = _handler().handle(
        _request_with(
            [bare], _policy_with(transient_name_patterns=(r"p[0-9]+-c[0-9]+-.*",))
        )
    )
    unnamed = _handler().handle(_request_with([bare], _policy_with()))
    assert named.clean is True
    assert [f.kind.value for f in unnamed.findings] == ["container_undeclared"]
    with pytest.raises(ValidationError, match="regular expression"):
        _policy_with(transient_name_patterns=("(",))


def test_tilde_root_expands_with_the_hosts_home() -> None:
    c = _container(
        working_dir="/srv/u/clones/web",
        config_files="/srv/u/clones/web/docker-compose.yml",
    )
    inside = _handler().handle(_request_with([c], _policy_with(), home_dir="/srv/u"))
    unresolved = _handler().handle(_request_with([c], _policy_with(), home_dir=""))
    assert inside.clean is True
    assert [f.kind.value for f in unresolved.findings] == [
        "compose_dir_outside_clone",
        "compose_file_differs",
    ]


def test_project_run_from_two_trees_names_the_stragglers() -> None:
    old = "/srv/clones/web-old"
    containers = [
        _container(name="web-1"),
        _container(name="web-2"),
        _container(name="web-3", working_dir=old, config_files=old + "/c.yml"),
    ]
    verdict = _handler().handle(_request_with(containers, _policy_with()))
    mixed = [
        f for f in verdict.findings if f.kind.value == "project_mixed_working_dirs"
    ]
    assert [f.container for f in mixed] == ["web-3"]
    assert old in mixed[0].detail


def test_mixed_trees_tie_goes_to_the_greatest_path_in_any_order() -> None:
    a = _container(name="a", working_dir="/srv/clones/a")
    b = _container(name="b", working_dir="/srv/clones/b")
    one = _handler().handle(_request_with([a, b], _policy_with()))
    two = _handler().handle(_request_with([b, a], _policy_with()))
    assert one == two
    assert [
        f.container
        for f in one.findings
        if f.kind.value == "project_mixed_working_dirs"
    ] == ["a"]


def test_every_finding_kind_has_a_test_above() -> None:
    kinds = {k.value for k in _models().EnumComposeCensusFinding}
    assert kinds == {
        "compose_dir_outside_clone",
        "compose_file_missing",
        "compose_file_differs",
        "project_undeclared",
        "version_mismatch",
        "project_mixed_working_dirs",
        "container_undeclared",
    }


# --- seam: the rows the lane census collector writes (lane_census_inventory envelope) --------------------
# The fixtures are rows taken from live hosts on 2026-11-13 in the collector's envelope shape (Names, State,
# Status, Image, Labels as a mapping). The collector lives in another repository, so its shape is pinned here
# by fixture: if the collector renames a key, from_envelope_row refuses the row; if this node renames what it
# reads, the golden findings below change.

FIXTURES = Path(__file__).parent / "fixtures"


def _envelope(name: str) -> list[dict]:
    import json

    return json.loads((FIXTURES / name).read_text())


def _live_policy():
    m = _models()
    return m.ModelComposeCensusPolicy(
        worktree_segments=("worktrees", "scratch"),
        deployed_roots=("~/.omnibase/infra/deployed",),
        hosts=(
            m.ModelComposeCensusHost(
                name="h051",
                aliases=("node-a",),
                clone_roots=("/srv/workspace",),
            ),
            m.ModelComposeCensusHost(
                name="h201",
                aliases=("node-b",),
                clone_roots=("/srv/workspace",),
            ),
        ),
        projects=(
            m.ModelComposeCensusProject(
                project="stack-infra-canary",
                hosts=("h201",),
                compose_file="infra_repo/docker/docker-compose.yml",
            ),
        ),
        standalone=(
            m.ModelComposeCensusStandalone(
                hosts=("h051",),
                name_prefix="buildx_buildkit_",
                declared_by="docker buildx lane builder",
            ),
        ),
    )


def test_seam_h051_envelope_phoenix_and_second_clone_are_named() -> None:
    m = _models()
    rows = [
        m.ModelComposeCensusContainer.from_envelope_row(r)
        for r in _envelope("census_envelope_h051.json")
    ]
    verdict = _handler().handle(
        m.ModelComposeCensusRequest(
            host="h051",
            containers=tuple(rows),
            policy=_live_policy(),
            home_dir="/srv/user",
            absent_paths=(
                "/srv/workspace/worktrees/OMN-21685-move-to-51-1240/infra_repo/docker",
            ),
        )
    )
    by_container: dict[str, set[str]] = {}
    for f in verdict.findings:
        by_container.setdefault(f.container, set()).add(f.kind.value)
    assert by_container["stack-infra-phoenix"] == {
        "compose_dir_outside_clone",
        "compose_file_missing",
        "project_undeclared",
    }
    assert by_container["lab-factory-floor-web"] == {
        "compose_dir_outside_clone",
        "project_undeclared",
    }
    # buildkit containers carry no compose label and the standalone prefix declares them.
    assert not any(n.startswith("buildx_buildkit_") for n in by_container)
    assert verdict.clean is False


def test_seam_h201_stability_envelope_names_the_two_old_tree_containers() -> None:
    m = _models()
    rows = [
        m.ModelComposeCensusContainer.from_envelope_row(r)
        for r in _envelope("census_envelope_h201_stability.json")
    ]
    verdict = _handler().handle(
        m.ModelComposeCensusRequest(
            host="h201",
            containers=tuple(rows),
            policy=_live_policy(),
            home_dir="/srv/user",
        )
    )
    mixed = sorted(
        f.container
        for f in verdict.findings
        if f.kind.value == "project_mixed_working_dirs"
    )
    assert mixed == [
        "stack-infra-canary-keycloak",
        "stack-infra-canary-valkey",
    ]


def test_seam_envelope_row_shape_is_refused_when_the_collector_changes_a_key() -> None:
    row = dict(_envelope("census_envelope_h051.json")[0])
    row["Name"] = row.pop("Names")
    with pytest.raises(ValueError, match="Names"):
        _models().ModelComposeCensusContainer.from_envelope_row(row)


def test_example_overlay_loads_through_the_policy_model() -> None:
    node_dir = Path(importlib.import_module(PACKAGE).__file__).parent
    document = yaml.safe_load((node_dir / "overlay.example.yaml").read_text())
    assert _models().ModelComposeCensusPolicy.model_validate(document).hosts
