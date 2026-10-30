# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-17232 -- the host table is the one source placement reads for what a host may take.

On 2026-10-29 a host marked ``available: false`` ran seven or more units over the bus because
placement read only the capacity advertisements; a suite ended 128 failed there, and a census found
the one repository whose unit suite dies of the missing instruction set (modelled below as
native_audio). These tests pin the three behaviours the operator asked for: an unavailable host
is refused on the bus path, an AVX2 unit is refused on a no-AVX2 host and placed on an AVX2 host,
and a light unit is placed on the small laptop under its own lower memory bar.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

from omnimarket.lab_work import cli
from omnimarket.lab_work.bus import LabWorkCaller, LabWorkHost
from omnimarket.models.overlay_keys import (
    ModelLabHostsTable,
    ModelUnitNeedsRule,
)
from omnimarket.nodes.node_lab_placement_compute.handlers import (
    HandlerLabPlacement,
    HandlerLabPoolPlace,
    HostTableError,
    load_host_table,
    load_placement_rules,
)
from omnimarket.nodes.node_lab_placement_compute.models import (
    DEFAULT_MIN_FREE_BYTES,
    EnumPlacementDecision,
    EnumRefusalReason,
    ModelLabPoolPlaceRequest,
    ModelPlacement,
    ModelPlacementRules,
    ModelUnitProfile,
)
from omnimarket.nodes.node_lab_work_unit_effect import (
    HandlerHostCapacityAdvertiseEffect,
    HandlerLabWorkUnitEffect,
    ModelHostCapacityAdvertisement,
    ModelLabWorkUnitReceipt,
    ModelLabWorkUnitRequest,
)
from omnimarket.nodes.node_lab_work_unit_effect.protocols import (
    HostReading,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 10, 29, 18, 0, 0, tzinfo=UTC)
GB = 1024**3

#: The lab host table of 2026-10-29 in the shape the real one has: h051 (80 threads, no AVX2,
#: registered unavailable), h106 (7.8 GB, light jobs only, its own 4 GB bar, 128 s against h101's
#: 55 s for one suite) and two ordinary hosts.
TABLE_YAML = """
hosts:
  - name: h101
    target: lab-h101
  - name: h201
    target: lab-h201
    penalty: 0.25
  - name: h051
    target: lab-h051
    avx2: false
    available: false
    unavailable_reason: "h051 has no AVX2 (Ivy Bridge) and is registered for the offload plan"
  - name: h051-live
    target: lab-h051-live
    avx2: false
  - name: h106
    target: lab-h106
    min_free_gb: 4.0
    job_kinds: [lint, test]
    max_weight: light
    speed: 0.43
unit_needs:
  - repo: native_audio
    needs: [avx2]
    reason: "its unit suite imports a wheel (native_audio_ext) that dies with exit 132 on a CPU without AVX2"
"""


@pytest.fixture(scope="module")
def table() -> ModelLabHostsTable:
    return ModelLabHostsTable.model_validate(yaml.safe_load(TABLE_YAML))


@pytest.fixture(scope="module")
def rules() -> ModelPlacementRules:
    return load_placement_rules()


def _ad(
    host: str,
    load1: float,
    cores: int,
    *,
    free_gb: float = 32,
    max_units: int = 4,
) -> ModelHostCapacityAdvertisement:
    return ModelHostCapacityAdvertisement(
        host_name=host,
        cores=cores,
        load1=load1,
        mem_available_bytes=int(free_gb * GB),
        tools=["git", "uv"],
        running_units=0,
        max_units=max_units,
        rank_penalty=0.0,
        advertised_at=NOW - timedelta(seconds=1),
        cadence_seconds=10,
    )


