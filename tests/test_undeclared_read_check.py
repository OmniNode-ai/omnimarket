# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A change that adds a read of a setting no overlay declares is refused (OMN-26424, AC7).

The check judges the ADDED lines of a unified diff against the overlay documents: an environment variable read in Python or
shell, a repository variable of a workflow, an image build argument. Each failing control adds an undeclared read and expects
the refusal; each positive control proves the check sees a read at all, so a pass is not an empty scan.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_undeclared_read_check_compute.__main__ import (
    EXIT_OK,
    EXIT_REFUSED,
    EXIT_UNREAD,
    main,
)
from omnimarket.nodes.node_undeclared_read_check_compute.handlers import (
    HandlerUndeclaredReadCheck,
)
from omnimarket.nodes.node_undeclared_read_check_compute.models import (
    EnumReadKind,
    ModelUndeclaredReadCheckRequest,
)

pytestmark = pytest.mark.unit

REGISTER = {
    "schema_version": 1,
    "declared_env": ["host:* ONEX_DECLARED_ONE=value"],
    "declared_repo_variables": ["OmniNode-ai/omnimarket DECLARED_FLAG=true"],
    "declared_build_args": [
        "OmniNode-ai/omnibase_infra:docker/Dockerfile.runtime DECLARED_ARG=x"
    ],
    "declared_broker_settings": ["lane:lane-a enable_sasl=true"],
    "platform_env_names": ["HOME", "CI"],
    "platform_env_prefixes": ["GITHUB_"],
}
DOCUMENTS = {
    "declared_settings": REGISTER,
    "node_other_effect": {"journal_env": "ONEX_NAMED_IN_ANOTHER_OVERLAY"},
}


def diff(path: str, *added: str, removed: tuple[str, ...] = ()) -> str:
    body = "".join(f"-{line}\n" for line in removed) + "".join(
        f"+{line}\n" for line in added
    )
    return (
        f"diff --git a/{path} b/{path}\nindex 1111111..2222222 100644\n--- a/{path}\n+++ b/{path}\n"
        f"@@ -10,{len(removed)} +10,{len(added)} @@ def f():\n{body}"
    )


def judge(text: str, documents: dict[str, object] | None = None):
    request = ModelUndeclaredReadCheckRequest(
        diff_text=text, overlay_documents=DOCUMENTS if documents is None else documents
    )
    return HandlerUndeclaredReadCheck().handle(request)


def test_an_added_undeclared_python_env_read_is_refused() -> None:
    result = judge(
        diff("src/omnimarket/x.py", '    value = os.environ["ONEX_UNDECLARED_NEW"]')
    )
    assert result.refused
    assert [(r.name, r.kind, r.line) for r in result.undeclared] == [
        ("ONEX_UNDECLARED_NEW", EnumReadKind.ENV, 10)
    ]


@pytest.mark.parametrize(
    "line",
    [
        '    a = os.environ.get("ONEX_UNDECLARED_NEW", "")',
        '    a = os.getenv("ONEX_UNDECLARED_NEW")',
        '    a = environ.get("ONEX_UNDECLARED_NEW")',
    ],
)
def test_each_python_read_form_is_refused(line: str) -> None:
    assert judge(diff("src/omnimarket/x.py", line)).refused


def test_an_added_undeclared_shell_read_is_refused() -> None:
    assert judge(
        diff("scripts/run.sh", 'echo "${ONEX_UNDECLARED_NEW:-fallback}"')
    ).refused


def test_an_added_undeclared_repository_variable_is_refused() -> None:
    result = judge(
        diff(
            ".github/workflows/ci.yml",
            "        if: ${{ vars.UNDECLARED_FLAG == 'true' }}",
        )
    )
    assert [(r.name, r.kind) for r in result.undeclared] == [
        ("UNDECLARED_FLAG", EnumReadKind.REPO_VARIABLE)
    ]


def test_an_added_undeclared_build_argument_is_refused() -> None:
    result = judge(diff("docker/Dockerfile.runtime", "ARG UNDECLARED_ARG="))
    assert [(r.name, r.kind) for r in result.undeclared] == [
        ("UNDECLARED_ARG", EnumReadKind.BUILD_ARG)
    ]


def test_a_read_the_register_declares_is_seen_and_admitted() -> None:
    """Positive control: the read is found (so an empty scan cannot pass here) and is declared."""
    result = judge(diff("src/omnimarket/x.py", 'a = os.environ["ONEX_DECLARED_ONE"]'))
    assert len(result.reads) == 1
    assert not result.refused


