# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21469: `onex dashboard` serves the seven-page OmniDash with one Settings page.

omnidash v1.1.11 is the first release carrying the seven-page redesign
(OmniNode-ai/omnidash#465 to #470): the primary navigation reads Overview, Runs,
Workflow, Usage and Settings, with Model providers, Access and System status
under Settings. v1.1.10 predates all of it, so pinning it serves the old pages
while every server-side test stays green.

AC2, the release itself, is checked against v1.1.11 by name, so these cases keep
proving that release after the pin later moves on:

* the tag peels to omnidash#471's merge commit, the version-bump change;
* the release carries both the bundle and its ``.sha256`` sidecar, the bundle
  verified by ``ensure_bundle`` and the sidecar naming that digest and asset;
* the publish-dashboard-bundle run for the tag concluded success.

AC3, the pin, is checked from the pin: its floor, and the pinned asset fetched
and verified through ``ensure_bundle`` (the path `onex dashboard` serves from)
with the Settings entry present in the navigation list itself, not merely the
word, which the bundle also uses in its route map and page heading.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
from pathlib import Path
from typing import cast

import pytest
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

from omnimarket.github_api import download_release_asset
from omnimarket.models.model_github_gateway_io import ModelWorkflowRunsResult
from omnimarket.nodes.node_github_repo_gateway_effect.dispatcher import dispatch
from omnimarket.nodes.node_github_repo_gateway_effect.models.model_gateway_io import (
    EnumGithubGatewayOperation,
    ModelGithubGatewayRequest,
)
from omnimarket.nodes.node_local_dashboard_serve_effect.bundle import (
    BundlePin,
    ensure_bundle,
    load_pin,
)
from tests.nodes.node_github_repo_gateway_effect.recorded_transport import (
    JsonExchange,
    RecordedGatewayTransport,
    get,
    json_response,
)

_REPOSITORY = "OmniNode-ai/omnidash"
_RELEASE = "v1.1.11"
_RELEASE_SHA256 = "e231574dab6f3a7441ab251ae3f4c82439d128efe8b02d11a6c9b8af80f14874"
_RELEASE_ASSET = f"omnidash-bundle-{_RELEASE}.tar.gz"
# The merge commit of the version-bump change the tag is cut on (omnidash#471).
_BUMP_MERGE_COMMIT = "3149cd09f4da87094901fa787a287f9dba23db74"

# The first omnidash release with the seven-page navigation and one Settings page.
_FIRST_SETTINGS_PAGE_RELEASE = (1, 1, 11)

# The Settings entry of the primary navigation list as the built JavaScript
# spells it. The route map ("local-settings":"Settings") and the page heading
# (children:"Settings") do not match, so removing the entry fails this case.
_SETTINGS_NAV_ENTRY = re.compile(r'\{page:"local-settings",label:"Settings"')


def _release(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.removeprefix("v").split("."))


def _carries_settings_nav_entry(root: Path) -> bool:
    return any(
        _SETTINGS_NAV_ENTRY.search(script.read_text(encoding="utf-8", errors="replace"))
        for script in sorted(root.rglob("*.js"))
    )


# The publish-dashboard-bundle run listing for the tag, recorded from the
# repository gateway read node, so the check reads without the public endpoint's
# rate limits. It is replayed through the node's own read, so the recorded shape
# is parsed by the code that serves it.
_PUBLISH_RUNS_FIXTURE = (
    Path(__file__).parents[1]
    / "fixtures"
    / "dashboard_bundle"
    / "omnidash_publish_runs_v1_1_11.json"
)
_PUBLISH_WORKFLOW = "publish-dashboard-bundle.yml"


def _publish_runs(body: dict[str, object]) -> ModelWorkflowRunsResult:
    request = ModelGithubGatewayRequest.model_validate(
        {
            "operation": EnumGithubGatewayOperation.WORKFLOW_RUNS,
            "repo": _REPOSITORY,
            "workflow": _PUBLISH_WORKFLOW,
            "branch": _RELEASE,
            "event": "push",
        }
    )
    path = (
        f"/repos/{_REPOSITORY}/actions/workflows/{_PUBLISH_WORKFLOW}/runs"
        f"?branch={_RELEASE}&event=push&per_page=30"
    )
    transport = RecordedGatewayTransport([JsonExchange(get(path), json_response(body))])
    result = dispatch(request, transport)
    transport.assert_drained()
    assert isinstance(result, ModelWorkflowRunsResult)
    return result


def _recorded_publish_body() -> dict[str, object]:
    return json.loads(_PUBLISH_RUNS_FIXTURE.read_text(encoding="utf-8"))


@pytest.mark.unit
def test_the_pinned_bundle_is_the_seven_page_release() -> None:
    pin = load_pin()
    assert _release(pin.version) >= _FIRST_SETTINGS_PAGE_RELEASE
    assert pin.asset == f"omnidash-bundle-{pin.version}.tar.gz"


@pytest.mark.integration
def test_the_pinned_asset_verifies_and_lists_settings_in_its_navigation(
    tmp_path: Path,
) -> None:
    root = ensure_bundle(load_pin(), cache_root=tmp_path)
    assert (root / "index.html").is_file()
    assert _carries_settings_nav_entry(root), (
        "the pinned bundle's navigation has no Settings entry"
    )


@pytest.mark.integration
def test_the_release_tag_peels_to_the_bump_merge_commit() -> None:
    listing = subprocess.run(
        [
            "git",
            "ls-remote",
            f"https://github.com/{_REPOSITORY}.git",
            f"refs/tags/{_RELEASE}^{{}}",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
        env=scrub_git_location_env(os.environ),
    ).stdout.split()
    assert listing[:1] == [_BUMP_MERGE_COMMIT]


@pytest.mark.integration
def test_the_release_carries_the_bundle_and_its_sidecar(tmp_path: Path) -> None:
    pin = BundlePin(
        version=_RELEASE,
        sha256=_RELEASE_SHA256,
        asset=_RELEASE_ASSET,
        repository=_REPOSITORY,
    )
    assert (ensure_bundle(pin, cache_root=tmp_path / "cache") / "index.html").is_file()
    sidecar = tmp_path / f"{_RELEASE_ASSET}.sha256"
    download_release_asset(f"{pin.url}.sha256", sidecar)
    assert sidecar.read_text(encoding="utf-8").split() == [
        _RELEASE_SHA256,
        _RELEASE_ASSET,
    ]


@pytest.mark.unit
def test_the_publish_run_for_the_tag_succeeded() -> None:
    assert _publish_runs(_recorded_publish_body()).succeeded_at(_BUMP_MERGE_COMMIT), (
        f"no successful publish-dashboard-bundle run for {_RELEASE}"
    )


@pytest.mark.unit
def test_the_publish_run_check_reads_the_recorded_fixture_without_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the publish-run check opened a network connection")

    monkeypatch.setattr(socket.socket, "connect", _refuse)
    assert _publish_runs(_recorded_publish_body()).succeeded_at(_BUMP_MERGE_COMMIT)


@pytest.mark.unit
def test_the_publish_run_check_rejects_a_wrong_run() -> None:
    body = _recorded_publish_body()
    runs = cast(list[dict[str, object]], body["workflow_runs"])
    assert not _publish_runs(body).succeeded_at("0" * 40)
    failed = {**body, "workflow_runs": [{**runs[0], "conclusion": "failure"}]}
    assert not _publish_runs(failed).succeeded_at(_BUMP_MERGE_COMMIT)
    assert not _publish_runs({**body, "workflow_runs": []}).succeeded_at(
        _BUMP_MERGE_COMMIT
    )
