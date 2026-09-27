# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18876 AC1: a no-RED-derivable decline says what it considered and why.

Before this, ``select_asserted_check`` dropped a failing candidate with a bare
``continue`` and the emitter's decline printed only an aggregate count, so a
reader could not tell whether a changed file had been examined and rejected or
never looked at. These tests pin the pure half: every rejection branch of the
selection bar reports its reason through ``on_reject``, and every changed file
that produced no candidate gets a reason naming which grammar skipped it.
"""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from omnimarket.occ_content_probe import (
    ConsideredPath,
    SymbolCandidate,
    build_considered_paths,
    describe_uncandidated_path,
    render_considered_paths,
    render_considered_paths_inline,
    select_asserted_check,
)

_HEAD = "b" * 40
_BASE = "a" * 40


def _select(
    candidates: list[SymbolCandidate],
    contents: Mapping[tuple[str, str], str | None],
    *,
    accept: bool = True,
) -> tuple[str | None, list[tuple[SymbolCandidate, str]]]:
    rejected: list[tuple[SymbolCandidate, str]] = []
    check = select_asserted_check(
        candidates,
        repo="o/r",
        head_sha=_HEAD,
        base_sha=_BASE,
        fetch_content=lambda path, ref: contents.get((path, ref)),
        accept=None if accept else (lambda _check: False),
        on_reject=lambda candidate, reason: rejected.append((candidate, reason)),
    )
    return check, rejected


@pytest.mark.unit
class TestSelectionReportsEveryRejection:
    def test_absent_at_head(self) -> None:
        cand = SymbolCandidate(path="x.py", kind="class", symbol="Gone")
        check, rejected = _select([cand], {("x.py", _HEAD): "class Other:\n"})
        assert check is None
        assert rejected == [(cand, "absent at head (count 0)")]

    def test_unreadable_at_head_names_the_contents_api_limit(self) -> None:
        cand = SymbolCandidate(path="uv.lock", kind="lock_line", symbol="x" * 20)
        _check, rejected = _select([cand], {("uv.lock", _HEAD): None})
        assert rejected[0][1] == (
            "unreadable at head (no content returned; the contents API returns "
            "no body for a file over 1 MB)"
        )

    def test_already_present_at_the_merge_base(self) -> None:
        cand = SymbolCandidate(
            path="CHANGELOG.md", kind="text_line", symbol="## v1.2.3 (2026-09-26)"
        )
        text = "## v1.2.3 (2026-09-26)\n"
        _check, rejected = _select(
            [cand], {("CHANGELOG.md", _HEAD): text, ("CHANGELOG.md", _BASE): text}
        )
        assert rejected[0][1] == (
            "not RED-controlled: count 1 at the merge base, 1 at head"
        )

    def test_a_destination_constraint_rejection_is_named(self) -> None:
        cand = SymbolCandidate(path="x.py", kind="class", symbol="New")
        check, rejected = _select(
            [cand],
            {("x.py", _HEAD): "class New:\n", ("x.py", _BASE): ""},
            accept=False,
        )
        assert check is None
        assert rejected[0][1] == "rendered check refused by the destination constraint"

    def test_the_selected_candidate_is_never_reported_as_rejected(self) -> None:
        first = SymbolCandidate(path="x.py", kind="class", symbol="Old")
        second = SymbolCandidate(path="x.py", kind="class", symbol="New")
        text_head = "class Old:\nclass New:\n"
        check, rejected = _select(
            [first, second],
            {("x.py", _HEAD): text_head, ("x.py", _BASE): "class Old:\n"},
        )
        assert check is not None
        assert "class New" in check
        assert [c for c, _ in rejected] == [first]

    def test_on_reject_is_optional_and_changes_no_verdict(self) -> None:
        cand = SymbolCandidate(path="x.py", kind="class", symbol="New")
        contents = {("x.py", _HEAD): "class New:\n", ("x.py", _BASE): ""}
        with_cb, _ = _select([cand], contents)
        without_cb = select_asserted_check(
            [cand],
            repo="o/r",
            head_sha=_HEAD,
            base_sha=_BASE,
            fetch_content=lambda path, ref: contents.get((path, ref)),
        )
        assert with_cb == without_cb


@pytest.mark.unit
class TestEveryUncandidatedPathGetsAReason:
    @pytest.mark.parametrize(
        ("kwargs", "fragment"),
        [
            (
                {"path": "README.md", "status": "modified"},
                "no candidate grammar reads this file type",
            ),
            (
                {"path": ".github/workflows/ci.yml", "status": "modified"},
                "no candidate grammar reads this file type",
            ),
            (
                {"path": "src/x.py", "status": "removed"},
                "status 'removed'",
            ),
            (
                {"path": "src/x.py", "status": "modified"},
                "no added class or def line",
            ),
            (
                {"path": "src/x.py", "status": "modified", "patch_present": False},
                "GitHub omitted its patch",
            ),
            (
                {"path": "uv.lock", "status": "modified", "head_readable": True},
                "no net-new quoted run",
            ),
            (
                {"path": "uv.lock", "status": "modified", "head_readable": False},
                "over 1 MB",
            ),
            (
                {
                    "path": "CHANGELOG.md",
                    "status": "modified",
                    "release_only_diff": False,
                },
                "the diff also changes a path that is not a release artefact",
            ),
            (
                {
                    "path": "docker/Dockerfile.runtime",
                    "status": "modified",
                    "release_only_diff": True,
                    "head_readable": True,
                },
                "no net-new heading or quoted run",
            ),
        ],
    )
    def test_reason(self, kwargs: dict[str, object], fragment: str) -> None:
        call: dict[str, object] = {
            "patch_present": True,
            "release_only_diff": False,
            "head_readable": None,
        }
        call.update(kwargs)
        reason = describe_uncandidated_path(**call)  # type: ignore[arg-type]
        assert fragment in reason, reason


@pytest.mark.unit
class TestRendering:
    _CONSIDERED = (
        ConsideredPath(path="CHANGELOG.md", reason="not RED-controlled: x"),
        ConsideredPath(path="README.md", reason="no candidate grammar reads it"),
    )

    def test_block_lists_every_path_with_its_reason(self) -> None:
        block = render_considered_paths(self._CONSIDERED)
        assert block.splitlines() == [
            "Considered 2 changed file(s):",
            "- `CHANGELOG.md`: not RED-controlled: x",
            "- `README.md`: no candidate grammar reads it",
        ]

    def test_block_caps_and_says_how_many_were_left_out(self) -> None:
        many = tuple(ConsideredPath(path=f"f{i}.md", reason="r") for i in range(5))
        block = render_considered_paths(many, limit=2)
        assert block.splitlines()[-1] == "- ... and 3 more changed file(s)"
        assert block.count("\n- `") == 2

    def test_inline_form_is_one_line(self) -> None:
        inline = render_considered_paths_inline(self._CONSIDERED)
        assert "\n" not in inline
        assert inline == (
            "considered 2 changed file(s): CHANGELOG.md (not RED-controlled: x); "
            "README.md (no candidate grammar reads it)"
        )

    def test_nothing_considered_is_said_plainly(self) -> None:
        assert render_considered_paths(()) == "Considered 0 changed file(s)."
        assert render_considered_paths_inline(()) == "considered 0 changed file(s)"


@pytest.mark.unit
class TestBuildConsideredPaths:
    def _build(
        self,
        files: list[dict[str, object]],
        candidates: list[SymbolCandidate],
        rejections: list[tuple[SymbolCandidate, str]],
        selected_outcome: str | None = None,
    ) -> tuple[ConsideredPath, ...]:
        return build_considered_paths(
            files=files,
            candidates=candidates,
            rejections=rejections,
            selected_outcome=selected_outcome,
            release_only_diff=False,
            head_readable=lambda _path: None,
        )

    def test_every_changed_file_appears_once_in_listing_order(self) -> None:
        cand = SymbolCandidate(path="src/x.py", kind="class", symbol="New")
        considered = self._build(
            [
                {"filename": "README.md", "status": "modified"},
                {"filename": "src/x.py", "status": "modified", "patch": "+class New:"},
            ],
            [cand],
            [(cand, "absent at head (count 0)")],
        )
        assert [c.path for c in considered] == ["README.md", "src/x.py"]
        assert "no candidate grammar reads this file type" in considered[0].reason
        assert considered[1].reason == "class `New` absent at head (count 0)"

    def test_selected_and_unevaluated_candidates_are_told_apart(self) -> None:
        a = SymbolCandidate(path="x.py", kind="class", symbol="A")
        b = SymbolCandidate(path="x.py", kind="class", symbol="B")
        c = SymbolCandidate(path="x.py", kind="def", symbol="c")
        considered = self._build(
            [{"filename": "x.py", "status": "modified", "patch": ""}],
            [a, b, c],
            [(a, "absent at head (count 0)")],
            selected_outcome="selected, but the mint-time GREEN execution exited 1",
        )
        assert considered[0].reason == (
            "class `A` absent at head (count 0); "
            "class `B` selected, but the mint-time GREEN execution exited 1; "
            "def `c` not evaluated (selection stops at the first passing candidate)"
        )

    def test_a_long_needle_is_shortened_in_the_reason(self) -> None:
        cand = SymbolCandidate(path="uv.lock", kind="lock_line", symbol="u" * 200)
        considered = self._build(
            [{"filename": "uv.lock", "status": "modified"}],
            [cand],
            [(cand, "absent at head (count 0)")],
        )
        assert "u" * 77 + "..." in considered[0].reason
        assert "u" * 78 not in considered[0].reason

    def test_rejections_out_of_order_are_refused(self) -> None:
        a = SymbolCandidate(path="x.py", kind="class", symbol="A")
        b = SymbolCandidate(path="x.py", kind="class", symbol="B")
        with pytest.raises(ValueError, match="prefix"):
            self._build([], [a, b], [(b, "absent at head (count 0)")])
