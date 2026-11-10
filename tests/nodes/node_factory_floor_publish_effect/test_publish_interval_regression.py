# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Replay the oversized process series and the timer's missing environment."""

from __future__ import annotations

import gzip
import json
import plistlib
import subprocess
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_factory_floor_publish_effect.handlers.handler_delegation_report import (
    HandlerDelegationReport,
)
from omnimarket.nodes.node_factory_floor_publish_effect.handlers.handler_factory_floor_publish import (
    HandlerFactoryFloorPublish,
    PublishRefusedError,
    load_runtime,
)
from omnimarket.nodes.node_factory_floor_publish_effect.handlers.handler_process_health_payload import (
    HandlerProcessHealthPayload,
    load_payload_policy,
)
from omnimarket.nodes.node_factory_floor_publish_effect.handlers.handler_reports_publish import (
    MAX_DATA_BYTES,
    HandlerReportsPublish,
)
from omnimarket.nodes.node_factory_floor_publish_effect.models.model_delegation_report import (
    ModelDelegationReadOverlay,
    ModelDelegationReportRequest,
)
from omnimarket.nodes.node_factory_floor_publish_effect.models.model_factory_floor_publish import (
    ModelOperatorAccessResult,
    ModelReportsPublishResult,
)
from omnimarket.nodes.node_factory_floor_publish_effect.models.model_process_health_payload import (
    ModelProcessHealthPayloadRequest,
    ModelProcessHealthPayloadResult,
)
from omnimarket.nodes.node_factory_floor_publish_effect.runtime_factory_floor_publish import (
    main,
)
from omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units import (
    HandlerRegistryUnits,
    load_units,
)
from omnimarket.nodes.node_host_installer_registry_units.models.model_registry_units import (
    ModelRegistryUnitsRequest,
)
from tests.nodes.node_factory_floor_publish_effect.test_factory_floor_publish import (
    LocalSsh,
    _request,
)


def large_series(home: Path) -> dict[str, Any]:
    """The recorded series shape, over the same 8 MiB report-reader boundary."""
    document = {
        "generated_at": "2026-11-04T23:40:13Z",
        "days": [],
        "summary": {"comparisons": []},
        "range_overlay": {
            "start": "2026-10-01T00:00:00Z",
            "end": "2026-11-04T23:40:13Z",
            "measures": [
                {
                    "key": "lanes.terminals",
                    "reducer": "sum",
                    "observations": [
                        {
                            "at": "2026-11-04T23:00:00Z",
                            "value": 1,
                            "identity": "a" * 24,
                            "weight": 2,
                        }
                    ]
                    * 60_000,
                }
            ],
        },
    }
    path = home / load_runtime().process_health_relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2))
    assert path.stat().st_size > MAX_DATA_BYTES
    return document


