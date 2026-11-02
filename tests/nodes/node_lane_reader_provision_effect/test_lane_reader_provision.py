# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_lane_reader_provision_effect: the catalog find and ledger parity reader inputs, provisioned per declared lane host (OMN-22100).

The host boundary is injected, so the handler runs against an in-memory host here; the same scenario also runs over the in-memory
bus and, marked ``kafka``, over a Redpanda broker. Hosts are documentation names (``.test``) and paths sit under a fake home.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_lane_reader_provision_effect.handlers.adapter_bus import (
    contract_topics,
    serve_on_bus,
)
from omnimarket.nodes.node_lane_reader_provision_effect.handlers.adapter_host_files import (
    NODE_NAME,
    HostFiles,
    load_overlay,
    load_table,
)
from omnimarket.nodes.node_lane_reader_provision_effect.handlers.handler_lane_reader_provision import (
    CATALOG_ENV,
    WL_ENV,
    WL_PASSWORD_KEY,
    HandlerLaneReaderProvision,
    env_key_names,
    upsert_env_lines,
)
from omnimarket.nodes.node_lane_reader_provision_effect.models.model_lane_reader_provision import (
    ModelLaneReaderProvisionHost,
    ModelLaneReaderProvisionOverlay,
    ModelLaneReaderProvisionRequest,
    ModelLaneReaderProvisionResult,
)
from tests.node_overlay_support import install_node_overlay

pytestmark = pytest.mark.unit

HOME = "/home/lane.test"
SERVE_ENV = f"{HOME}/.config/onex/lab-work-serve.env"
LANE_BIN = f"{HOME}/.local/bin"
CLI_TARGET = "/srv/internal.test/.venv/bin/onex-capability-catalog"
TABLE_TEXT = (
    "hosts:\n"
    "  lane-a.test:\n"
    "    catalog_json: .onex_state/capability/catalog.json\n"
    "    wl_env_file: .omnibase/.env\n"
)
CATALOG = f"{HOME}/.onex_state/capability/catalog.json"
WL_ENV_PATH = f"{HOME}/.omnibase/.env"
# A stand-in credential the result must never carry.
SECRET_VALUE = "pw-sentinel-0451"


class FakeHost:
    """An in-memory host: files, links and recorded commands."""

    def __init__(self) -> None:
        self.files: dict[str, str] = {}
        self.links: dict[str, str] = {}
        self.ran: list[tuple[tuple[str, ...], dict[str, str]]] = []
        self.producer_status = 0
        self.producer_writes_catalog = True

    def exists(self, path: str) -> bool:
        return path in self.files or path in self.links

    def read_text(self, path: str) -> str | None:
        return self.files.get(path)

    def write_text_atomic(self, path: str, text: str) -> None:
        self.files[path] = text

    def link_target(self, path: str) -> str | None:
        return self.links.get(path)

    def replace_link(self, path: str, target: str) -> None:
        self.links[path] = target

    def run(self, argv: Sequence[str], env: Mapping[str, str]) -> tuple[int, str]:
        self.ran.append((tuple(argv), dict(env)))
        if (
            argv[0] == "producer"
            and self.producer_status == 0
            and self.producer_writes_catalog
        ):
            catalog = (
                f"{argv[argv.index('--out-dir') + 1]}/catalog.json"
                if "--out-dir" in argv
                else argv[-1]
            )
            self.files[catalog] = "{}"
        return (self.producer_status, "producer output")


def host_row(**changes: object) -> ModelLaneReaderProvisionHost:
    base: dict[str, object] = {
        "name": "lane-a.test",
        "home": HOME,
        "serve_env_file": SERVE_ENV,
        "lane_bin_dir": LANE_BIN,
        "catalog_cli_target": CLI_TARGET,
        "catalog_producer_argv": ("producer", "{catalog_json}"),
        "registry_root": "/srv/omni.test",
        "restart_argv": ("restart-serve",),
    }
    return ModelLaneReaderProvisionHost.model_validate({**base, **changes})


