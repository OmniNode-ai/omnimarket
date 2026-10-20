# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The landing controller's TLC approval receipt is built and installed by machine (OMN-17427).

Before this node a new decision pin in the controller's tree made ``refresh_install`` refuse
TLC-RECEIPT-MISSING until someone ran the 94-config model check by hand and copied a receipt into
the controller's state directory (about 25 hand installs in four days). ``publish`` builds that
receipt in CI with the controller's own gate and packs it; ``sync`` installs a published,
re-verified receipt where the installer looks (``g-act-<pin[:8]>`` under the landing state dir).

The landing controller's gate script is given by ``--gate-script``; ``STUB_GATE`` carries the entry
points the node calls with the rules cut down, and is written to a temporary directory so the node
loads it as it loads the real file.
"""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import tarfile
from collections.abc import Callable
from importlib.metadata import entry_points
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_landing_tlc_receipt_effect.handlers import (
    HandlerLandingTlcPublish,
    HandlerLandingTlcSync,
)
from omnimarket.nodes.node_landing_tlc_receipt_effect.handlers import (
    handler_landing_tlc_receipt_cli as cli,
)
from omnimarket.nodes.node_landing_tlc_receipt_effect.models import (
    ModelLandingTlcPublishRequest,
    ModelLandingTlcSyncRequest,
    ModelLandingTlcSyncResult,
)

pytestmark = pytest.mark.unit

PIN = "55bfbe86f144126795519eeb225eee3aa932e2e6"
OK = "Model checking completed. No error has been found.\n"
REPO = "OmniNode-ai/omnimarket"
STUB_GATE = r"""
import argparse, hashlib, json, sys
from importlib.metadata import entry_points
from pathlib import Path

def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def _load_receipt(path, label, reasons):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        reasons.append(f"{label} receipt unreadable: {exc}")
        return None

def _validate_receipt_shape(receipt, kind, label, reasons):
    ok = receipt.get("kind") == kind
    if not ok:
        reasons.append(f"{label} receipt kind is not {kind}")
    return ok

def _check_binding(receipt, label, t3_commit, controller_sha256, reasons):
    if receipt.get("t3_commit") != t3_commit:
        reasons.append(f"{label} receipt t3_commit does not match installed commit")

def _verified_evidence(receipt, evidence_dir, label, reasons):
    paths = []
    for entry in receipt["evidence"]:
        path = Path(evidence_dir) / entry["name"]
        if not path.is_file() or _sha(path) != entry["sha256"]:
            reasons.append(f"{label} evidence {entry['name']} sha256 mismatch")
            return None
        paths.append(path)
    return paths

def _verify_tlc_claims(receipt, paths, platform_marker, reasons):
    for path in paths:
        if path.suffix == ".out" and "No error has been found" not in path.read_text():
            reasons.append(f"tlc config {path.stem} does not meet its expectation")

def main(argv=None):
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build").add_subparsers(dest="builder", required=True)
    tlc = build.add_parser("tlc")
    for flag in ("--results-dir", "--model", "--t3-commit", "--host", "--out"):
        tlc.add_argument(flag, required=True)
    args = parser.parse_args(argv)
    outs = sorted(Path(args.results_dir).glob("*.out"))
    evidence = [{"name": p.name, "sha256": _sha(p)} for p in [*outs, Path(args.model)]]
    receipt = {"schema": "stub/1", "kind": "tlc", "t3_commit": args.t3_commit, "host": args.host,
               "evidence": evidence, "model_sha256": _sha(args.model)}
    Path(args.out).write_text(json.dumps(receipt, sort_keys=True) + "\n")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
