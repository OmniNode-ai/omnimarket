# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC2 (OMN-23865): every host, volume, class root, topic and the schedule come from the overlay; no default path."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_lab_disk_watermark_effect import (
    handlers as handlers_package,
)
from omnimarket.nodes.node_lab_disk_watermark_effect.handlers.host_disk_ports import (
    InMemoryHostDiskPublisher,
)
from omnimarket.nodes.node_lab_disk_watermark_effect.models.model_host_disk_reading import (
    ModelHostDiskRead,
)
from omnimarket.nodes.node_lab_disk_watermark_effect.models.model_lab_disk_watermark import (
    ModelLabDiskWatermarkRequest,
)
from omnimarket.nodes.node_lab_disk_watermark_effect.runtime_lab_disk_watermark import (
    main,
)

from .host_disk_inputs import (
    FAILED_TOPIC,
    READ_TOPIC,
    handler,
    make_host,
    overlay_document,
    write_overlay,
)

pytestmark = pytest.mark.unit
NODE = Path(handlers_package.__file__).resolve().parent.parent


def test_a_class_root_missing_from_the_overlay_is_an_unread_class_never_a_default_path(
    tmp_path: Path,
) -> None:
    paths = make_host(tmp_path)
    overlay = write_overlay(
        tmp_path, overlay_document(paths, classes=("worktrees",), docker=False)
    )
    publisher = InMemoryHostDiskPublisher()
    result = handler(tmp_path, publisher).handle(
        ModelLabDiskWatermarkRequest(host_name="pool-a", overlay_file=str(overlay))
    )
    assert isinstance(result.host_disk, ModelHostDiskRead)
    by_name = {c.class_name: c for c in result.host_disk.classes}
    for name in ("uv_cache", "lab_run"):
        assert by_name[name].status == "unread"
        assert by_name[name].unread_reason == "class root not declared in the overlay"
        assert (by_name[name].total_bytes, by_name[name].groups) == (0, ())
    for name in (
        "docker_images",
        "docker_build_cache",
        "docker_volumes",
        "docker_exited_containers",
    ):
        assert (
            by_name[name].unread_reason == "docker reader not declared in the overlay"
        )
    assert by_name["worktrees"].status == "read"
    # the unread classes are still published: the projection keeps them as unread, it does not drop them
    assert [topic for topic, _ in publisher.published] == [READ_TOPIC]


def test_a_declared_root_that_is_not_there_is_unread_with_its_reason(
    tmp_path: Path,
) -> None:
    paths = make_host(tmp_path)
    document = overlay_document(paths, docker=False)
    document["hosts"][0]["class_roots"][1]["path"] = str(tmp_path / "gone")  # type: ignore[index]
    overlay = write_overlay(tmp_path, document)
    result = handler(tmp_path, InMemoryHostDiskPublisher()).handle(
        ModelLabDiskWatermarkRequest(host_name="pool-a", overlay_file=str(overlay))
    )
    assert isinstance(result.host_disk, ModelHostDiskRead)
    worktrees = next(c for c in result.host_disk.classes if c.class_name == "worktrees")
    assert worktrees.status == "unread"
    assert "unreadable (FileNotFoundError)" in worktrees.unread_reason


def test_no_handler_source_holds_a_path_or_host_literal() -> None:
    offenders: list[str] = []
    for name in (
        "handler_host_disk_classes.py",
        "adapter_host_disk_bus.py",
        "host_disk_ports.py",
    ):
        tree = ast.parse((NODE / "handlers" / name).read_text())
        offenders += [
            f"{name}:{node.lineno}: {node.value!r}"
            for node in ast.walk(tree)
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and (len(node.value) > 1 and node.value.startswith(("/", "~", "$HOME")))
            )
            or (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node.value[:1] == "h"
                and node.value[1:].isdigit()
            )
        ]
    assert offenders == []


def test_the_overlay_topics_are_the_topics_the_contract_publishes() -> None:
    contract = yaml.safe_load((NODE / "contract.yaml").read_text())
    assert contract["event_bus"]["publish_topics"] == [READ_TOPIC, FAILED_TOPIC]


def test_the_install_takes_its_interval_from_the_overlay(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    paths = make_host(tmp_path)
    overlay = write_overlay(tmp_path, overlay_document(paths, interval_seconds=900))
    python = tmp_path / "python"
    python.write_text("")
    code = main(
        [
            "schedule",
            "install",
            "--host",
            "pool-a",
            "--python",
            str(python),
            "--source-root",
            str(tmp_path),
            "--overlay",
            str(overlay),
            "--dry-run",
        ]
    )
    assert code == 0
    assert "OnUnitActiveSec=900s" in capsys.readouterr().out
    contract_interval = yaml.safe_load((NODE / "contract.yaml").read_text())[
        "schedule"
    ]["cadence"]["interval_seconds"]
    assert contract_interval != 900


def test_an_overlay_with_an_unknown_class_root_name_is_refused(tmp_path: Path) -> None:
    paths = make_host(tmp_path)
    document = overlay_document(paths)
    document["hosts"][0]["class_roots"][0]["name"] = "downloads"  # type: ignore[index]
    overlay = write_overlay(tmp_path, document)
    with pytest.raises(ValueError, match=r"class_roots\.0\.name"):
        handler(tmp_path, InMemoryHostDiskPublisher()).handle(
            ModelLabDiskWatermarkRequest(host_name="pool-a", overlay_file=str(overlay))
        )
