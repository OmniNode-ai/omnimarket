# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18010 deliverable 2 — released is part of Done.

Every probe answer in this file is a RECORDED fixture, captured live on
2026-09-07 and quoted in :data:`RECORDED` below. Nothing here touches the
network or a git clone: the two probes are :class:`typing.Protocol` stubs that
replay those recordings, so the suite is hermetic and deterministic. The live
counterparts are exercised by the ``integration`` test at the bottom, which is
marked so it never runs in the hermetic lane.

The failure class being closed: a merged PR in a repo that publishes to a
package index used to satisfy Done. ``omnimarket#2304`` merged 2026-09-05 and
sat unreleased with its own release ticket in Backlog; ``#2334`` likewise.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_dod_verify.handlers.handler_dod_evidence_github_effect import (
    PACKAGE_INDEX_JSON_URL,
    pypi_release_files,
)
from omnimarket.nodes.node_dod_verify.services.evidence_collector import (
    git_release_tags_containing,
)
from omnimarket.nodes.node_dod_verify.services.released_evidence import (
    PUBLISHING_REPO_DISTRIBUTIONS,
    REQUIRED_INDEX_PACKAGE_TYPES,
    EnumReleasedOutcome,
    ModelReleasedCitationInput,
    aggregate_outcome,
    distribution_for_repo,
    evaluate_released,
    is_closing_outcome,
    is_publishing_repo,
    parse_released_check_value,
    version_from_release_tag,
)

# --------------------------------------------------------------------------- #
# Recorded fixtures — captured live 2026-09-07, commands quoted verbatim
# --------------------------------------------------------------------------- #

# ``git -C $OMNI_HOME/omnibase_core tag --list 'v*' --contains 9386a873``
#   => v0.47.4\nv0.47.5      (OMN-17980's merge; the ticket's own positive control)
# ``git -C $OMNI_HOME/omnimarket tag --list 'v*' --contains 7287a42c``
#   => (empty)               (omnimarket#2370, merged, in no release tag)
# ``git -C $OMNI_HOME/omnimarket tag --list 'v*' --contains 021b29ca``
#   => v0.4.22               (positive control for the SAME probe, so the empty
#                             result above is proven to be a real zero and not a
#                             broken invocation)
RECORDED_TAGS: dict[tuple[str, str], tuple[str, ...] | None] = {
    ("OmniNode-ai/omnibase_core", "9386a873"): ("v0.47.4", "v0.47.5"),
    ("OmniNode-ai/omnimarket", "7287a42c"): (),
    ("OmniNode-ai/omnimarket", "021b29ca"): ("v0.4.22",),
}

# ``curl -sS https://pypi.org/pypi/omnibase-core/0.47.4/json`` => HTTP 200,
#   packagetypes ['bdist_wheel', 'sdist']
# ``curl -o /dev/null -w '%{http_code}' https://pypi.org/pypi/omnibase-core/0.99.99/json``
#   => 404  (negative control: a real distribution, a version it does not serve)
# ``curl -o /dev/null -w '%{http_code}' https://pypi.org/pypi/definitely-not-a-real-pkg-xyzzy/json``
#   => 404  (negative control for the probe itself)
RECORDED_INDEX: dict[tuple[str, str], frozenset[str] | None] = {
    ("omnibase-core", "0.47.4"): frozenset({"bdist_wheel", "sdist"}),
    ("omnibase-core", "0.47.5"): frozenset({"bdist_wheel", "sdist"}),
    ("omnibase-core", "0.99.99"): frozenset(),
    ("omnimarket", "0.4.22"): frozenset({"bdist_wheel", "sdist"}),
}


def _tags_probe(
    overrides: dict[tuple[str, str], tuple[str, ...] | None] | None = None,
):
    table = dict(RECORDED_TAGS)
    table.update(overrides or {})

    def probe(repo: str, commit_sha: str) -> tuple[str, ...] | None:
        if (repo, commit_sha) not in table:
            msg = f"unrecorded tag probe: {repo}@{commit_sha}"
            raise AssertionError(msg)
        return table[(repo, commit_sha)]

    return probe


def _index_probe(
    overrides: dict[tuple[str, str], frozenset[str] | None] | None = None,
):
    table = dict(RECORDED_INDEX)
    table.update(overrides or {})

    def probe(distribution: str, version: str) -> frozenset[str] | None:
        if (distribution, version) not in table:
            msg = f"unrecorded index probe: {distribution} {version}"
            raise AssertionError(msg)
        return table[(distribution, version)]

    return probe


def _cite(repo: str, sha: str, pr: int | None = None) -> ModelReleasedCitationInput:
    return ModelReleasedCitationInput(repo=repo, merge_sha=sha, pr_number=pr)


