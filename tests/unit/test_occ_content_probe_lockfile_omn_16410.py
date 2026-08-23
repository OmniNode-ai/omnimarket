# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure unit tests for the OMN-16410 lockfile content-bound candidate grammar.

OMN-15247's own docstring flagged the Python-only ``class``/``def`` grammar as
a deliberate slice-1 limitation: a non-Python PR yields zero candidates and
therefore no content-bound check, forcing the emitter to decline and demand
hand-authored evidence. OMN-16410 is that residual gap made concrete: the
OMN-13902 sibling-lock-refresh bot's PRs are uv.lock-only, so every one of
them hit exactly that decline — live-reproduced on omnibase_infra#2848
("no changed-file candidate could be proven RED against the merge base...
Hand-authored evidence is required (OMN-15247)").

These tests exercise ONLY the pure functions in ``occ_content_probe`` —
``extract_lock_line_candidates``, the ``lock_line`` branch of
``declaration_count`` and ``build_content_read_check`` — with directly
constructed content fixtures. Zero network. The property under test is the
same one OMN-15247 established for Python candidates
(``feedback_prove_red_against_exists_but_wrong``): a selected check_value must
be able to go RED against content that does not actually carry the asserted
change.
"""

from __future__ import annotations

from omnimarket.occ_content_probe import (
    SymbolCandidate,
    build_content_read_check,
    declaration_count,
    extract_lock_line_candidates,
    select_asserted_check,
)

_HEAD_SHA = "f" * 40
_BASE_SHA = "0" * 40

# Miniature stand-ins for real uv.lock package blocks, shaped like the live
# omnibase_infra#2848 case (registry-mirror URL rewrite; sha256 hash and
# package name/version unchanged). The mirror host below is a synthetic
# placeholder, not the real internal hostname the live PR carries.
_BASE_BLOCK = (
    "[[package]]\n"
    'name = "omnibase-core"\n'
    'version = "0.46.9"\n'
    'source = { registry = "https://pypi.org/simple" }\n'
    'sdist = { url = "https://files.pythonhosted.org/packages/f6/omnibase_core-0.46.9.tar.gz", hash = "sha256:f5f6c3fde204e29b8bb327faa66190cf482d357581b25e242ad3dcd0400f4a7b" }\n'
)
_HEAD_BLOCK = (
    "[[package]]\n"
    'name = "omnibase-core"\n'
    'version = "0.46.9"\n'
    'source = { registry = "http://mirror.example.test:3141/root/pypi/+simple/" }\n'
    'sdist = { url = "http://mirror.example.test:3141/root/pypi/+f/f5f/omnibase_core-0.46.9.tar.gz", hash = "sha256:f5f6c3fde204e29b8bb327faa66190cf482d357581b25e242ad3dcd0400f4a7b" }\n'
)


class TestExtractLockLineCandidates:
    def test_finds_a_net_new_quoted_run(self) -> None:
        candidates = extract_lock_line_candidates(
            path="uv.lock", head_content=_HEAD_BLOCK, base_content=_BASE_BLOCK
        )
        assert candidates
        symbols = {c.symbol for c in candidates}
        assert "http://mirror.example.test:3141/root/pypi/+simple/" in symbols
        assert all(c.kind == "lock_line" and c.path == "uv.lock" for c in candidates)

    def test_ignores_a_quoted_run_unchanged_between_refs(self) -> None:
        """The unchanged sha256 hash must not be proposed — it does not
        discriminate head from base (present, identically, in both)."""
        candidates = extract_lock_line_candidates(
            path="uv.lock", head_content=_HEAD_BLOCK, base_content=_BASE_BLOCK
        )
        symbols = {c.symbol for c in candidates}
        assert (
            "sha256:f5f6c3fde204e29b8bb327faa66190cf482d357581b25e242ad3dcd0400f4a7b"
            not in symbols
        )

    def test_identical_content_yields_nothing(self) -> None:
        assert (
            extract_lock_line_candidates(
                path="uv.lock", head_content=_BASE_BLOCK, base_content=_BASE_BLOCK
            )
            == ()
        )

    def test_none_head_content_yields_nothing(self) -> None:
        assert (
            extract_lock_line_candidates(
                path="uv.lock", head_content=None, base_content=_BASE_BLOCK
            )
            == ()
        )

    def test_none_base_content_treats_every_head_line_as_net_new(self) -> None:
        """A brand-new uv.lock (file did not exist at the merge base) — every
        quoted run in it is, by construction, absent at base."""
        candidates = extract_lock_line_candidates(
            path="uv.lock", head_content=_HEAD_BLOCK, base_content=None
        )
        assert candidates
        symbols = {c.symbol for c in candidates}
        assert (
            "sha256:f5f6c3fde204e29b8bb327faa66190cf482d357581b25e242ad3dcd0400f4a7b"
            in symbols
        )

    def test_caps_at_five_candidates_per_file(self) -> None:
        head = "".join(f'x = "{"z" * 12}{i:03d}extra_padding"\n' for i in range(20))
        candidates = extract_lock_line_candidates(
            path="uv.lock", head_content=head, base_content=""
        )
        assert len(candidates) == 5

    def test_short_quoted_run_below_minimum_is_ignored(self) -> None:
        candidates = extract_lock_line_candidates(
            path="uv.lock", head_content='name = "short"\n', base_content=""
        )
        assert candidates == ()

    def test_is_deterministic_across_repeated_calls(self) -> None:
        first = extract_lock_line_candidates(
            path="uv.lock", head_content=_HEAD_BLOCK, base_content=_BASE_BLOCK
        )
        second = extract_lock_line_candidates(
            path="uv.lock", head_content=_HEAD_BLOCK, base_content=_BASE_BLOCK
        )
        assert first == second


class TestDeclarationCountLockLine:
    def test_counts_literal_substring_occurrences(self) -> None:
        content = "a-needle-here\nsomething else\na-needle-here\n"
        assert declaration_count(content, "lock_line", "a-needle-here") == 2

    def test_zero_when_absent(self) -> None:
        assert declaration_count("nothing to see", "lock_line", "a-needle-here") == 0

    def test_zero_on_none_content(self) -> None:
        assert declaration_count(None, "lock_line", "a-needle-here") == 0

    def test_is_not_a_regex_dot_does_not_wildcard(self) -> None:
        """OMN-16410: lock_line needles may contain regex metacharacters (a
        URL's ``.``) with their LITERAL meaning — this must be a substring
        count, never a pattern match."""
        content = "a.b.c.d\naXbXcXd\n"
        assert declaration_count(content, "lock_line", "a.b.c.d") == 1


class TestBuildContentReadCheckLockLine:
    def test_uses_fixed_string_grep_and_the_needle_verbatim(self) -> None:
        needle = "http://mirror.example.test:3141/root/pypi/+simple/"
        check = build_content_read_check(
            repo="OmniNode-ai/omnibase_infra",
            path="uv.lock",
            kind="lock_line",
            symbol=needle,
            head_sha=_HEAD_SHA,
        )
        assert "OmniNode-ai/omnibase_infra" in check
        assert "uv.lock" in check
        assert _HEAD_SHA in check
        assert needle in check
        assert "grep -cF" in check
        # No "{kind} {symbol}" prefix -- unlike the Python branch, the needle
        # is not preceded by a synthetic "lock_line " token.
        assert f"lock_line {needle}" not in check


class TestSelectAssertedCheckWithLockLineCandidates:
    def test_selects_the_net_new_mirror_url_red_at_base_green_at_head(self) -> None:
        candidates = extract_lock_line_candidates(
            path="uv.lock", head_content=_HEAD_BLOCK, base_content=_BASE_BLOCK
        )

        def fetch(path: str, ref: str) -> str | None:
            return _HEAD_BLOCK if ref == _HEAD_SHA else _BASE_BLOCK

        check = select_asserted_check(
            candidates,
            repo="OmniNode-ai/omnibase_infra",
            head_sha=_HEAD_SHA,
            base_sha=_BASE_SHA,
            fetch_content=fetch,
        )
        assert check is not None
        assert "mirror.example.test" in check
        assert _HEAD_SHA in check

    def test_a_pure_mirror_rewrite_with_no_actual_line_delta_yields_no_check(
        self,
    ) -> None:
        """Sanity: when head and base are byte-identical, no candidate exists
        at all (extraction itself returns nothing) — never a false RED."""
        candidates = extract_lock_line_candidates(
            path="uv.lock", head_content=_BASE_BLOCK, base_content=_BASE_BLOCK
        )
        assert candidates == ()
        assert (
            select_asserted_check(
                candidates,
                repo="OmniNode-ai/omnibase_infra",
                head_sha=_HEAD_SHA,
                base_sha=_BASE_SHA,
                fetch_content=lambda _p, _r: _BASE_BLOCK,
            )
            is None
        )


def test_symbol_candidate_kind_accepts_lock_line() -> None:
    # SymbolCandidate.kind widened from Literal["class", "def"] to include
    # "lock_line" (OMN-16410) -- construction must not raise.
    candidate = SymbolCandidate(path="uv.lock", kind="lock_line", symbol="x" * 12)
    assert candidate.kind == "lock_line"
