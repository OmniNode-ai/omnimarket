# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""beta_board: the golden chain event (every reading in, the page out on every host) and the error chain events (OMN-23315 AC1, AC2)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_beta_board_effect.handlers.handler_beta_board import (
    OWN_CONTRACT,
    HandlerBetaBoard,
    load_runtime,
)
from omnimarket.nodes.node_beta_board_effect.models.model_beta_board import (
    ModelBetaBoard,
    ModelBetaBoardRequest,
)
from omnimarket.nodes.node_beta_board_effect.runtime_beta_board import main
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import ModelLedgerRead
from tests.nodes.node_beta_board_effect.support import (
    NOW,
    OVERLAY,
    ago,
    run_list_answer,
    runtime,
)

LEDGER_ROW = "2026-11-09T11:55:00Z | STATUS | lane=a | src=thing | verdict=WORKING | probed_at=2026-11-09T11:55:00Z"


def make_home(tmp_path: Path) -> Path:
    return tmp_path


class Fakes:
    """A scripted run-list answer per workflow and a recording copier that can refuse chosen hosts."""

    def __init__(
        self, answers: dict[str, tuple[int, str, str]], refuse: set[str] | None = None
    ) -> None:
        self.answers = answers
        self.refuse = refuse or set()
        self.copies: list[tuple[str, str, bytes]] = []

    def run(self, argv, timeout):
        return self.answers[argv[5]]

    def copy(self, argv, payload, timeout):
        host, command = argv[-2], argv[-1]
        if host in self.refuse:
            return 255, "ssh: connect to host failed"
        name = (
            command.split("&& mv -f ")[1]
            .split()[1]
            .rstrip(";")
            .rsplit("/", 1)[-1]
            .strip("'")
        )
        self.copies.append((host, name, payload))
        return 0, ""


def passing() -> dict[str, tuple[int, str, str]]:
    ok = run_list_answer([("completed", "success", ago(minutes=10))])
    return {"local.yml": (0, ok, ""), "cloud.yml": (0, ok, "")}


def handler(fakes: Fakes, ledger=None) -> HandlerBetaBoard:
    return HandlerBetaBoard(
        runtime(),
        run=fakes.run,
        copy=fakes.copy,
        clock=lambda: NOW,
        ledger=ledger if ledger is not None else InMemoryLedger([LEDGER_ROW]),
    )


def test_beta_board_golden_chain_reads_computes_writes_and_publishes_to_every_host(
    tmp_path: Path,
) -> None:
    home = make_home(tmp_path)
    fakes = Fakes(passing())
    result = handler(fakes).handle(ModelBetaBoardRequest(stage="run", home=str(home)))
    assert result.ok
    assert result.stage == "run"
    assert result.counts["pass"] == 2
    assert result.counts["no_reading"] == 1
    board = ModelBetaBoard.model_validate_json(
        (home / runtime().output_relpath).read_text()
    )
    assert {c.id: c.state for c in board.criteria} == {
        "T1": "pass",
        "T2": "pass",
        "T3": "no_reading",
    }
    assert sorted((h, n) for h, n, _ in fakes.copies) == [
        ("alias-a", "beta-board.json"),
        ("alias-a", "beta.html"),
        ("alias-b", "beta-board.json"),
        ("alias-b", "beta.html"),
    ]
    page = next(
        p for h, n, p in fakes.copies if h == "alias-b" and n == "beta.html"
    ).decode()
    for cid in ("T1", "T2", "T3"):
        assert f'data-criterion="{cid}"' in page
    assert 'data-state="pass"' in page
    assert "OMN-3" in page
    assert "Org/cloudrepo" in page
    assert (home / runtime().reads_relpath).is_file()
    assert result.targets
    assert all(t.ok for t in result.targets)