def prepared_host() -> FakeHost:
    host = FakeHost()
    host.files[SERVE_ENV] = "OMNI_HOME=/srv/omni.test\nPATH=/usr/bin\n"
    host.files[CLI_TARGET] = "#!tool"
    host.files[WL_ENV_PATH] = f"INFISICAL_URL=x\n{WL_PASSWORD_KEY}={SECRET_VALUE}\n"
    return host


def handler_for(
    host: FakeHost, *rows: ModelLaneReaderProvisionHost
) -> HandlerLaneReaderProvision:
    overlay = ModelLaneReaderProvisionOverlay(hosts=rows or (host_row(),))
    return HandlerLaneReaderProvision(overlay, load_table(TABLE_TEXT), host)


def request(**changes: object) -> ModelLaneReaderProvisionRequest:
    return ModelLaneReaderProvisionRequest.model_validate(
        {"host": "lane-a.test", **changes}
    )


def states(result: ModelLaneReaderProvisionResult) -> dict[str, str]:
    return {s.step: s.state for s in result.steps}


def test_provisions_the_catalog_the_link_and_the_two_variables() -> None:
    host = prepared_host()
    result = handler_for(host).handle(request())

    assert states(result) == {
        "catalog-json": "changed",
        "catalog-cli": "changed",
        "serve-env": "changed",
        "wl-env-file": "unchanged",
    }
    assert result.ok
    assert result.restart_needed
    assert result.operator_steps == ()
    assert host.files[CATALOG] == "{}"
    assert host.links[f"{LANE_BIN}/onex-capability-catalog"] == CLI_TARGET
    assert host.files[SERVE_ENV] == (
        "OMNI_HOME=/srv/omni.test\nPATH=/usr/bin\n"
        f"{CATALOG_ENV}={CATALOG}\n{WL_ENV}={WL_ENV_PATH}\n"
    )
    assert host.ran == [
        (("producer", CATALOG), {"HOME": HOME, "OMNI_HOME": "/srv/omni.test"})
    ]


def test_runtime_defaults_load_host_configuration_only_when_handling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnimarket.nodes.node_lane_reader_provision_effect.handlers import (
        handler_lane_reader_provision as module,
    )

    host = prepared_host()
    calls: list[str] = []

    def overlay() -> ModelLaneReaderProvisionOverlay:
        calls.append("overlay")
        return ModelLaneReaderProvisionOverlay(hosts=(host_row(),))

    def table() -> dict[str, module.ModelLaneReaderTableRow]:
        calls.append("table")
        return load_table(TABLE_TEXT)

    monkeypatch.setattr(module, "load_overlay", overlay)
    monkeypatch.setattr(module, "load_table", table)
    handler = HandlerLaneReaderProvision(port=host)
    assert calls == []

    result = handler.handle(request())

    assert calls == ["overlay", "table"]
    assert result.ok
    assert states(result)["catalog-json"] == "changed"
    assert host.files[CATALOG] == "{}"


def test_catalog_producer_substitutes_catalog_directory() -> None:
    host = prepared_host()
    producer = (
        "producer",
        "collect",
        "--tools",
        "--out-dir",
        "{catalog_dir}",
        "--catalog={catalog_json}",
    )

    result = handler_for(host, host_row(catalog_producer_argv=producer)).handle(
        request()
    )

    assert states(result)["catalog-json"] == "changed"
    assert host.files[CATALOG] == "{}"
    assert host.ran == [
        (
            (
                "producer",
                "collect",
                "--tools",
                "--out-dir",
                str(Path(CATALOG).parent),
                f"--catalog={CATALOG}",
            ),
            {"HOME": HOME, "OMNI_HOME": "/srv/omni.test"},
        )
    ]


def test_a_second_run_changes_nothing() -> None:
    host = prepared_host()
    handler = handler_for(host)
    handler.handle(request())
    before = (dict(host.files), dict(host.links), len(host.ran))

    result = handler.handle(request())

    assert set(states(result).values()) == {"unchanged"}
    assert not result.restart_needed
    assert (dict(host.files), dict(host.links), len(host.ran)) == before