"""


def _gate(tmp_path: Path) -> Path:
    path = tmp_path / "gate" / "gate.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(STUB_GATE)
    return path


def _inputs(tmp_path: Path, *, out_text: str = OK) -> ModelLandingTlcPublishRequest:
    results = tmp_path / "results"
    results.mkdir(parents=True, exist_ok=True)
    (results / "VW_lease.out").write_text(out_text)
    (results / "VW_block.out").write_text(OK)
    model = tmp_path / "LandingController.tla"
    model.write_text("---- MODULE LandingController ----\n")
    pin_file = tmp_path / "landing_decision.pin.json"
    pin_file.write_text(json.dumps({"commit": PIN, "repo": "OmniNode-ai/omnimarket"}))
    return ModelLandingTlcPublishRequest(
        gate_script=_gate(tmp_path),
        pin_file=pin_file,
        results_dir=results,
        model=model,
        host="test-platform",
        out_dir=tmp_path / "out",
    )


def _tarball_bytes(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _published(tmp_path: Path) -> tuple[str, bytes]:
    """(release tag, tarball) as a successful publish produced them."""
    result = HandlerLandingTlcPublish().handle(_inputs(tmp_path / "pub"))
    assert result.status == "published", result.reasons
    assert result.archive is not None
    return result.tag, result.archive.read_bytes()


def _fake_gh(
    releases: dict[str, bytes],
) -> Callable[..., subprocess.CompletedProcess[str]]:
    """``gh release list`` names the tags, newest first; ``download`` writes the tarball."""

    def run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        assert argv[:2] == ["gh", "release"], argv
        if argv[2] == "list":
            rows = [{"tagName": tag} for tag in releases]
            return subprocess.CompletedProcess(argv, 0, json.dumps(rows), "")
        assert argv[2] == "download"
        tag = argv[3]
        directory = Path(argv[argv.index("--dir") + 1])
        pin8 = tag.split("-")[2]
        (directory / f"g-act-{pin8}.tar.gz").write_bytes(releases[tag])
        return subprocess.CompletedProcess(argv, 0, "", "")

    return run


def _sync(
    tmp_path: Path, releases: dict[str, bytes]
) -> tuple[ModelLandingTlcSyncResult, Path]:
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    result = HandlerLandingTlcSync(run=_fake_gh(releases)).handle(
        ModelLandingTlcSyncRequest(
            gate_script=_gate(tmp_path), state_dir=state, repo=REPO
        )
    )
    return result, state


def test_publish_builds_a_receipt_bound_to_the_pin_and_packs_it(tmp_path: Path) -> None:
    result = HandlerLandingTlcPublish().handle(_inputs(tmp_path))
    assert result.status == "published", result.reasons
    model8 = hashlib.sha256(
        (tmp_path / "LandingController.tla").read_bytes()
    ).hexdigest()[:8]
    assert result.tag == f"landing-tlc-{PIN[:8]}-{model8}"
    assert result.archive == tmp_path / "out" / f"g-act-{PIN[:8]}.tar.gz"
    assert result.archive is not None
    with tarfile.open(result.archive) as archive:
        names = sorted(archive.getnames())
        receipt_file = archive.extractfile(f"g-act-{PIN[:8]}/tlc.json")
        assert receipt_file is not None
        receipt = json.loads(receipt_file.read())
    assert names == [
        f"g-act-{PIN[:8]}",
        f"g-act-{PIN[:8]}/LandingController.tla",
        f"g-act-{PIN[:8]}/VW_block.out",
        f"g-act-{PIN[:8]}/VW_lease.out",
        f"g-act-{PIN[:8]}/tlc.json",
    ]
    assert receipt["t3_commit"] == PIN
    assert receipt["host"] == "test-platform"


def test_publish_refuses_a_model_check_that_found_a_violation(tmp_path: Path) -> None:
    request = _inputs(tmp_path, out_text="Error: Invariant P1 is violated.\n")
    result = HandlerLandingTlcPublish().handle(request)
    assert result.status == "refused"
    assert any("VW_lease" in reason for reason in result.reasons)
    assert result.archive is None
    assert not (tmp_path / "out" / f"g-act-{PIN[:8]}.tar.gz").exists()


def test_publish_refuses_when_no_model_check_ran(tmp_path: Path) -> None:
    request = _inputs(tmp_path)
    for out in request.results_dir.glob("*.out"):
        out.unlink()
    result = HandlerLandingTlcPublish().handle(request)
    assert result.status == "refused"
    assert any("no .out" in reason for reason in result.reasons)


def test_publish_refuses_an_unreadable_pin(tmp_path: Path) -> None:
    request = _inputs(tmp_path)
    request.pin_file.write_text("{}")
    result = HandlerLandingTlcPublish().handle(request)
    assert result.status == "refused"
    assert any("pin" in reason for reason in result.reasons)


def test_sync_installs_a_published_receipt_where_the_installer_looks(
    tmp_path: Path,
) -> None:
    tag, tarball = _published(tmp_path)
    result, state = _sync(tmp_path, {tag: tarball})
    assert result.status == "synced"
    receipt = json.loads((state / f"g-act-{PIN[:8]}" / "tlc.json").read_text())
    assert receipt["t3_commit"] == PIN
    assert (state / f"g-act-{PIN[:8]}" / "VW_lease.out").read_text() == OK
    assert [p.name for p in state.iterdir()] == [f"g-act-{PIN[:8]}"]


def test_sync_leaves_a_current_receipt_alone(tmp_path: Path) -> None:
    tag, tarball = _published(tmp_path)
    first, state = _sync(tmp_path, {tag: tarball})
    assert first.status == "synced"
    marker = state / f"g-act-{PIN[:8]}" / "marker"
    marker.write_text("kept")
    second, _ = _sync(tmp_path, {tag: tarball})
    assert second.status == "current"
    assert marker.read_text() == "kept"


def test_sync_replaces_a_receipt_built_from_another_model(tmp_path: Path) -> None:
    tag, tarball = _published(tmp_path)
    state = tmp_path / "state"
    old = state / f"g-act-{PIN[:8]}"
    old.mkdir(parents=True)
    (old / "tlc.json").write_text(
        json.dumps({"t3_commit": PIN, "model_sha256": "0" * 64})
    )
    result, _ = _sync(tmp_path, {tag: tarball})
    assert result.status == "synced"
    assert json.loads((old / "tlc.json").read_text())["model_sha256"] != "0" * 64


def test_sync_refuses_a_receipt_the_gate_refuses_and_changes_nothing(
    tmp_path: Path,
) -> None:
    tag, tarball = _published(tmp_path)
    with tarfile.open(fileobj=io.BytesIO(tarball)) as archive:
        members = {}
        for member in archive.getmembers():
            if member.isfile():
                extracted = archive.extractfile(member)
                assert extracted is not None
                members[member.name] = extracted.read()
    members[f"g-act-{PIN[:8]}/VW_lease.out"] = b"Error: Invariant P1 is violated.\n"
    result, state = _sync(tmp_path, {tag: _tarball_bytes(members)})
    assert result.status == "refused"
    assert list(state.iterdir()) == []


def test_sync_refuses_an_archive_that_names_a_path_outside_its_directory(
    tmp_path: Path,
) -> None:
    tag = f"landing-tlc-{PIN[:8]}-00000000"
    result, state = _sync(tmp_path, {tag: _tarball_bytes({"../escape": b"x"})})
    assert result.status == "refused"
    assert list(state.iterdir()) == []
    assert not (tmp_path / "escape").exists()


def test_sync_reports_a_failed_release_read_and_changes_nothing(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()

    def broken(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 1, "", "HTTP 401")

    result = HandlerLandingTlcSync(run=broken).handle(
        ModelLandingTlcSyncRequest(
            gate_script=_gate(tmp_path), state_dir=state, repo=REPO
        )
    )
    assert result.status == "failed"
    assert "HTTP 401" in " ".join(result.reasons)
    assert list(state.iterdir()) == []


def test_cli_publish_prints_the_tag_it_will_release_under(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    request = _inputs(tmp_path)
    code = cli.main(
        [
            "publish",
            "--gate-script",
            str(request.gate_script),
            "--pin-file",
            str(request.pin_file),
            "--results-dir",
            str(request.results_dir),
            "--model",
            str(request.model),
            "--host",
            "test-platform",
            "--out-dir",
            str(request.out_dir),
        ]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert out.startswith("landing_tlc_receipt=published tag=landing-tlc-55bfbe86-")


def test_cli_publish_exits_nonzero_on_a_refusal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    request = _inputs(tmp_path, out_text="Error: Invariant P1 is violated.\n")
    code = cli.main(
        [
            "publish",
            "--gate-script",
            str(request.gate_script),
            "--pin-file",
            str(request.pin_file),
            "--results-dir",
            str(request.results_dir),
            "--model",
            str(request.model),
            "--host",
            "test-platform",
            "--out-dir",
            str(request.out_dir),
        ]
    )
    assert code == 1
    assert capsys.readouterr().out.startswith("landing_tlc_receipt=refused")


def test_registered_node_declares_the_completion_topic() -> None:
    entry = entry_points(group="onex.nodes")["node_landing_tlc_receipt_effect"]
    module = entry.load()
    contract_path = Path(module.__file__).with_name("contract.yaml")
    contract = yaml.safe_load(contract_path.read_text())
    assert (
        contract["terminal_event"]
        == "onex.evt.omnimarket.landing-tlc-receipt-completed.v1"
    )
    assert contract["terminal_event"] in contract["externally_consumed_topics"]
    assert isinstance(contract["runtime_dispatch"]["external_trigger"], str)
    assert contract["runtime_dispatch"]["external_trigger"].strip()
    assert contract["metadata"]["transport_type"] == "local"
    metadata = yaml.safe_load(contract_path.with_name("metadata.yaml").read_text())
    assert metadata["entry_points"]["onex.nodes"][entry.name] == entry.value
