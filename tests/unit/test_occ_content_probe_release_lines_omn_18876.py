# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""RED-proven content-bound candidates for release cuts and runtime pin lines (OMN-18876).

Two bot PR shapes declined ``skip:NO_RED_DERIVABLE_CHECK`` and were then
evidenced by hand:

* a release-train cut whose whole diff is ``CHANGELOG.md`` (omnibase_core#1789,
  omnibase_infra#3868). The release claim is falsifiable: the changelog gains a
  heading for the version being cut, absent at the merge base.
* a runtime plugin pin cascade whose diff is ``docker/Dockerfile.runtime``
  (omnibase_infra#4168 shape). The claim is the new pin literal, absent at the
  merge base.

Neither is an exemption. The candidates below go through the SAME
``select_asserted_check`` RED/GREEN bar as a Python symbol or a lockfile line,
and they are offered only when every changed path is a release artefact
(:func:`is_release_artifact_only_diff`), so a mixed diff never trades its real
change for a changelog line. Zero network.
"""

from __future__ import annotations

import pytest

from omnimarket.occ_content_probe import (
    SymbolCandidate,
    build_content_read_check,
    classify_dependency_pin_only,
    declaration_count,
    extract_release_line_candidates,
    is_release_artifact_only_diff,
    is_shell_safe_check,
    select_asserted_check,
)

pytestmark = pytest.mark.unit

_HEAD = "2993f96192607603cb380ecfa1d75ba54eb4201e"
_BASE = "e1160539656bd4f3a7a05516fd3de1cf23ea1f65"
_REPO_CORE = "OmniNode-ai/omnibase_core"
_REPO_INFRA = "OmniNode-ai/omnibase_infra"

# omnibase_core#1789, trimmed: the release train prepends one version section.
_CHANGELOG_BASE = (
    "## v0.47.23 (2026-09-25)\n"
    "\n"
    "### Release\n"
    "- Cut omnibase-core from dev at 0.47.23 by the scheduled release train.\n"
)
_CHANGELOG_HEAD = (
    "## v0.47.24 (2026-09-26)\n"
    "\n"
    "### Release\n"
    "- Cut omnibase-core from dev at 0.47.24 by the scheduled release train.\n"
    "- 15 release-relevant commit(s) merged since v0.47.23.\n"
    "\n"
    "### Included Since v0.47.23\n"
    "- feat: typed runtime lane declaration (#1786)\n"
    "\n" + _CHANGELOG_BASE
)

# omnibase_infra docker/Dockerfile.runtime plugin block (bounded ranges, OMN-18595).
_DOCKER_BASE = (
    "RUN --mount=type=cache,target=/root/.cache/uv,sharing=locked \\\n"
    "    uv-with-retry pip install --no-deps \\\n"
    '    "omninode-claude>=0.25.1,<1.0.0" \\\n'
    '    "omninode-memory>=0.18.2,<1.0.0" \\\n'
    '    "omninode-intelligence>=0.24.0,<1.0.0"\n'
)
_DOCKER_HEAD = _DOCKER_BASE.replace(
    '"omninode-memory>=0.18.2,<1.0.0"', '"omninode-memory>=0.18.3,<1.0.0"'
)


def _fetch_from(contents: dict[tuple[str, str], str]):  # type: ignore[no-untyped-def]
    def fetch(path: str, ref: str) -> str | None:
        return contents.get((path, ref))

    return fetch


class TestReleaseArtifactOnlyDiff:
    def test_changelog_only_release_cut_is_release_shaped(self) -> None:
        assert is_release_artifact_only_diff(["CHANGELOG.md"])

    def test_changelog_plus_version_manifest_and_lock_is_release_shaped(self) -> None:
        assert is_release_artifact_only_diff(
            ["CHANGELOG.md", "pyproject.toml", "uv.lock"]
        )

    def test_runtime_dockerfile_pin_is_release_shaped(self) -> None:
        assert is_release_artifact_only_diff(["docker/Dockerfile.runtime"])

    def test_any_source_file_disqualifies(self) -> None:
        assert not is_release_artifact_only_diff(
            ["CHANGELOG.md", "src/omnibase_core/models/x.py"]
        )

    def test_a_workflow_file_disqualifies(self) -> None:
        # omnibase_infra#4168 as opened against main carried 100 files, workflows
        # included; that diff must never be treated as a pin bump.
        assert not is_release_artifact_only_diff(
            ["docker/Dockerfile.runtime", ".github/workflows/ci.yml"]
        )

    def test_empty_diff_is_not_release_shaped(self) -> None:
        # An unobservable diff is not an empty one (same rule as OMN-18848).
        assert not is_release_artifact_only_diff([])

    def test_manifest_only_is_not_release_shaped(self) -> None:
        # Needs a changelog or runtime pin to carry a claim; a manifest/lock-only
        # bump stays on the OMN-18848 classifier, untouched here.
        assert not is_release_artifact_only_diff(["pyproject.toml", "uv.lock"])


class TestExtractReleaseLineCandidates:
    def test_changelog_yields_the_new_version_heading_first(self) -> None:
        candidates = extract_release_line_candidates(
            path="CHANGELOG.md",
            head_content=_CHANGELOG_HEAD,
            base_content=_CHANGELOG_BASE,
        )
        assert candidates
        assert candidates[0] == SymbolCandidate(
            path="CHANGELOG.md", kind="text_line", symbol="## v0.47.24 (2026-09-26)"
        )

    def test_changelog_ignores_headings_already_present_at_base(self) -> None:
        symbols = {
            c.symbol
            for c in extract_release_line_candidates(
                path="CHANGELOG.md",
                head_content=_CHANGELOG_HEAD,
                base_content=_CHANGELOG_BASE,
            )
        }
        assert "## v0.47.23 (2026-09-25)" not in symbols
        # "### Release" also exists at base: not RED-controllable.
        assert "### Release" not in symbols

    def test_changelog_bullets_are_not_candidates(self) -> None:
        for c in extract_release_line_candidates(
            path="CHANGELOG.md",
            head_content=_CHANGELOG_HEAD,
            base_content=_CHANGELOG_BASE,
        ):
            assert c.symbol.startswith("#")

    def test_dockerfile_yields_the_new_pin_literal(self) -> None:
        candidates = extract_release_line_candidates(
            path="docker/Dockerfile.runtime",
            head_content=_DOCKER_HEAD,
            base_content=_DOCKER_BASE,
        )
        assert [c.symbol for c in candidates] == ["omninode-memory>=0.18.3,<1.0.0"]
        assert candidates[0].kind == "text_line"

    def test_unchanged_dockerfile_yields_nothing(self) -> None:
        assert (
            extract_release_line_candidates(
                path="docker/Dockerfile.runtime",
                head_content=_DOCKER_BASE,
                base_content=_DOCKER_BASE,
            )
            == ()
        )

    def test_other_files_yield_nothing(self) -> None:
        assert (
            extract_release_line_candidates(
                path="README.md",
                head_content="# A brand new heading\n",
                base_content="",
            )
            == ()
        )

    def test_none_head_yields_nothing(self) -> None:
        assert (
            extract_release_line_candidates(
                path="CHANGELOG.md", head_content=None, base_content=_CHANGELOG_BASE
            )
            == ()
        )

    def test_unsafe_characters_are_never_proposed(self) -> None:
        head = "## v1.0.0 it's $HOME `x`\n" + _CHANGELOG_BASE
        assert (
            extract_release_line_candidates(
                path="CHANGELOG.md", head_content=head, base_content=_CHANGELOG_BASE
            )
            == ()
        )

    def test_is_deterministic(self) -> None:
        args = {
            "path": "CHANGELOG.md",
            "head_content": _CHANGELOG_HEAD,
            "base_content": _CHANGELOG_BASE,
        }
        assert extract_release_line_candidates(
            **args
        ) == extract_release_line_candidates(**args)


class TestTextLineCheckIsFixedString:
    def test_declaration_count_is_a_literal_substring_count(self) -> None:
        assert (
            declaration_count(_CHANGELOG_HEAD, "text_line", "## v0.47.24 (2026-09-26)")
            == 1
        )
        assert (
            declaration_count(_CHANGELOG_BASE, "text_line", "## v0.47.24 (2026-09-26)")
            == 0
        )
        # A regex would let "." match any char; fixed-string must not.
        assert (
            declaration_count(
                "## v0x47x24 (2026-09-26)", "text_line", "## v0.47.24 (2026-09-26)"
            )
            == 0
        )

    def test_build_uses_fixed_string_grep(self) -> None:
        check = build_content_read_check(
            repo=_REPO_CORE,
            path="CHANGELOG.md",
            kind="text_line",
            symbol="## v0.47.24 (2026-09-26)",
            head_sha=_HEAD,
        )
        assert check == (
            f"gh api repos/{_REPO_CORE}/contents/CHANGELOG.md?ref={_HEAD} "
            "--jq '.content' | base64 -d | grep -cF '## v0.47.24 (2026-09-26)'"
        )
        assert is_shell_safe_check(check)


class TestReplaySpecimens:
    """The #1789 and #4168 shapes, end to end through the RED/GREEN selector."""

    def test_release_cut_1789_shape_binds_a_red_proven_heading(self) -> None:
        candidates = extract_release_line_candidates(
            path="CHANGELOG.md",
            head_content=_CHANGELOG_HEAD,
            base_content=_CHANGELOG_BASE,
        )
        fetch = _fetch_from(
            {
                ("CHANGELOG.md", _HEAD): _CHANGELOG_HEAD,
                ("CHANGELOG.md", _BASE): _CHANGELOG_BASE,
            }
        )
        check = select_asserted_check(
            candidates,
            repo=_REPO_CORE,
            head_sha=_HEAD,
            base_sha=_BASE,
            fetch_content=fetch,
        )
        assert check is not None
        assert "grep -cF '## v0.47.24 (2026-09-26)'" in check
        assert f"?ref={_HEAD}" in check

    def test_release_cut_red_leg_fails_if_heading_already_at_base(self) -> None:
        # Falsifier: a re-opened release PR whose heading already landed is NOT
        # provable, and must still decline.
        candidates = extract_release_line_candidates(
            path="CHANGELOG.md",
            head_content=_CHANGELOG_HEAD,
            base_content=_CHANGELOG_HEAD,
        )
        assert candidates == ()

    def test_pin_cascade_4168_shape_binds_the_new_pin(self) -> None:
        path = "docker/Dockerfile.runtime"
        candidates = extract_release_line_candidates(
            path=path, head_content=_DOCKER_HEAD, base_content=_DOCKER_BASE
        )
        fetch = _fetch_from({(path, _HEAD): _DOCKER_HEAD, (path, _BASE): _DOCKER_BASE})
        check = select_asserted_check(
            candidates,
            repo=_REPO_INFRA,
            head_sha=_HEAD,
            base_sha=_BASE,
            fetch_content=fetch,
        )
        assert check == (
            f"gh api repos/{_REPO_INFRA}/contents/{path}?ref={_HEAD} "
            "--jq '.content' | base64 -d | grep -cF 'omninode-memory>=0.18.3,<1.0.0'"
        )

    def test_release_shape_is_not_a_pin_only_exemption(self) -> None:
        # The OMN-18848 exemption surface is unchanged: a changelog is still not
        # a dependency manifest, so these PRs get evidence, not an exemption.
        verdict, _ = classify_dependency_pin_only(
            ["CHANGELOG.md"], pyproject_head=None, pyproject_base=None
        )
        assert verdict is False