def _place(
    ads: list[ModelHostCapacityAdvertisement],
    table: ModelLabHostsTable,
    rules: ModelPlacementRules,
    *,
    repo: str,
    kind: str = "test",
    weight: str | None = None,
    only_host: str | None = None,
    min_free_bytes: int = DEFAULT_MIN_FREE_BYTES,
) -> ModelPlacement:
    return HandlerLabPoolPlace(judge=HandlerLabPlacement(rules), environ={}).handle(
        ModelLabPoolPlaceRequest(
            advertisements=tuple(ads),
            evaluated_at=NOW,
            table=table,
            repo=repo,
            kind=kind,
            weight=weight,
            only_host=only_host,
            min_free_bytes=min_free_bytes,
        )
    )


def _profile(
    rules: ModelPlacementRules, repo: str, kind: str, weight: str | None = None
) -> ModelUnitProfile:
    return HandlerLabPlacement(rules).unit_profile(
        repo=repo,
        kind=kind,
        weight=weight,
        table_needs=ModelLabHostsTable.model_validate(
            yaml.safe_load(TABLE_YAML)
        ).unit_needs,
    )


# ---------------------------------------------------------------- the rules are contract data


def test_the_contract_declares_the_weights_and_no_private_repository(
    rules: ModelPlacementRules,
) -> None:
    assert rules.weights == ("light", "standard", "heavy")
    assert rules.weight_by_kind["lint"] == "light"
    # The h051 census (2026-10-29) cleared both public suites; a private repository's rule is the table's.
    needing = {rule.repo for rule in rules.unit_needs if "avx2" in rule.needs}
    assert not needing & {"omnibase_core", "omnimarket"}


def test_a_rule_naming_an_unknown_capability_is_refused_at_load() -> None:
    with pytest.raises(ValueError, match="not host capability columns"):
        ModelPlacementRules.model_validate(
            {
                "weights": ["light"],
                "weight_by_kind": {"lint": "light"},
                "host_defaults": {"avx2": True, "max_weight": "light", "speed": 1.0},
                "unit_needs": [{"repo": "x", "needs": ["gpu"], "reason": "r"}],
            }
        )


def test_a_table_rule_naming_an_unknown_capability_is_refused(
    rules: ModelPlacementRules,
) -> None:
    bad = ModelUnitNeedsRule(repo="x", needs=("gpu",), reason="r")
    with pytest.raises(HostTableError, match="not host capability columns"):
        HandlerLabPlacement(rules).unit_profile(
            repo="x", kind="test", weight=None, table_needs=(bad,)
        )


def test_an_unknown_weight_is_refused_not_guessed(rules: ModelPlacementRules) -> None:
    with pytest.raises(HostTableError, match="not one of"):
        _profile(rules, "native_audio", "test", "enormous")


def test_a_unit_takes_its_needs_from_its_repo_name_whatever_the_owner(
    rules: ModelPlacementRules,
) -> None:
    unit = _profile(rules, "OmniNode-ai/native_audio", "other")
    assert unit.repo == "native_audio"
    assert unit.needs == ("avx2",)
    other = _profile(rules, "OmniNode-ai/omnidash", "test")
    assert other.needs == ()


# ------------------------------------------------- an unavailable host is refused on the bus path


def test_an_unavailable_host_gets_no_unit_however_idle_it_is(
    table: ModelLabHostsTable, rules: ModelPlacementRules
) -> None:
    ads = [_ad("h051", 0.5, 80, free_gb=300), _ad("h101", 6.0, 12)]
    result = _place(ads, table, rules, repo="OmniNode-ai/omnidash")
    assert result.decision is EnumPlacementDecision.PLACED
    assert result.host_name == "h101"
    refused = next(v for v in result.verdicts if v.host_name == "h051")
    assert not refused.eligible
    assert refused.reason.startswith("host table: unavailable in the host table")
    assert "no AVX2" in refused.reason


def test_when_the_only_advertising_host_is_unavailable_the_unit_is_refused(
    table: ModelLabHostsTable, rules: ModelPlacementRules
) -> None:
    result = _place([_ad("h051", 0.5, 80)], table, rules, repo="OmniNode-ai/omnidash")
    assert result.decision is EnumPlacementDecision.REFUSED
    assert result.refusal is EnumRefusalReason.HOST_TABLE
    assert result.host_name == ""