def test_beta_board_page_names_state_since_age_owner_and_the_cloud_probes(
    tmp_path: Path,
) -> None:
    home = make_home(tmp_path)
    fakes = Fakes(passing())
    handler(fakes).handle(ModelBetaBoardRequest(stage="run", home=str(home)))
    page = next(p for h, n, p in fakes.copies if n == "beta.html").decode()
    row = page.split('data-criterion="T2"')[1].split("</tr>")[0]
    assert "PASS" in row
    assert "2026-11-09 11:55:00Z" in row
    assert "OMN-3" in row
    assert 'data-age-seconds="' in row
    assert "Probes still read from a cloud repository (1)" in page
    assert 'data-probe="probe-cloud"' in page
    assert 'data-probe="probe-local"' not in page


def test_beta_board_error_chain_a_probe_that_cannot_be_read_draws_unread_and_the_board_is_not_met(
    tmp_path: Path,
) -> None:
    home = make_home(tmp_path)
    answers = passing()
    answers["cloud.yml"] = (1, "", "gateway down")
    fakes = Fakes(answers)
    result = handler(fakes).handle(ModelBetaBoardRequest(stage="run", home=str(home)))
    assert result.ok
    assert result.met is False
    board = ModelBetaBoard.model_validate_json(
        (home / runtime().output_relpath).read_text()
    )
    t2 = next(c for c in board.criteria if c.id == "T2")
    assert t2.state == "unread"
    assert "gateway down" in t2.reason


def test_beta_board_error_chain_one_unreachable_host_is_named_and_does_not_stop_the_others(
    tmp_path: Path,
) -> None:
    home = make_home(tmp_path)
    fakes = Fakes(passing(), refuse={"alias-a"})
    result = handler(fakes).handle(ModelBetaBoardRequest(stage="run", home=str(home)))
    assert result.ok is False
    by_name = {t.name: t for t in result.targets}
    assert by_name["host-a"].ok is False
    assert "exit 255" in by_name["host-a"].detail
    assert by_name["host-b"].ok is True
    assert {h for h, _, _ in fakes.copies} == {"alias-b"}


def test_beta_board_error_chain_unreadable_ledger_leaves_its_criterion_unread(
    tmp_path: Path,
) -> None:
    class Broken:
        def read(self, query):
            return ModelLedgerRead(error="read-command-exit-3")

    result = handler(Fakes(passing()), ledger=Broken()).handle(
        ModelBetaBoardRequest(stage="build", home=str(tmp_path))
    )
    board = ModelBetaBoard.model_validate_json(
        (tmp_path / runtime().output_relpath).read_text()
    )
    t2 = next(c for c in board.criteria if c.id == "T2")
    assert result.ok
    assert t2.state == "unread"
    assert "read-command-exit-3" in t2.reason


def test_beta_board_error_chain_publish_without_a_board_refuses_by_name(
    tmp_path: Path,
) -> None:
    result = handler(Fakes(passing())).handle(
        ModelBetaBoardRequest(stage="publish", home=str(tmp_path))
    )
    assert result.ok is False
    assert "no board to publish" in result.detail


def test_beta_board_error_chain_no_registry_home_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OMNI_HOME", raising=False)
    result = handler(Fakes(passing())).handle(ModelBetaBoardRequest(stage="build"))
    assert result.ok is False
    assert "OMNI_HOME" in result.detail


def test_beta_board_error_chain_no_overlay_refuses_and_names_the_setting() -> None:
    with pytest.raises(
        NodeOverlayError, match=r"ONEX_BETA_BOARD_OVERLAY|node_beta_board_effect"
    ):
        load_runtime(OWN_CONTRACT, environ={})


def test_beta_board_command_line_reads_the_overlay_file_the_contract_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    import yaml

    overlay_file = tmp_path / "overlay.yaml"
    overlay_file.write_text(yaml.safe_dump(OVERLAY))
    monkeypatch.setenv("ONEX_BETA_BOARD_OVERLAY", str(overlay_file))
    monkeypatch.setenv("OMNI_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(
        "omnimarket.nodes.node_beta_board_effect.handlers.adapter_beta_board_sources.run_command",
        lambda _argv, _timeout: (1, "", "no gateway in the test"),
    )
    code = main(["build"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0
    assert out["stage"] == "build"
    assert out["counts"]["unread"] == 2