@pytest.mark.unit
class TestPublishingRepoRegistry:
    """The trigger is 'lands in a publishing repo', not a label."""

    def test_every_registry_repo_maps_to_a_distribution(self) -> None:
        for repo, dist in PUBLISHING_REPO_DISTRIBUTIONS.items():
            assert repo.startswith("OmniNode-ai/"), repo
            assert dist, repo
            assert dist == dist.lower().replace("_", "-"), dist
            assert is_publishing_repo(repo)
            assert distribution_for_repo(repo) == dist

    @pytest.mark.parametrize(
        "repo",
        [
            "OmniNode-ai/onex_change_control",
            "OmniNode-ai/omnidash",
            "OmniNode-ai/omniweb",
            "OmniNode-ai/omni_home",
        ],
    )
    def test_non_publishing_repos_are_not_in_the_registry(self, repo: str) -> None:
        """A repo that publishes nothing can never be 'unreleased'.

        Negative control for the registry: onex_change_control publishes no
        distribution (``https://pypi.org/pypi/onex-change-control/json`` => 404,
        recorded 2026-09-06 on the ticket), so a merge there is NOT_APPLICABLE.
        """
        assert not is_publishing_repo(repo)
        assert distribution_for_repo(repo) is None

    @pytest.mark.parametrize(
        ("tag", "expected"),
        [
            ("v0.47.4", "0.47.4"),
            ("v0.4.22", "0.4.22"),
            ("v1.0.0", "1.0.0"),
            ("0.47.4", None),
            ("v0.47.4-rc1", None),
            ("lane-marker", None),
            ("", None),
        ],
    )
    def test_release_tag_version_parsing(self, tag: str, expected: str | None) -> None:
        assert version_from_release_tag(tag) == expected

    def test_index_url_shape_is_pinned(self) -> None:
        """The URL a reader can re-run by hand, asserted without calling it."""
        assert (
            PACKAGE_INDEX_JSON_URL.format(
                distribution="omnibase-core", version="0.47.4"
            )
            == "https://pypi.org/pypi/omnibase-core/0.47.4/json"
        )


