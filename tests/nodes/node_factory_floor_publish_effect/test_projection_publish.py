# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read the computed projection through HTTP after the publisher's shell writes."""

from __future__ import annotations

import hashlib
import json
from functools import partial
from http.client import HTTPConnection
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from threading import Thread

import pytest

from omnimarket.models.model_factory_floor_input import (
    ModelFloorManifest,
    ModelFloorManifestTask,
    ModelFloorPlanInput,
)
from omnimarket.nodes.node_factory_floor_compute.handlers.handler_factory_floor import (
    compute_factory_floor,
)
from omnimarket.nodes.node_factory_floor_publish_effect.handlers.handler_factory_floor_publish import (
    HandlerFactoryFloorPublish,
    load_runtime,
)
from omnimarket.nodes.node_factory_floor_publish_effect.models.model_factory_floor_publish import (
    ModelFactoryFloorPublishOverlay,
    ModelFactoryFloorPublishRequest,
)
from tests.nodes.node_factory_floor_compute.support import (
    NOW,
    pr,
    proven,
    request,
    running_on_dev,
)
from tests.nodes.node_factory_floor_publish_effect.limits_support import TEST_LIMITS
from tests.nodes.node_factory_floor_publish_effect.test_factory_floor_publish import (
    LocalSsh,
)

pytestmark = pytest.mark.unit


def test_published_projection_http_readback(tmp_path: Path) -> None:
    """Local shell transport and an actual HTTP server, without touching a deployed site."""
    plan = ModelFloorPlanInput(
        plan_path="beta/plans/2026-11-02-projection-plan.md",
        manifest=ModelFloorManifest(
            tasks=(
                ModelFloorManifestTask(id="W1", title="Merged", prs=("repo#1",)),
                ModelFloorManifestTask(id="W2", title="Pending"),
                ModelFloorManifestTask(id="G1", title="Verify", kind="gate"),
            )
        ),
    )
    projection = compute_factory_floor(
        request(
            plans=(plan,),
            prs=(pr("repo#1", "MERGED"),),
            ledger=proven("repo#1"),
            running_evidence=running_on_dev("repo#1"),
            selection=("projection-plan", "unread-plan"),
        )
    )
    data = projection.model_dump_json().encode("utf-8")
    home, site = tmp_path / "home", tmp_path / "site"
    source = home / load_runtime().source.relpath
    source.parent.mkdir(parents=True)
    source.write_bytes(data)

    with HTTPServer(
        ("127.0.0.1", 0), partial(SimpleHTTPRequestHandler, directory=str(site))
    ) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        try:
            result = HandlerFactoryFloorPublish(LocalSsh()).handle(
                ModelFactoryFloorPublishRequest(
                    home=str(home),
                    skip_refresh=True,
                    now=NOW,
                    overlay=ModelFactoryFloorPublishOverlay(
                        limits=TEST_LIMITS,
                        ssh_target="local-shell",
                        remote_dir=str(site),
                        serve_url=f"http://127.0.0.1:{server.server_port}/",
                        bus_lane="projection-readback",
                        history_root=str(tmp_path / "history"),
                        owner_login="owner@example.invalid",
                    ),
                ),
                environ={},
            )
            connection.request("GET", "/factory-floor.json")
            response = connection.getresponse()
            served = response.read()
            assert response.status == 200
        finally:
            connection.close()
            server.shutdown()
            thread.join(timeout=5)

    assert served == data
    assert hashlib.sha256(served).hexdigest() == result.data_sha256
    lines = json.loads(served)["lines"]
    assert [line["source"] for line in lines] == ["manifest", "undrawn"]
    for line in lines:
        assert {"progress", "gates", "condition", "data_age"} <= line.keys()
        assert line["data_age"]["read_at"] == "2026-10-31T11:57:00Z"
        assert line["data_age"]["age_min"] == 3
    assert lines[0]["progress"]["completed"] == 1
    assert lines[0]["progress"]["total"] == 2
    assert lines[0]["progress"]["percent"] == 50.0
    assert lines[0]["gates"]["waiting"] == 1
    assert lines[0]["condition"] != "complete"
    assert lines[1]["progress"]["state"] == "unknown"
    assert lines[1]["progress"]["percent"] is None
    assert lines[1]["condition"] == "stopped"


def test_published_site_pages_carry_memory_used_label(tmp_path: Path) -> None:
    """AC66: the pages one publish puts on the site carry the Memory used label."""
    projection = compute_factory_floor(request())
    home, site = tmp_path / "home", tmp_path / "site"
    source = home / load_runtime().source.relpath
    source.parent.mkdir(parents=True)
    source.write_bytes(projection.model_dump_json().encode("utf-8"))

    result = HandlerFactoryFloorPublish(LocalSsh()).handle(
        ModelFactoryFloorPublishRequest(
            home=str(home),
            skip_refresh=True,
            now=NOW,
            overlay=ModelFactoryFloorPublishOverlay(
                limits=TEST_LIMITS,
                ssh_target="local-shell",
                remote_dir=str(site),
                serve_url="http://127.0.0.1:1/",
                bus_lane="projection-readback",
                history_root=str(tmp_path / "history"),
                owner_login="owner@example.invalid",
            ),
        ),
        environ={},
    )

    pages = sorted(str(path.relative_to(site)) for path in site.rglob("*.html"))
    assert pages == [
        "compare.html",
        "delegation.html",
        "detail.html",
        "floor/index.html",
        "index.html",
    ], (
        pages,
        result.published,
    )
    # The address shows the simple floor (OMN-19998); the pages with every figure are the detail page and the floor page.
    for page in ("detail.html", "floor/index.html"):
        assert "Memory used" in (site / page).read_text(encoding="utf-8"), page
