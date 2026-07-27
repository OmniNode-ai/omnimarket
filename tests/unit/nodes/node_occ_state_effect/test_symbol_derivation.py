# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure unit tests for the OMN-14619 content-read symbol-derivation logic.

These exercise ONLY the pure functions in ``handler_occ_state_effect`` —
``extract_symbol_candidates``, ``declaration_count``, ``build_content_read_check``,
``select_asserted_check`` — with directly-constructed diff/content fixtures and an
injected ``fetch_content`` stub. Zero network, zero subprocess: the live-network
half (``HandlerOccStateEffect._gather_sync``) is proven separately against a real
canary PR (OMN-14619 evidence), not unit-tested here.

The property under test is exactly feedback_prove_red_against_exists_but_wrong:
a selected check_value must be able to go RED against a PR that claims the
symbol but never actually introduced it (a symbol already present at the base
ref is rejected, never asserted as evidence).
"""

from __future__ import annotations

from omnimarket.nodes.node_occ_state_effect.handlers.handler_occ_state_effect import (
    SymbolCandidate,
    build_content_read_check,
    declaration_count,
    extract_symbol_candidates,
    select_asserted_check,
)

_HEAD_SHA = "f" * 40
_BASE_SHA = "0" * 40


def _file(filename: str, status: str, patch: str | None) -> dict[str, object]:
    entry: dict[str, object] = {"filename": filename, "status": status}
    if patch is not None:
        entry["patch"] = patch
    return entry


# ---------------------------------------------------------------------------
# extract_symbol_candidates
# ---------------------------------------------------------------------------


class TestExtractSymbolCandidates:
    def test_finds_added_top_level_class(self) -> None:
        patch = (
            "@@ -0,0 +1,5 @@\n"
            "+from __future__ import annotations\n"
            "+\n"
            "+\n"
            "+class HandlerCodegenOutcomeReducer:\n"
            "+    pass\n"
        )
        files = [_file("src/x/handler_x.py", "added", patch)]
        candidates = extract_symbol_candidates(files)
        assert candidates == (
            SymbolCandidate(
                path="src/x/handler_x.py",
                kind="class",
                symbol="HandlerCodegenOutcomeReducer",
            ),
        )

    def test_finds_added_nested_async_def(self) -> None:
        patch = "@@ -10,0 +11,2 @@ class Foo:\n+    async def handle(self, x):\n+        return x\n"
        files = [_file("src/x/handler_x.py", "modified", patch)]
        candidates = extract_symbol_candidates(files)
        assert candidates == (
            SymbolCandidate(path="src/x/handler_x.py", kind="def", symbol="handle"),
        )

    def test_ignores_non_python_files(self) -> None:
        patch = "+class Foo:\n"
        files = [_file("contracts/OMN-1.yaml", "added", patch)]
        assert extract_symbol_candidates(files) == ()

    def test_ignores_removed_files(self) -> None:
        patch = "+class Foo:\n"
        files = [_file("src/x/old.py", "removed", patch)]
        assert extract_symbol_candidates(files) == ()

    def test_ignores_context_and_removed_lines(self) -> None:
        patch = (
            "@@ -1,3 +1,3 @@\n class Existing:\n-    def old(self): ...\n     pass\n"
        )
        files = [_file("src/x/handler_x.py", "modified", patch)]
        assert extract_symbol_candidates(files) == ()

    def test_no_patch_field_yields_no_candidates(self) -> None:
        files = [_file("src/x/handler_x.py", "modified", None)]
        assert extract_symbol_candidates(files) == ()

    def test_yaml_only_diff_yields_no_candidates(self) -> None:
        files = [_file("contracts/OMN-1.yaml", "added", "+ticket_id: OMN-1\n")]
        assert extract_symbol_candidates(files) == ()


# ---------------------------------------------------------------------------
# declaration_count
# ---------------------------------------------------------------------------


class TestDeclarationCount:
    def test_counts_class_declaration(self) -> None:
        content = "class Foo:\n    pass\n\n\nclass Foo:\n    pass\n"
        assert declaration_count(content, "class", "Foo") == 2

    def test_counts_def_and_async_def(self) -> None:
        content = "def handle():\n    pass\n\nasync def handle():\n    pass\n"
        assert declaration_count(content, "def", "handle") == 2

    def test_zero_when_absent(self) -> None:
        assert declaration_count("class Bar:\n    pass\n", "class", "Foo") == 0

    def test_zero_on_none_content(self) -> None:
        assert declaration_count(None, "class", "Foo") == 0

    def test_does_not_match_substring_symbol(self) -> None:
        # "FooBar" must not count as a match for symbol "Foo".
        content = "class FooBar:\n    pass\n"
        assert declaration_count(content, "class", "Foo") == 0


# ---------------------------------------------------------------------------
# build_content_read_check
# ---------------------------------------------------------------------------


class TestBuildContentReadCheck:
    def test_pins_repo_path_head_sha_and_symbol(self) -> None:
        check = build_content_read_check(
            repo="OmniNode-ai/omnimarket",
            path="src/x/handler_x.py",
            kind="class",
            symbol="HandlerX",
            head_sha=_HEAD_SHA,
        )
        assert "OmniNode-ai/omnimarket" in check
        assert "src/x/handler_x.py" in check
        assert _HEAD_SHA in check
        assert "class HandlerX" in check
        assert "grep -c" in check


# ---------------------------------------------------------------------------
# select_asserted_check — the RED-control property
# ---------------------------------------------------------------------------


class TestSelectAssertedCheck:
    def test_selects_candidate_present_at_head_absent_at_base(self) -> None:
        candidates = (
            SymbolCandidate(path="src/x.py", kind="class", symbol="HandlerX"),
        )

        def fetch(path: str, ref: str) -> str | None:
            if ref == _HEAD_SHA:
                return "class HandlerX:\n    pass\n"
            return None  # absent at base -> file didn't exist before the PR

        check = select_asserted_check(
            candidates,
            repo="OmniNode-ai/omnimarket",
            head_sha=_HEAD_SHA,
            base_sha=_BASE_SHA,
            fetch_content=fetch,
        )
        assert check is not None
        assert "class HandlerX" in check
        assert _HEAD_SHA in check

    def test_rejects_candidate_already_present_at_base(self) -> None:
        """RED-control: a symbol that already existed at base is not evidence
        the PR added it — must not be asserted (feedback_prove_red_against_
        exists_but_wrong)."""
        candidates = (SymbolCandidate(path="src/x.py", kind="def", symbol="handle"),)

        def fetch(path: str, ref: str) -> str | None:
            return "def handle():\n    pass\n"  # identical at both refs

        check = select_asserted_check(
            candidates,
            repo="OmniNode-ai/omnimarket",
            head_sha=_HEAD_SHA,
            base_sha=_BASE_SHA,
            fetch_content=fetch,
        )
        assert check is None

    def test_falls_through_to_next_candidate_when_first_fails_red_control(
        self,
    ) -> None:
        candidates = (
            SymbolCandidate(path="src/x.py", kind="def", symbol="existing"),
            SymbolCandidate(path="src/x.py", kind="class", symbol="NewOne"),
        )

        def fetch(path: str, ref: str) -> str | None:
            if ref == _HEAD_SHA:
                return "def existing():\n    pass\n\nclass NewOne:\n    pass\n"
            return "def existing():\n    pass\n"  # NewOne absent at base

        check = select_asserted_check(
            candidates,
            repo="OmniNode-ai/omnimarket",
            head_sha=_HEAD_SHA,
            base_sha=_BASE_SHA,
            fetch_content=fetch,
        )
        assert check is not None
        assert "class NewOne" in check

    def test_returns_none_when_no_candidate_survives(self) -> None:
        candidates = (SymbolCandidate(path="src/x.py", kind="def", symbol="existing"),)

        def fetch(path: str, ref: str) -> str | None:
            return "def existing():\n    pass\n"

        assert (
            select_asserted_check(
                candidates,
                repo="OmniNode-ai/omnimarket",
                head_sha=_HEAD_SHA,
                base_sha=_BASE_SHA,
                fetch_content=fetch,
            )
            is None
        )

    def test_returns_none_for_empty_candidates(self) -> None:
        assert (
            select_asserted_check(
                (),
                repo="OmniNode-ai/omnimarket",
                head_sha=_HEAD_SHA,
                base_sha=_BASE_SHA,
                fetch_content=lambda _path, _ref: None,
            )
            is None
        )