def test_an_advertising_host_the_table_does_not_know_takes_nothing(
    table: ModelLabHostsTable, rules: ModelPlacementRules
) -> None:
    result = _place([_ad("h999", 0.1, 32)], table, rules, repo="OmniNode-ai/omnidash")
    assert result.refusal is EnumRefusalReason.HOST_TABLE
    assert "not a row of the host table" in result.verdicts[0].reason


def test_naming_an_unavailable_host_does_not_override_the_table(
    table: ModelLabHostsTable, rules: ModelPlacementRules
) -> None:
    result = _place(
        [_ad("h051", 0.5, 80), _ad("h101", 1.0, 12)],
        table,
        rules,
        repo="OmniNode-ai/omnidash",
        only_host="h051",
    )
    assert result.decision is EnumPlacementDecision.REFUSED
    assert result.refusal is EnumRefusalReason.HOST_TABLE


# ------------------------------------------------------- AVX2 needs against AVX2 capability


def test_an_avx2_unit_is_refused_on_a_no_avx2_host_and_placed_on_an_avx2_host(
    table: ModelLabHostsTable, rules: ModelPlacementRules
) -> None:
    ads = [_ad("h051-live", 0.5, 80, free_gb=300), _ad("h101", 6.0, 12)]
    result = _place(ads, table, rules, repo="OmniNode-ai/native_audio")
    assert result.host_name == "h101"
    refused = next(v for v in result.verdicts if v.host_name == "h051-live")
    assert not refused.eligible
    assert "lacks avx2" in refused.reason
    assert "native_audio_ext" in refused.reason


def test_a_public_repository_the_census_cleared_runs_on_the_no_avx2_host(
    table: ModelLabHostsTable, rules: ModelPlacementRules
) -> None:
    ads = [_ad("h051-live", 0.5, 80, free_gb=300), _ad("h101", 6.0, 12)]
    for repo in ("OmniNode-ai/omnibase_core", "OmniNode-ai/omnimarket"):
        assert _place(ads, table, rules, repo=repo).host_name == "h051-live", repo


def test_an_avx2_unit_alone_with_a_no_avx2_host_is_refused(
    table: ModelLabHostsTable, rules: ModelPlacementRules
) -> None:
    result = _place(
        [_ad("h051-live", 0.5, 80)], table, rules, repo="OmniNode-ai/native_audio"
    )
    assert result.decision is EnumPlacementDecision.REFUSED
    assert result.refusal is EnumRefusalReason.HOST_TABLE


def test_a_unit_that_needs_no_avx2_still_runs_on_the_no_avx2_host(
    table: ModelLabHostsTable, rules: ModelPlacementRules
) -> None:
    ads = [_ad("h051-live", 0.5, 80, free_gb=300), _ad("h101", 6.0, 12)]
    result = _place(ads, table, rules, repo="OmniNode-ai/omnidash")
    assert result.host_name == "h051-live"


# ---------------------------------------------------- a light unit on the small laptop's own bar


def test_a_light_unit_is_placed_on_h106_under_its_own_lower_memory_bar(
    table: ModelLabHostsTable, rules: ModelPlacementRules
) -> None:
    ads = [_ad("h106", 0.5, 8, free_gb=5.2, max_units=1)]
    # The caller's bar (the shared admission rule's 10 GB) would refuse a host with 5.2 GB free.
    result = _place(
        ads,
        table,
        rules,
        repo="OmniNode-ai/omnidash",
        kind="lint",
        min_free_bytes=10 * GB,
    )
    assert result.decision is EnumPlacementDecision.PLACED
    assert result.host_name == "h106"


def test_the_same_memory_is_short_on_a_host_without_its_own_bar(
    table: ModelLabHostsTable, rules: ModelPlacementRules
) -> None:
    result = _place(
        [_ad("h101", 0.5, 12, free_gb=5.2)],
        table,
        rules,
        repo="OmniNode-ai/omnidash",
        kind="lint",
        min_free_bytes=10 * GB,
    )
    assert result.refusal is EnumRefusalReason.OVER_CAPACITY
    assert result.verdicts[0].reason == "short of free memory"