@pytest.mark.unit
class TestReleasedEvaluation:
    """The core verdict: released, merged-unreleased, tag-not-on-index, unknown."""

    def test_released_merge_passes(self) -> None:
        """OMN-17980's own positive control: 9386a873 in v0.47.4, index 200."""
        result = evaluate_released(
            (_cite("OmniNode-ai/omnibase_core", "9386a873", 1698),),
            release_tags_containing=_tags_probe(),
            index_release_files=_index_probe(),
        )
        assert result.outcome is EnumReleasedOutcome.RELEASED
        assert result.passed
        assert result.citations[0].released_version == "0.47.4"
        assert "v0.47.4" in result.message

    def test_merged_but_in_no_release_tag_is_merged_unreleased(self) -> None:
        """The headline non-closing state. omnimarket#2370 is merged, untagged."""
        result = evaluate_released(
            (_cite("OmniNode-ai/omnimarket", "7287a42c", 2370),),
            release_tags_containing=_tags_probe(),
            index_release_files=_index_probe(),
        )
        assert result.outcome is EnumReleasedOutcome.MERGED_UNRELEASED
        assert not result.passed
        assert "shipped nowhere" in result.message

    def test_positive_control_for_the_empty_tag_result(self) -> None:
        """The same probe, same repo, a sha that IS released => a non-empty answer.

        Without this, the empty tuple in the test above is indistinguishable
        from a probe that silently returns nothing for every input.
        """
        result = evaluate_released(
            (_cite("OmniNode-ai/omnimarket", "021b29ca", 2369),),
            release_tags_containing=_tags_probe(),
            index_release_files=_index_probe(),
        )
        assert result.outcome is EnumReleasedOutcome.RELEASED
        assert result.citations[0].containing_tags == ("v0.4.22",)

    def test_tag_exists_but_index_does_not_serve_it(self) -> None:
        """A tag is not a release: no wheel+sdist on the index => non-closing."""
        result = evaluate_released(
            (_cite("OmniNode-ai/omnibase_core", "deadbee", 1),),
            release_tags_containing=_tags_probe(
                {("OmniNode-ai/omnibase_core", "deadbee"): ("v0.99.99",)}
            ),
            index_release_files=_index_probe(
                {("omnibase-core", "0.99.99"): frozenset()}
            ),
        )
        assert result.outcome is EnumReleasedOutcome.RELEASED_TAG_NOT_ON_INDEX
        assert not result.passed
        assert "A tag is not a release" in result.message

    def test_wheel_without_sdist_is_not_served(self) -> None:
        """Both distribution files are required, not either."""
        result = evaluate_released(
            (_cite("OmniNode-ai/omnibase_core", "deadbee", 1),),
            release_tags_containing=_tags_probe(
                {("OmniNode-ai/omnibase_core", "deadbee"): ("v0.99.99",)}
            ),
            index_release_files=_index_probe(
                {("omnibase-core", "0.99.99"): frozenset({"bdist_wheel"})}
            ),
        )
        assert result.outcome is EnumReleasedOutcome.RELEASED_TAG_NOT_ON_INDEX
        assert "sdist" in result.message

    def test_unresolvable_tag_lookup_is_indeterminate_not_released(self) -> None:
        """Fail-closed. An unfetched clone must not read as merged-unreleased.

        This is the direction that would silently invert the check: a probe
        that cannot answer returning ``()`` would be read as 'no tag contains
        it' and produce a false finding; returning None must stay distinct.
        """
        result = evaluate_released(
            (_cite("OmniNode-ai/omnibase_core", "cafebab", 1),),
            release_tags_containing=_tags_probe(
                {("OmniNode-ai/omnibase_core", "cafebab"): None}
            ),
            index_release_files=_index_probe(),
        )
        assert result.outcome is EnumReleasedOutcome.INDETERMINATE
        assert not result.passed
        assert "NOT evidence that" in result.message

    def test_unreachable_index_is_indeterminate_not_released(self) -> None:
        result = evaluate_released(
            (_cite("OmniNode-ai/omnibase_core", "9386a873", 1),),
            release_tags_containing=_tags_probe(),
            index_release_files=_index_probe(
                {
                    ("omnibase-core", "0.47.4"): None,
                    ("omnibase-core", "0.47.5"): None,
                }
            ),
        )
        assert result.outcome is EnumReleasedOutcome.INDETERMINATE
        assert not result.passed

    def test_malformed_sha_is_indeterminate(self) -> None:
        result = evaluate_released(
            (
                ModelReleasedCitationInput(
                    repo="OmniNode-ai/omnimarket", merge_sha="zzzzzzz"
                ),
            ),
            release_tags_containing=_tags_probe(),
            index_release_files=_index_probe(),
        )
        assert result.outcome is EnumReleasedOutcome.INDETERMINATE

    def test_non_publishing_repo_is_not_applicable(self) -> None:
        result = evaluate_released(
            (_cite("OmniNode-ai/onex_change_control", "abcdef1", 8519),),
            release_tags_containing=_tags_probe(),
            index_release_files=_index_probe(),
        )
        assert result.outcome is EnumReleasedOutcome.NOT_APPLICABLE
        assert result.passed

    def test_no_citations_is_not_applicable(self) -> None:
        result = evaluate_released(
            (),
            release_tags_containing=_tags_probe(),
            index_release_files=_index_probe(),
        )
        assert result.outcome is EnumReleasedOutcome.NOT_APPLICABLE
        assert result.passed

    def test_one_unreleased_citation_blocks_a_mixed_set(self) -> None:
        """EVERY cited merge must be released — not a majority, not the first."""
        result = evaluate_released(
            (
                _cite("OmniNode-ai/omnibase_core", "9386a873", 1698),
                _cite("OmniNode-ai/omnimarket", "7287a42c", 2370),
            ),
            release_tags_containing=_tags_probe(),
            index_release_files=_index_probe(),
        )
        assert result.outcome is EnumReleasedOutcome.MERGED_UNRELEASED
        assert not result.passed
        assert "1 of 2" in result.message

    def test_merged_unreleased_outranks_an_incidental_probe_failure(self) -> None:
        """The actionable state is the headline, not whichever came first."""
        assert (
            aggregate_outcome.__doc__ is not None
        )  # documented precedence, asserted below
        result = evaluate_released(
            (
                _cite("OmniNode-ai/omnibase_core", "cafebab", 1),
                _cite("OmniNode-ai/omnimarket", "7287a42c", 2370),
            ),
            release_tags_containing=_tags_probe(
                {("OmniNode-ai/omnibase_core", "cafebab"): None}
            ),
            index_release_files=_index_probe(),
        )
        assert result.outcome is EnumReleasedOutcome.MERGED_UNRELEASED

    @pytest.mark.parametrize(
        ("outcome", "closes"),
        [
            (EnumReleasedOutcome.RELEASED, True),
            (EnumReleasedOutcome.NOT_APPLICABLE, True),
            (EnumReleasedOutcome.MERGED_UNRELEASED, False),
            (EnumReleasedOutcome.RELEASED_TAG_NOT_ON_INDEX, False),
            (EnumReleasedOutcome.INDETERMINATE, False),
        ],
    )
    def test_only_two_outcomes_permit_closure(
        self, outcome: EnumReleasedOutcome, closes: bool
    ) -> None:
        assert is_closing_outcome(outcome) is closes

    def test_required_package_types(self) -> None:
        assert frozenset({"bdist_wheel", "sdist"}) == REQUIRED_INDEX_PACKAGE_TYPES