def test_check_only_reports_and_writes_nothing() -> None:
    host = prepared_host()
    before = (dict(host.files), dict(host.links))

    result = handler_for(host).handle(request(check_only=True))

    assert states(result)["catalog-json"] == "would-change"
    assert states(result)["catalog-cli"] == "would-change"
    assert states(result)["serve-env"] == "would-change"
    assert (dict(host.files), dict(host.links), host.ran) == (*before, [])


def test_only_the_two_variable_lines_are_rewritten() -> None:
    old = "A=1\nONEX_CAPABILITY_CATALOG_JSON=/old\n# keep me\nWL_ENV_FILE=/old\nWL_ENV_FILE=/dup\nB=2\n"
    new = upsert_env_lines(old, [(CATALOG_ENV, "/n/c.json"), (WL_ENV, "/n/.env")])
    assert (
        new
        == "A=1\nONEX_CAPABILITY_CATALOG_JSON=/n/c.json\n# keep me\nWL_ENV_FILE=/n/.env\nB=2\n"
    )


def test_a_missing_password_key_is_an_operator_step_named_without_a_value() -> None:
    host = prepared_host()
    host.files[WL_ENV_PATH] = (
        "INFISICAL_URL=x\nINFISICAL_CLIENT_SECRET=" + SECRET_VALUE + "\n"
    )

    result = handler_for(host).handle(request())

    assert states(result)["wl-env-file"] == "needs-operator"
    assert result.ok, "an operator step is open work, not a failure"
    assert len(result.operator_steps) == 1
    assert WL_PASSWORD_KEY in result.operator_steps[0]
    assert WL_ENV_PATH in result.operator_steps[0]
    assert SECRET_VALUE not in result.model_dump_json()
    assert env_key_names(host.files[WL_ENV_PATH]) == {
        "INFISICAL_URL",
        "INFISICAL_CLIENT_SECRET",
    }


def test_an_absent_lab_env_file_is_an_operator_step() -> None:
    host = prepared_host()
    del host.files[WL_ENV_PATH]

    result = handler_for(host).handle(request())

    assert states(result)["wl-env-file"] == "needs-operator"
    assert result.operator_steps


def test_a_host_the_overlay_or_the_table_lacks_is_refused() -> None:
    host = prepared_host()
    assert states(handler_for(host).handle(request(host="other.test"))) == {
        "declaration": "refused"
    }
    assert states(
        handler_for(host, host_row(name="other.test")).handle(
            request(host="other.test")
        )
    ) == {"declaration": "refused"}
    assert host.ran == []
    assert not host.links


def test_an_unquotable_path_is_refused_before_any_write() -> None:
    host = prepared_host()
    result = handler_for(host, host_row(home="/home/lane test")).handle(request())
    assert states(result) == {"declaration": "refused"}
    assert host.files[SERVE_ENV] == "OMNI_HOME=/srv/omni.test\nPATH=/usr/bin\n"


def test_a_failing_producer_fails_the_step() -> None:
    host = prepared_host()
    host.producer_status = 3
    result = handler_for(host).handle(request())
    assert states(result)["catalog-json"] == "failed"
    assert not result.ok


def test_a_producer_that_writes_no_catalog_fails_the_step() -> None:
    host = prepared_host()
    host.producer_writes_catalog = False
    assert states(handler_for(host).handle(request()))["catalog-json"] == "failed"


def test_a_regular_file_at_the_link_is_left_alone() -> None:
    host = prepared_host()
    host.files[f"{LANE_BIN}/onex-capability-catalog"] = "not a link"
    result = handler_for(host).handle(request())
    assert states(result)["catalog-cli"] == "refused"
    assert host.files[f"{LANE_BIN}/onex-capability-catalog"] == "not a link"


def test_an_absent_serve_env_file_is_refused_not_created() -> None:
    host = prepared_host()
    del host.files[SERVE_ENV]
    result = handler_for(host).handle(request())
    assert states(result)["serve-env"] == "refused"
    assert SERVE_ENV not in host.files