def test_h106_keeps_its_own_bar_below_which_it_is_refused(
    table: ModelLabHostsTable, rules: ModelPlacementRules
) -> None:
    result = _place(
        [_ad("h106", 0.5, 8, free_gb=3.0, max_units=1)],
        table,
        rules,
        repo="OmniNode-ai/omnidash",
        kind="lint",
        min_free_bytes=10 * GB,
    )
    assert result.decision is EnumPlacementDecision.REFUSED
    assert "own bar is 4.0GB" in result.verdicts[0].reason


def test_h106_takes_light_units_only(
    table: ModelLabHostsTable, rules: ModelPlacementRules
) -> None:
    ads = [_ad("h106", 0.5, 8, free_gb=6, max_units=1)]
    heavy = _place(ads, table, rules, repo="OmniNode-ai/omnidash", kind="build")
    assert heavy.refusal is EnumRefusalReason.HOST_TABLE
    assert "takes only ['lint', 'test'] units, not build" in heavy.verdicts[0].reason
    standard = _place(ads, table, rules, repo="OmniNode-ai/omnidash", kind="test")
    assert (
        "takes light units at most, this one is standard" in standard.verdicts[0].reason
    )
    # A focused test file the caller declares light is a light unit.
    focused = _place(
        ads, table, rules, repo="OmniNode-ai/omnidash", kind="test", weight="light"
    )
    assert focused.host_name == "h106"


def test_a_measured_speed_ranks_hosts_without_barring_the_slow_one(
    table: ModelLabHostsTable, rules: ModelPlacementRules
) -> None:
    # Equal load per core: h101 (speed 1.0) beats h106 (0.43); h106 alone is still placed.
    ads = [_ad("h101", 2.4, 12, max_units=2), _ad("h106", 1.6, 8, max_units=2)]
    both = _place(ads, table, rules, repo="OmniNode-ai/omnidash", kind="lint")
    assert both.host_name == "h101"
    alone = _place(ads[1:], table, rules, repo="OmniNode-ai/omnidash", kind="lint")
    assert alone.host_name == "h106"


# ------------------------------------------------------------- the table is read, or nothing runs


def test_the_table_comes_from_the_file_onex_lab_run_hosts_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "hosts.yaml"
    path.write_text(TABLE_YAML, encoding="utf-8")
    document = load_host_table({"ONEX_LAB_RUN_HOSTS": str(path)})
    assert {row.name for row in document.hosts} >= {"h051", "h106"}
    h106 = next(row for row in document.hosts if row.name == "h106")
    assert (h106.min_free_gb, h106.max_weight, h106.speed) == (4.0, "light", 0.43)
    assert [rule.repo for rule in document.unit_needs] == ["native_audio"]