@pytest.mark.unit
class TestProductionProbesFailClosed:
    """The live probes must return None — not a false zero — when they cannot run."""

    def test_missing_clone_is_none_not_empty(self, tmp_path) -> None:
        missing = tmp_path / "not-a-clone"
        assert git_release_tags_containing(missing, "9386a873") is None

    def test_directory_that_is_not_a_git_repo_is_none(self, tmp_path) -> None:
        plain = tmp_path / "plain"
        plain.mkdir()
        assert git_release_tags_containing(plain, "9386a873") is None


@pytest.mark.unit
class TestReleasedCheckValueParsing:
    """The declared ``check_type: released`` surface."""

    def test_single_citation(self) -> None:
        citations, err = parse_released_check_value(
            "OmniNode-ai/omnimarket@021b29cacc8c5711ea83d132df63f30e10b26fca"
        )
        assert err is None
        assert len(citations) == 1
        assert citations[0].repo == "OmniNode-ai/omnimarket"
        assert citations[0].merge_sha.startswith("021b29ca")

    def test_multiple_citations_comma_and_whitespace_separated(self) -> None:
        citations, err = parse_released_check_value(
            "OmniNode-ai/omnimarket@021b29ca, OmniNode-ai/omnibase_core@9386a873\n"
        )
        assert err is None
        assert [c.repo for c in citations] == [
            "OmniNode-ai/omnimarket",
            "OmniNode-ai/omnibase_core",
        ]

    @pytest.mark.parametrize(
        "value",
        ["", "   ", "omnimarket@021b29ca", "OmniNode-ai/omnimarket@zzz", "#2370"],
    )
    def test_malformed_values_are_errors_not_shorter_lists(self, value: str) -> None:
        """A partially parseable value must not silently drop a citation.

        Dropping one would drop exactly the merge nobody released, which is the
        defect this check exists to find.
        """
        citations, err = parse_released_check_value(value)
        assert citations == ()
        assert err is not None

    def test_one_bad_token_rejects_the_whole_value(self) -> None:
        citations, err = parse_released_check_value(
            "OmniNode-ai/omnimarket@021b29ca not-a-citation"
        )
        assert citations == ()
        assert err is not None
        assert "not-a-citation" in err


@pytest.mark.unit
class TestGitTagProbeAgainstARealRepo:
    """The live git probe, against a temp repo. Hermetic: no network, no clone."""

    @staticmethod
    def _git(root, *args: str) -> str:
        import subprocess

        return subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    def _repo(self, tmp_path):
        root = tmp_path / "repo"
        root.mkdir()
        self._git(root, "init", "-q", "-b", "dev")
        self._git(root, "config", "user.email", "t@example.invalid")
        self._git(root, "config", "user.name", "t")
        (root / "a.txt").write_text("one", encoding="utf-8")
        self._git(root, "add", "a.txt")
        self._git(root, "commit", "-qm", "one")
        tagged = self._git(root, "rev-parse", "HEAD")
        self._git(root, "tag", "v1.0.0")
        (root / "b.txt").write_text("two", encoding="utf-8")
        self._git(root, "add", "b.txt")
        self._git(root, "commit", "-qm", "two")
        untagged = self._git(root, "rev-parse", "HEAD")
        return root, tagged, untagged

    def test_tagged_commit_reports_its_tag(self, tmp_path) -> None:
        root, tagged, _ = self._repo(tmp_path)
        assert git_release_tags_containing(root, tagged) == ("v1.0.0",)

    def test_commit_after_the_tag_reports_an_empty_tuple(self, tmp_path) -> None:
        """A definite zero — and the test above is its positive control."""
        root, _, untagged = self._repo(tmp_path)
        assert git_release_tags_containing(root, untagged) == ()

    def test_a_sha_the_clone_does_not_know_is_none_not_empty(self, tmp_path) -> None:
        """The distinction the whole fail-closed posture rests on."""
        root, _, _ = self._repo(tmp_path)
        assert (
            git_release_tags_containing(
                root, "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
            )
            is None
        )


@pytest.mark.integration
@pytest.mark.slow
class TestReleasedEvidenceLive:
    """Live counterparts of the recordings above. Never runs in the unit lane."""

    def test_live_index_read_matches_the_recording(self) -> None:
        served = pypi_release_files("omnibase-core", "0.47.4")
        assert served is not None
        assert served >= REQUIRED_INDEX_PACKAGE_TYPES

    def test_live_index_negative_control(self) -> None:
        """A real distribution, a version it does not serve => a definite empty."""
        assert pypi_release_files("omnibase-core", "0.99.99") == frozenset()
