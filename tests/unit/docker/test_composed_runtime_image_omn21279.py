"""OMN-21279: market owns the composed image and its runtime boot assertions."""

from __future__ import annotations

import os
import re
import subprocess
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
TEST_COMMAND = (
    "uv run pytest tests/unit/docker/test_composed_runtime_image_omn21279.py -q"
)
INFRA_TAG = "dev-20261024144528-903e73af2f49"
INFRA_SHA = "903e73af2f495f28270c4d8c33f6f60504dfd0f1"
HEALTH_STEP = "Every health-checked laptop service reports healthy"
NEGATIVE_STEP = (
    "Stopped laptop service fails the health step and restoring it passes "
    "(negative control)"
)


def lock(source: str, version: str = "0.38.77") -> str:
    return (
        '[[package]]\nname = "omnibase-infra"\n'
        f'version = "{version}"\nsource = {{ {source} }}\n'
    )


@pytest.fixture
def dockerfile() -> str:
    return (ROOT / "Dockerfile.runtime").read_text()


@pytest.fixture
def job() -> dict[str, Any]:
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/runtime-boot-composed.yml").read_text()
    )
    assert isinstance(workflow, dict)
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    job = jobs["composed-runtime-boot"]
    assert isinstance(job, dict)
    return job


def step(job: dict[str, Any], name: str) -> dict[str, Any]:
    matches = [s for s in job["steps"] if s.get("name") == name]
    assert len(matches) == 1
    match = matches[0]
    assert isinstance(match, dict)
    return match


def run_pins(
    job: dict[str, Any], tmp_path: Path, lock_text: str
) -> subprocess.CompletedProcess[str]:
    pins = [s for s in job["steps"] if s.get("id") == "pins"]
    assert len(pins) == 1
    run = pins[0]["run"]
    assert isinstance(run, str)
    (tmp_path / "uv.lock").write_text(lock_text, encoding="utf-8")
    return subprocess.run(
        ["bash", "-c", run],
        cwd=tmp_path,
        env={"PATH": os.environ["PATH"], "GITHUB_OUTPUT": str(tmp_path / "out")},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def core_lock() -> str:
    return (
        '[[package]]\nname = "omnibase-core"\nversion = "0.47.39"\n'
        'source = { registry = "https://pypi.org/simple" }\n'
    )


@pytest.mark.unit
def test_pins_check_out_the_commit_a_release_train_tag_locks(
    job: dict[str, Any], tmp_path: Path
) -> None:
    text = core_lock() + lock(
        f'git = "https://github.com/OmniNode-ai/omnibase_infra.git?tag={INFRA_TAG}#{INFRA_SHA}"'
    )
    result = run_pins(job, tmp_path, text)
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"infra_ref={INFRA_SHA}" in (tmp_path / "out").read_text().splitlines()


@pytest.mark.unit
def test_pins_registry_version(job: dict[str, Any], tmp_path: Path) -> None:
    text = core_lock() + lock('registry = "https://pypi.org/simple"', version="0.38.80")
    result = run_pins(job, tmp_path, text)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "infra_ref=v0.38.80" in (tmp_path / "out").read_text().splitlines()


@pytest.mark.unit
@pytest.mark.parametrize(
    "text",
    [
        lock('git = "https://github.com/OmniNode-ai/omnibase_infra.git?branch=main"'),
        lock('path = "../omnibase_infra"'),
        '[[package]]\nname = "other"\nversion = "1.0.0"\n',
        lock('registry = "https://pypi.org/simple"') * 2,
    ],
)
def test_pins_refuse_unpinned_sources(
    job: dict[str, Any], tmp_path: Path, text: str
) -> None:
    result = run_pins(job, tmp_path, core_lock() + text)
    assert result.returncode != 0
    assert "::error::omnibase-infra: " in result.stdout
    assert not (tmp_path / "out").exists()


@pytest.mark.unit
def test_pins_read_the_market_checkout_lock(job: dict[str, Any]) -> None:
    pins = [s for s in job["steps"] if s.get("id") == "pins"]
    assert len(pins) == 1
    assert pins[0]["working-directory"] == "market"


@pytest.mark.unit
def test_dockerfile_inherits_unpinned_argument(dockerfile: str) -> None:
    assert re.search(r"^ARG INFRA_RUNTIME_IMAGE$", dockerfile, re.MULTILINE)
    assert "FROM ${INFRA_RUNTIME_IMAGE}" in dockerfile
    assert not re.search(r"[0-9a-fA-F]{40}|@sha256:", dockerfile)
    assert not re.search(r"^(ENTRYPOINT|CMD|HEALTHCHECK)\b", dockerfile, re.MULTILINE)
    assert dockerfile.rstrip().endswith("USER root")


@pytest.mark.unit
def test_dockerfile_replaces_market_and_strips_entry_points(dockerfile: str) -> None:
    assert (
        "uv pip uninstall --python /app/.venv/bin/python omnimarket || true"
        in dockerfile
    )
    install = dockerfile.index("uv pip install --python /app/.venv/bin/python")
    assert "VIRTUAL_ENV=/app/.venv" in dockerfile
    assert (
        "--no-deps --no-sources --no-cache --reinstall-package omnimarket" in dockerfile
    )
    assert '"omnimarket @ file:///opt/omnimarket"' in dockerfile
    assert (
        dockerfile.index("-m omnibase_infra.runtime.strip_runtime_entry_points")
        > install
    )
    assert "HandlerLlmDelegationCall" in dockerfile
    assert (
        'importlib.import_module("omnimarket.nodes.node_delegate_skill_orchestrator")'
        in dockerfile
    )


@pytest.mark.unit
def test_dockerfile_binds_packaged_configs(dockerfile: str) -> None:
    base = "/app/config/delegation/bifrost_delegation.base.yaml"
    assert f"ENV BIFROST_BASE_CONTRACT_PATH={base}" in dockerfile
    assert (
        "COPY --chown=omniinfra:omniinfra src/omnimarket/configs/bifrost_delegation.yaml "
        + base
    ) in dockerfile
    assert (
        "COPY --chown=omniinfra:omniinfra src/omnimarket/configs/routing_tiers.yaml "
        "/app/config/delegation/routing_tiers.yaml"
    ) in dockerfile
    assert (ROOT / "src/omnimarket/configs/bifrost_delegation.yaml").is_file()
    assert 'Path(os.environ["BIFROST_BASE_CONTRACT_PATH"]).is_file()' in dockerfile


@pytest.mark.unit
def test_dockerfile_copies_every_wheel_force_include(dockerfile: str) -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    wheel = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]
    copied = {
        src
        for line in dockerfile.splitlines()
        if line.startswith("COPY ") and line.split()[-1].startswith("/opt/omnimarket/")
        for src in line.split()[1:-1]
        if not src.startswith("--")
    }
    for source in wheel["force-include"]:
        assert source in copied or (
            f"{source} /opt/omnimarket/{source}" in dockerfile
        ), source