def test_restart_runs_only_when_asked_and_the_environment_changed() -> None:
    host = prepared_host()
    handler = handler_for(host)
    result = handler.handle(request(restart=True))
    assert states(result)["restart"] == "changed"
    assert not result.restart_needed
    assert (("restart-serve",), {"HOME": HOME}) in host.ran
    host.ran.clear()
    handler.handle(request(restart=True))
    assert host.ran == [], "a second run changes nothing and restarts nothing"


def test_restart_without_a_declared_argv_is_refused() -> None:
    host = prepared_host()
    result = handler_for(host, host_row(restart_argv=None)).handle(
        request(restart=True)
    )
    assert states(result)["restart"] == "refused"
    assert result.restart_needed


def test_the_committed_table_resolves_for_every_declared_host() -> None:
    table = load_table()
    assert table
    assert all(name == row.host for name, row in table.items())
    assert all(row.catalog_json and row.wl_env_file for row in table.values())


def test_the_overlay_is_read_from_the_overlay_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install_node_overlay(
        monkeypatch,
        tmp_path,
        NODE_NAME,
        {"hosts": [json.loads(host_row().model_dump_json())]},
    )
    assert load_overlay().hosts[0].name == "lane-a.test"


def test_no_overlay_refuses_with_how_to_supply_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    with pytest.raises(NodeOverlayError, match=NODE_NAME):
        load_overlay()


def test_the_real_host_boundary_writes_atomically_and_links(tmp_path: Path) -> None:
    port = HostFiles()
    file = tmp_path / "unit.env"
    assert port.read_text(str(file)) is None
    file.write_text("A=1\n")
    file.chmod(0o640)
    port.write_text_atomic(str(file), "A=2\n")
    assert file.read_text() == "A=2\n"
    assert file.stat().st_mode & 0o777 == 0o640
    link = tmp_path / "bin" / "tool"
    assert port.link_target(str(link)) is None
    assert not port.exists(str(link))
    port.replace_link(str(link), "/x/tool")
    port.replace_link(str(link), "/y/tool")
    assert port.link_target(str(link)) == "/y/tool"
    assert port.run(["true"], {}) == (0, "")
    assert port.run(["no-such-program-omn22100"], {})[0] == 127


# --- the two bus profiles: the same scenario, only the bus differs ---------------------------------------------------


async def drive_over_bus(bus: Any) -> ModelLaneReaderProvisionResult:
    host = prepared_host()
    command_topic, completed_topic = contract_topics()
    group = f"lane-reader-provision-{uuid4().hex[:8]}"
    done: list[ModelLaneReaderProvisionResult] = []
    correlation = uuid4().hex

    async def on_completed(message: Any) -> None:
        result = ModelLaneReaderProvisionResult.model_validate_json(message.value)
        if result.correlation_id == correlation:
            done.append(result)

    stop_serving = await serve_on_bus(bus, handler_for(host), group_id=group)
    stop_observing = await bus.subscribe(
        completed_topic, on_message=on_completed, group_id=f"{group}-observer"
    )
    try:
        await bus.publish(
            command_topic,
            b"lane-a.test",
            request(correlation_id=correlation).model_dump_json().encode(),
        )
        for _ in range(150):
            if done:
                break
            await asyncio.sleep(0.2)
    finally:
        await stop_observing()
        await stop_serving()
    assert host.links[f"{LANE_BIN}/onex-capability-catalog"] == CLI_TARGET
    assert CATALOG_ENV in host.files[SERVE_ENV]
    assert done, "falsifier: no completed event reached the observer"
    return done[0]


async def test_over_the_in_memory_bus() -> None:
    bus = EventBusInmemory(environment="dev", group="lane-reader-provision")
    await bus.start()
    try:
        result = await drive_over_bus(bus)
    finally:
        await bus.close()
    assert states(result)["serve-env"] == "changed"


@pytest.mark.integration
@pytest.mark.kafka
async def test_over_the_redpanda_bus(kafka_integration_bus: Any) -> None:
    result = await drive_over_bus(kafka_integration_bus)
    assert states(result)["serve-env"] == "changed"
    assert result.ok