def test_large_series_keeps_the_floor_small_and_range_data_lossless(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    original = large_series(Path(request.home or ""))
    result = HandlerFactoryFloorPublish(LocalSsh()).handle(request, environ={})
    site = tmp_path / "site"
    summary_bytes = (site / "process-health.json").read_bytes()
    assert len(summary_bytes) < MAX_DATA_BYTES
    summary = json.loads(summary_bytes)
    assert "range_overlay" not in summary
    detail_name = summary["detail_file"]
    assert detail_name in result.published
    detail = json.loads(gzip.decompress((site / detail_name).read_bytes()))
    assert detail == original


def test_report_catalogue_reads_the_same_small_health_representation(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    home = Path(request.home or "")
    original = large_series(home)
    item = next(
        e for e in load_runtime().reports.entries if e.id == "process_health"
    ).data[0]
    row, payload = HandlerReportsPublish.read_data(home, item)
    assert row["status"] == "read", row["reason"]
    assert payload is not None
    assert len(payload) < MAX_DATA_BYTES
    assert json.loads(payload)["generated_at"] == original["generated_at"]


def test_binary_detail_survives_the_declared_result_wire_format(tmp_path: Path) -> None:
    large_series(tmp_path)
    result = HandlerProcessHealthPayload().handle(
        ModelProcessHealthPayloadRequest(
            source_path=str(tmp_path / load_runtime().process_health_relpath)
        )
    )
    assert result.detail is not None
    assert (
        ModelProcessHealthPayloadResult.model_validate_json(result.model_dump_json())
        == result
    )


def rendered_timer(tmp_path: Path, **fields: Any) -> dict[str, Any]:
    request = ModelRegistryUnitsRequest(
        unit="factory-floor-publish",
        home=str(tmp_path),
        registry_root=str(tmp_path),
        workspace_config_root=str(tmp_path),
        plans_path=str(tmp_path),
        marketplace_root=str(tmp_path),
        **fields,
    )
    handler = HandlerRegistryUnits(
        lambda argv: subprocess.CompletedProcess(argv, 0, "test-user", "")
    )
    text = handler.render(request, load_units()[request.unit], tmp_path, "uv", "python")
    return plistlib.loads(text.encode())


def test_timer_uses_the_environment_prepared_by_the_installer(tmp_path: Path) -> None:
    timer = rendered_timer(tmp_path)
    assert "--no-sync" in timer["ProgramArguments"]


def test_install_refuses_to_leave_the_projection_host_unconfigured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ONEX_RUNTIME_SSH_HOST", raising=False)
    monkeypatch.setenv("ONEX_LEDGER_HOST_NAME", "declared-ledger")
    with pytest.raises(ValueError, match="ONEX_RUNTIME_SSH_HOST is unset"):
        rendered_timer(tmp_path, mode="install")
    monkeypatch.setenv("ONEX_RUNTIME_SSH_HOST", "declared-runtime")
    timer = rendered_timer(tmp_path, mode="install")
    assert timer["EnvironmentVariables"]["ONEX_RUNTIME_SSH_HOST"] == "declared-runtime"


def test_oversized_source_is_refused_before_reading_its_bytes(tmp_path: Path) -> None:
    policy = load_payload_policy()
    path = tmp_path / "too-large.json"
    with path.open("wb") as stream:
        stream.truncate(policy.max_source_bytes + 1)
    result = HandlerProcessHealthPayload().handle(
        ModelProcessHealthPayloadRequest(source_path=str(path))
    )
    assert result.summary is None
    assert result.detail is None
    assert "source exceeds" in result.reason


def test_failed_detail_transfer_leaves_the_previous_health_summary(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    large_series(Path(request.home or ""))
    site = tmp_path / "site"
    site.mkdir()
    previous = b'{"generated_at":"previous"}'
    (site / "process-health.json").write_bytes(previous)
    with pytest.raises(PublishRefusedError, match="disk full"):
        HandlerFactoryFloorPublish(
            LocalSsh(fail_name=load_payload_policy().detail_file)
        ).handle(request, environ={})
    assert (site / "process-health.json").read_bytes() == previous


@pytest.mark.parametrize(
    "mode", ["compressed", "legacy", "mismatch", "unsafe-name", "cached"]
)
def test_browser_reads_the_lossless_detail_and_refuses_mixed_generations(
    mode: str,
) -> None:
    page = (
        Path(__file__).resolve().parents[3]
        / "src/omnimarket/nodes/node_factory_floor_publish_effect/page/reports/process-health/index.html"
    ).read_text()
    reader = page[
        page.index("async function readHealth(){") : page.index(
            "async function load(){"
        )
    ]
    original = {
        "generated_at": "2026-11-04T23:40:13Z",
        "days": [],
        "summary": {},
        "range_overlay": {"measures": []},
    }
    full = dict(original)
    summary = {key: value for key, value in full.items() if key != "range_overlay"}
    summary["detail_file"] = "process-health-detail.json.gz"
    if mode == "legacy":
        summary = original
    elif mode == "mismatch":
        full["generated_at"] = "newer"
    elif mode == "unsafe-name":
        summary["detail_file"] = "../private.json.gz"
    compressed = list(gzip.compress(json.dumps(full).encode(), mtime=0))
    program = f"""
const DATA_FILE = '../../process-health.json';
let health = {json.dumps(full) if mode == "cached" else "null"};
const calls = [];
global.fetch = async path => {{
  calls.push(path);
  return path===DATA_FILE ? Response.json({json.dumps(summary)}) : new Response(Uint8Array.from({json.dumps(compressed)}));
}};
{reader}
readHealth().then(value=>console.log(JSON.stringify({{value,calls}})))
  .catch(error=>console.log(JSON.stringify({{error:error.message,calls}})));
"""
    done = subprocess.run(
        ["node", "-e", program], capture_output=True, text=True, check=True
    )
    result = json.loads(done.stdout)
    if mode == "mismatch":
        assert "changed during publication" in result["error"]
    elif mode == "unsafe-name":
        assert result["error"] == "invalid detail file"
        assert len(result["calls"]) == 1
    else:
        assert result["value"] == original
        assert len(result["calls"]) == (2 if mode == "compressed" else 1)


def test_timer_persists_the_declared_runtime_host_for_delegation(
    tmp_path: Path,
) -> None:
    timer = rendered_timer(tmp_path, runtime_ssh_host="lab-runtime")
    environment = timer["EnvironmentVariables"]
    assert environment["ONEX_RUNTIME_SSH_HOST"] == "lab-runtime"
    overlay = ModelDelegationReadOverlay(
        ssh_host_env="ONEX_RUNTIME_SSH_HOST",
        compose_project="lab",
        postgres_service="postgres",
        runtime_service="runtime",
        database="projection",
        db_user="reader",
        contract_path="/app/delegation.yaml",
    )
    calls = []

    def run(
        argv: list[str], stdin: bytes | None, timeout: int
    ) -> subprocess.CompletedProcess[bytes]:
        calls.append(argv)
        snapshot = {"now": "2026-11-04T23:40:13Z", "total_rows": 1, "rows": []}
        payload = json.dumps(snapshot) + '\n@@CONFIG\n{"endpoints": [], "chains": []}'
        return subprocess.CompletedProcess(argv, 0, payload.encode(), b"")

    result = HandlerDelegationReport(run).handle(
        ModelDelegationReportRequest(overlay=overlay), environ=environment
    )
    assert result.report["status"] == "read"
    assert "lab-runtime" in calls[0]


def test_shared_report_pages_publish_even_when_the_floor_refresh_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = []

    def refuse(*args: Any, **kwargs: Any) -> None:
        raise PublishRefusedError("recorded required refresh failure")

    def reports(*args: Any, **kwargs: Any) -> ModelReportsPublishResult:
        calls.append("reports")
        return ModelReportsPublishResult(
            published=("reports/index.html",),
            unread=(),
            manifest_sha256="a" * 64,
            index_url="http://example.invalid/reports/",
        )

    monkeypatch.setattr(HandlerFactoryFloorPublish, "handle", refuse)
    monkeypatch.setattr(
        "omnimarket.nodes.node_factory_floor_publish_effect.runtime_factory_floor_publish.HandlerOperatorAccess.handle",
        lambda *_args, **_kwargs: ModelOperatorAccessResult(
            admitted_devices=1,
            include_sha256="a" * 64,
            include_changed=False,
            published=(),
            unread=(),
        ),
    )
    monkeypatch.setattr(HandlerReportsPublish, "handle", reports)
    assert main([]) == 2
    assert calls == ["reports"]
    assert "recorded required refresh failure" in capsys.readouterr().err