def test_each_declared_kind_is_admitted() -> None:
    for path, line in (
        (".github/workflows/ci.yml", "if: ${{ vars.DECLARED_FLAG }}"),
        ("docker/Dockerfile.runtime", "ARG DECLARED_ARG"),
        ("src/omnimarket/y.py", 'os.getenv("enable_sasl")'),
    ):
        result = judge(diff(path, line))
        assert len(result.reads) == 1, (path, line)
        assert not result.refused, (path, line)


def test_a_name_another_overlay_holds_as_a_value_is_declared() -> None:
    assert not judge(
        diff("src/omnimarket/x.py", 'os.environ["ONEX_NAMED_IN_ANOTHER_OVERLAY"]')
    ).refused


def test_a_platform_name_and_prefix_are_declared_by_the_register() -> None:
    assert not judge(
        diff("src/omnimarket/x.py", 'os.environ["HOME"]', 'os.environ["GITHUB_SHA"]')
    ).refused


def test_a_removed_or_context_line_is_never_a_new_read() -> None:
    text = diff(
        "src/omnimarket/x.py",
        "x = 1",
        removed=('a = os.environ["ONEX_UNDECLARED_NEW"]',),
    )
    assert judge(text).reads == ()


def test_a_test_or_fixture_path_is_out_of_scope() -> None:
    assert (
        judge(diff("tests/unit/test_x.py", 'os.environ["ONEX_UNDECLARED_NEW"]')).reads
        == ()
    )
    assert (
        judge(
            diff("src/omnimarket/fixtures/x.py", 'os.environ["ONEX_UNDECLARED_NEW"]')
        ).reads
        == ()
    )


def test_a_comment_line_is_not_a_read() -> None:
    assert (
        judge(diff("src/omnimarket/x.py", '# os.environ["ONEX_UNDECLARED_NEW"]')).reads
        == ()
    )


def test_the_line_number_follows_the_hunk() -> None:
    text = diff("src/omnimarket/x.py", "x = 1", 'y = os.environ["ONEX_UNDECLARED_NEW"]')
    assert [r.line for r in judge(text).undeclared] == [11]


def test_with_no_register_every_read_is_undeclared() -> None:
    result = judge(
        diff("src/omnimarket/x.py", 'a = os.environ["ONEX_DECLARED_ONE"]'), documents={}
    )
    assert result.refused
    assert not result.register_present


def _roots(tmp_path: Path) -> str:
    for name, document in DOCUMENTS.items():
        directory = tmp_path / name
        directory.mkdir()
        (directory / "overlay.yaml").write_text(yaml.safe_dump(document))
    return str(tmp_path)


def _run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str, *flags: str
) -> int:
    path = tmp_path / "change.diff"
    path.write_text(text)
    return main(["--diff-file", str(path), *flags])


def test_the_cli_refuses_an_undeclared_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    roots = tmp_path / "roots"
    roots.mkdir()
    code = _run(
        tmp_path,
        monkeypatch,
        diff("src/omnimarket/x.py", 'os.environ["ONEX_UNDECLARED_NEW"]'),
        "--overlays-root",
        _roots(roots),
    )
    assert code == EXIT_REFUSED
    assert (
        "REFUSED src/omnimarket/x.py:10 reads env ONEX_UNDECLARED_NEW"
        in capsys.readouterr().err
    )


def test_the_cli_admits_a_declared_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    roots = tmp_path / "roots"
    roots.mkdir()
    assert (
        _run(
            tmp_path,
            monkeypatch,
            diff("src/omnimarket/x.py", 'os.environ["ONEX_DECLARED_ONE"]'),
            "--overlays-root",
            _roots(roots),
        )
        == EXIT_OK
    )


def test_the_cli_answers_unread_when_a_read_is_added_and_no_register_is_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    code = _run(
        tmp_path,
        monkeypatch,
        diff("src/omnimarket/x.py", 'os.environ["ONEX_DECLARED_ONE"]'),
        "--overlays-root",
        str(empty),
    )
    assert code == EXIT_UNREAD


def test_the_cli_needs_no_register_when_the_diff_adds_no_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert (
        _run(tmp_path, monkeypatch, diff("src/omnimarket/x.py", "value = 1")) == EXIT_OK
    )


def test_this_nodes_own_source_adds_no_read_it_does_not_declare() -> None:
    """The check runs on its own change: the handler's patterns are not reads."""
    source = (
        Path(__file__).resolve().parent.parent
        / "src/omnimarket/nodes/node_undeclared_read_check_compute"
    )
    for path in source.rglob("*.py"):
        relative = path.relative_to(source.parent.parent.parent.parent).as_posix()
        added = "".join(f"+{line}\n" for line in path.read_text().splitlines())
        text = f"diff --git a/{relative} b/{relative}\n--- a/{relative}\n+++ b/{relative}\n@@ -0,0 +1,1 @@\n{added}"
        assert not judge(text).refused, relative