@pytest.mark.unit
def test_workflow_consumers_and_health_controls(job: dict[str, Any]) -> None:
    probe = step(job, "Delegate-skill command topic has a live consumer group")["run"]
    for required in (
        "node_delegate_skill_orchestrator",
        '$3 == "Stable"',
        "seq 1 30",
        "-ge 1",
        "__t.${DELEGATE_COMMAND_TOPIC}",
        "RPK_SASL_MECHANISM=SCRAM-SHA-256",
    ):
        assert required in probe
    topic = step(job, "Read delegate-skill command topic from its contract")["run"]
    assert "node_delegate_skill_orchestrator/contract.yaml" in topic
    assert "DELEGATE_COMMAND_TOPIC=" in topic
    assert "command_topic" in topic
    assert "COMPOSED_BUNDLE" in step(job, HEALTH_STEP)["run"]
    negative = step(job, NEGATIVE_STEP)["run"]
    assert "for svc in " in negative
    assert "omnimarket-projection-llm-cost" in negative
    assert "JOB_NAME='composed-runtime-boot'" in negative
    assert "market/.github/workflows/runtime-boot-composed.yml" in negative
    assert 'sys.stdout.write(hits[0]["run"])' in negative
    step(job, "Both runtime kernels report healthy and running (/health body)")


@pytest.mark.unit
def test_workflow_dev_lane_version_and_crash_loop(job: dict[str, Any]) -> None:
    recreate = step(
        job, "Dev-lane class -- recreate both kernels with that environment"
    )["run"]
    assert 'm.version("omnimarket")' in recreate
    assert "pyproject.toml" in recreate
    assert "assert" in recreate
    assert "for kernel in omninode-runtime runtime-effects" in recreate
    assert "--no-build" in recreate
    assert "COMPOSED_FILES" in recreate
    crash = step(
        job,
        "Dev-lane class -- both kernels stay healthy and running with no restart (crash-loop gate)",
    )["run"]
    assert "+ 900" in crash
    assert "seq 1 8" in crash
    assert "RestartCount" in crash
    assert 'if [ "$restarts" != "0" ]' in crash


@pytest.mark.unit
def test_workflow_checks_out_pinned_infra_and_composes_market(
    job: dict[str, Any],
) -> None:
    checkouts = [
        s for s in job["steps"] if s.get("uses", "").startswith("actions/checkout@")
    ]
    infra = [
        s
        for s in checkouts
        if s.get("with", {}).get("repository") == "OmniNode-ai/omnibase_infra"
    ]
    assert len(infra) == 1
    assert infra[0]["with"]["ref"] == "${{ steps.pins.outputs.infra_ref }}"
    render = step(job, "Render the composed laptop bundle")["run"]
    for service in (
        "omnimarket-projection-delegation",
        "omnimarket-projection-tenant-credentials",
        "omnimarket-projection-llm-cost",
    ):
        assert service in render
    assert 'svc.pop("build", None)' in render
    assert "COMPOSED_FILES=" in render
    assert "COMPOSED_BUNDLE=" in render
    assert "--no-build" in step(job, "Boot the composed laptop profile")["run"]


@pytest.mark.unit
def test_contract_binds_tests_and_boot_job() -> None:
    contract = yaml.safe_load((ROOT / "contracts/OMN-21279.yaml").read_text())
    assert contract["ticket_id"] == "OMN-21279"
    for ac in ("AC1", "AC2"):
        evidence = [e for e in contract["dod_evidence"] if ac in e.get("binds_ac", [])]
        assert any(
            c["check_value"] == TEST_COMMAND for e in evidence for c in e["checks"]
        )
    assert "composed-runtime-boot" in str(contract["dod_evidence"])