def test_the_table_comes_from_the_lab_hosts_overlay_when_no_file_is_named(
    tmp_path: Path,
) -> None:
    overlay = tmp_path / "lab_hosts"
    overlay.mkdir()
    (overlay / "overlay.yaml").write_text(
        TABLE_YAML + "registry_root_candidates: [$HOME/registry]\n", encoding="utf-8"
    )
    document = load_host_table({"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)})
    assert {row.name for row in document.hosts} >= {"h051", "h106"}
    assert [rule.repo for rule in document.unit_needs] == ["native_audio"]


def test_an_unreadable_table_refuses_instead_of_admitting(tmp_path: Path) -> None:
    with pytest.raises(HostTableError, match="could not be read"):
        load_host_table({"ONEX_LAB_RUN_HOSTS": str(tmp_path / "absent.yaml")})
    with pytest.raises(HostTableError, match="could not be read"):
        load_host_table({"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)})


def test_the_judge_command_prints_the_tables_verdicts_for_the_shim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "hosts.yaml"
    path.write_text(TABLE_YAML, encoding="utf-8")
    monkeypatch.setenv("ONEX_LAB_RUN_HOSTS", str(path))
    result = CliRunner().invoke(
        cli.lab_work_group,
        [
            "judge",
            "--repo",
            "OmniNode-ai/native_audio",
            "--kind",
            "test",
            "h051",
            "h051-live",
            "h101",
            "h106",
        ],
    )
    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    assert body["unit"]["needs"] == ["avx2"]
    by_host = {v["host_name"]: v for v in body["verdicts"]}
    assert by_host["h101"]["eligible"] is True
    assert "unavailable" in by_host["h051"]["reason"]
    assert "lacks avx2" in by_host["h051-live"]["reason"]
    assert by_host["h106"]["eligible"] is False
    assert by_host["h106"]["min_free_gb"] == 4.0
    assert by_host["h106"]["speed"] == 0.43


def test_the_judge_command_reads_the_unit_from_the_launchers_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The plugin's launcher hands over the arguments it was given; the unit is named by --repo or by the clone's origin."""
    path = tmp_path / "hosts.yaml"
    path.write_text(TABLE_YAML, encoding="utf-8")
    monkeypatch.setenv("ONEX_LAB_RUN_HOSTS", str(path))

    def judge(words: list[str], *extra: str) -> dict[str, Any]:
        result = CliRunner().invoke(
            cli.lab_work_group,
            ["judge", "--words", json.dumps(words), *extra, "h051-live", "h106"],
        )
        assert result.exit_code == 0, result.output
        body: dict[str, Any] = json.loads(result.stdout)
        return body

    named = judge(
        ["--lane", "l", "--repo", "native_audio", "--kind=lint", "--ref", "b"]
    )
    assert named["unit"]["repo"] == "native_audio"
    assert named["unit"]["kind"] == "lint"
    assert named["unit"]["weight"] == "light"
    assert named["unit"]["needs"] == ["avx2"]
    origin = tmp_path / "clone"
    origin.mkdir()
    subprocess.run(
        ["git", "init", "-q", str(origin)],
        check=True,
        env=scrub_git_location_env(os.environ),
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(origin),
            "remote",
            "add",
            "origin",
            "git@github.com:OmniNode-ai/omnimarket.git",
        ],
        check=True,
        env=scrub_git_location_env(os.environ),
    )
    cloned = judge(["--clone", str(origin), "--kind", "test"])
    assert cloned["unit"]["repo"] == "omnimarket"
    explicit = judge(["--repo", "omnidash", "--kind", "test"], "--weight", "light")
    assert explicit["unit"]["weight"] == "light"
    assert explicit["unit"]["needs"] == []


def test_the_judge_command_exits_69_when_the_unit_or_the_table_cannot_be_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ONEX_LAB_RUN_HOSTS", str(tmp_path / "absent.yaml"))
    result = CliRunner().invoke(cli.lab_work_group, ["judge", "--repo", "x/y", "h101"])
    assert result.exit_code == cli.EXIT_NO_ADVERTISEMENT == 69
    path = tmp_path / "hosts.yaml"
    path.write_text(TABLE_YAML, encoding="utf-8")
    monkeypatch.setenv("ONEX_LAB_RUN_HOSTS", str(path))
    monkeypatch.chdir(tmp_path)  # not a git checkout, and no --repo
    unnamed = CliRunner().invoke(cli.lab_work_group, ["judge", "--words", "[]", "h101"])
    assert unnamed.exit_code == 69
    assert "repository is not named" in unnamed.output


# ----------------------------------------------------- end to end over the in-memory bus (`run`)


class _Reader:
    def __init__(self, load1: float, cores: int, free_gb: float) -> None:
        self.load1, self.cores, self.free_gb = load1, cores, free_gb

    def read(self, tools: list[str]) -> HostReading:
        return HostReading(
            cores=self.cores,
            load1=self.load1,
            mem_available_bytes=int(self.free_gb * GB),
            tools=("git", "uv"),
        )


class _NeverRuns:
    def run(
        self, request: ModelLabWorkUnitRequest, host_name: str
    ) -> ModelLabWorkUnitReceipt:
        raise AssertionError("a refused unit must not reach a host")


def _run_args(repo: str, *extra: str) -> list[str]:
    return [
        "run",
        "--bus",
        "inmemory",
        "--repo",
        repo,
        "--sha",
        "4c3985a451847571e2cebd06a6edf1940069946e",
        "--lane",
        "test-lane",
        "--window",
        "0.1",
        "--dry-run",
        *extra,
        "--",
        "uv",
        "run",
        "pytest",
    ]


def test_the_run_command_refuses_an_unavailable_host_that_advertises_and_places_the_other(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bus path itself: h051 advertises 80 idle cores and the table marks it unavailable."""
    path = tmp_path / "hosts.yaml"
    path.write_text(TABLE_YAML, encoding="utf-8")
    monkeypatch.setenv("ONEX_LAB_RUN_HOSTS", str(path))
    pool = [("h051", 0.5, 80, 300.0), ("h101", 6.0, 12, 32.0)]

    @asynccontextmanager
    async def open_pool(**kwargs: object) -> AsyncIterator[EventBusInmemory]:
        bus = EventBusInmemory(environment="local", group="lab-work-table-test")
        await bus.start()
        hosts = [
            LabWorkHost(
                bus,
                HandlerLabWorkUnitEffect(name, _NeverRuns()),
                HandlerHostCapacityAdvertiseEffect(_Reader(load, cores, free)),
                max_units=4,
            )
            for name, load, cores, free in pool
        ]

        async def collect(
            caller: LabWorkCaller, window_seconds: float
        ) -> list[ModelHostCapacityAdvertisement]:
            for host in hosts:
                await host.advertise_once()
            return caller.advertisements()

        monkeypatch.setattr(LabWorkCaller, "collect", collect)
        try:
            yield bus
        finally:
            await bus.close()

    monkeypatch.setattr(cli, "open_lab_run_bus", open_pool)
    ok = CliRunner().invoke(cli.lab_work_group, _run_args("OmniNode-ai/omnidash"))
    assert ok.exit_code == 0, ok.output
    assert "placement: placed on h101" in ok.output
    assert "h051: host table: unavailable in the host table" in ok.output

    core = CliRunner().invoke(cli.lab_work_group, _run_args("OmniNode-ai/native_audio"))
    assert core.exit_code == 0, core.output
    assert "placement: placed on h101" in core.output

    only = CliRunner().invoke(
        cli.lab_work_group, _run_args("OmniNode-ai/omnidash", "--host", "h051")
    )
    assert only.exit_code == cli.EXIT_NO_HOST == 75, only.output
    assert "placement: refused (host_table)" in only.output
    assert "not running it anywhere" in only.output


def test_the_run_command_reads_the_weight_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shim hands the weight down through ONEX_LAB_WORK_WEIGHT, as the lab-run launcher has no flag for it."""
    path = tmp_path / "hosts.yaml"
    path.write_text(TABLE_YAML, encoding="utf-8")
    monkeypatch.setenv("ONEX_LAB_RUN_HOSTS", str(path))

    @asynccontextmanager
    async def open_pool(**kwargs: object) -> AsyncIterator[EventBusInmemory]:
        bus = EventBusInmemory(environment="local", group="lab-work-weight-test")
        await bus.start()
        host = LabWorkHost(
            bus,
            HandlerLabWorkUnitEffect("h106", _NeverRuns()),
            HandlerHostCapacityAdvertiseEffect(_Reader(0.5, 8, 5.2)),
            max_units=1,
        )

        async def collect(
            caller: LabWorkCaller, window_seconds: float
        ) -> list[ModelHostCapacityAdvertisement]:
            await host.advertise_once()
            return caller.advertisements()

        monkeypatch.setattr(LabWorkCaller, "collect", collect)
        try:
            yield bus
        finally:
            await bus.close()

    monkeypatch.setattr(cli, "open_lab_run_bus", open_pool)
    args = _run_args("OmniNode-ai/omnidash", "--kind", "test", "--min-free-gb", "10")
    standard = CliRunner().invoke(cli.lab_work_group, args)
    assert standard.exit_code == cli.EXIT_NO_HOST, standard.output
    monkeypatch.setenv("ONEX_LAB_WORK_WEIGHT", "light")
    light = CliRunner().invoke(cli.lab_work_group, args)
    assert light.exit_code == 0, light.output
    assert "placement: placed on h106" in light.output


def test_injected_environment_weight_reaches_placement(
    table: ModelLabHostsTable,
) -> None:
    request = ModelLabPoolPlaceRequest(
        advertisements=(_ad("h106", 0.5, 8, free_gb=5.2),),
        evaluated_at=NOW,
        repo="OmniNode-ai/omnidash",
        kind="test",
        table=table,
        min_free_bytes=10 * GB,
    )
    assert not HandlerLabPoolPlace(environ={}).handle(request).placed
    light = HandlerLabPoolPlace(environ={"ONEX_LAB_WORK_WEIGHT": "light"}).handle(
        request
    )
    assert light.placed
    assert light.host_name == "h106"
    explicit = request.model_copy(update={"weight": "standard"})
    assert (
        not HandlerLabPoolPlace(environ={"ONEX_LAB_WORK_WEIGHT": "light"})
        .handle(explicit)
        .placed
    )


def test_default_placer_refuses_an_unreadable_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ONEX_LAB_RUN_HOSTS", str(tmp_path / "absent.yaml"))
    placement = HandlerLabPoolPlace().handle(
        ModelLabPoolPlaceRequest(
            advertisements=(_ad("h101", 0.1, 12),),
            evaluated_at=NOW,
        )
    )
    assert placement.decision is EnumPlacementDecision.REFUSED
    assert placement.refusal is EnumRefusalReason.COULD_NOT_CHECK
    assert not placement.placed
    assert placement.verdicts == []
    assert "could not be read" in placement.detail
    assert placement.detail in placement.describe().splitlines()


@pytest.mark.parametrize("weight", ["unknown", "heavy"])
def test_bad_weight_fails_closed(table: ModelLabHostsTable, weight: str) -> None:
    if weight == "heavy":
        table = table.model_copy(
            update={
                "hosts": tuple(
                    row.model_copy(update={"max_weight": "unknown"})
                    for row in table.hosts
                )
            }
        )
    placement = HandlerLabPoolPlace(environ={}).handle(
        ModelLabPoolPlaceRequest(
            advertisements=(_ad("h101", 0.1, 12),),
            evaluated_at=NOW,
            table=table,
            weight=weight,
        )
    )
    assert placement.refusal is EnumRefusalReason.COULD_NOT_CHECK
    assert "not one of" in placement.detail


def test_invalid_rules_refuse_inside_handle(
    table: ModelLabHostsTable, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnimarket.nodes.node_lab_placement_compute.handlers import (
        handler_lab_placement,
    )

    def invalid_rules() -> ModelPlacementRules:
        raise HostTableError("the lab placement rules are invalid: unknown weight")

    monkeypatch.setattr(handler_lab_placement, "load_placement_rules", invalid_rules)
    placer = HandlerLabPoolPlace()
    result = placer.handle(
        ModelLabPoolPlaceRequest(
            advertisements=(_ad("h101", 0.1, 12),),
            evaluated_at=NOW,
            table=table,
        )
    )
    assert result.refusal is EnumRefusalReason.COULD_NOT_CHECK
    assert "rules are invalid" in result.detail


def test_run_command_describes_an_unreadable_table_and_exits_69(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ONEX_LAB_RUN_HOSTS", str(tmp_path / "absent.yaml"))
    result = CliRunner().invoke(cli.lab_work_group, _run_args("OmniNode-ai/omnimarket"))
    assert result.exit_code == 69
    assert "placement: refused (could_not_check)" in result.output
    assert "could not be read" in result.output
    assert "no pool host can take this unit" in result.output
