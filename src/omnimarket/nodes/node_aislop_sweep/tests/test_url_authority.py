# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-12818 — url-authority ratchet gate (PR-2 of OMN-12803).

Every URL/endpoint must resolve from a contract. The gate flags public HTTPS
endpoint literals and *_URL/*_ENDPOINT env reads, leaves api-key/token reads and
config-PATH reads legal, honors the # url-authority-ok: annotation and the
authority-file path allowlist, and applies a burn-down-only ratchet baseline.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnimarket.nodes.node_aislop_sweep.handlers.url_authority import (
    assert_baseline_shrinks_only,
    load_baseline,
    make_fingerprint,
    partition_against_baseline,
    scan_source,
    scan_tree,
    serialize_baseline,
)

_REPO = "omnimarket"
_SRC = "src/omnimarket/nodes/node_x/handlers/handler_x.py"


def _rules(source: str, path: str = _SRC) -> list[str]:
    return [v.rule for v in scan_source(_REPO, path, source)]


@pytest.mark.unit
class TestPublicHttpsLiteral:
    def test_flags_github_api_literal(self) -> None:
        src = '_GITHUB = "https://api.github.com/graphql"\n'
        # Const-assignment rule wins for a NAME-bearing constant; either way it's flagged.
        assert _rules(src), "a public GitHub API literal must be flagged"

    def test_flags_linear_api_literal_inline(self) -> None:
        src = 'def f():\n    return call("https://api.linear.app/graphql")\n'
        assert "public-https-literal" in _rules(src)

    def test_flags_googleapis_literal(self) -> None:
        src = 'URL = "https://generativelanguage.googleapis.com/v1beta/openai/"\n'
        assert _rules(src)

    def test_does_not_flag_localhost(self) -> None:
        # localhost/loopback are owned by the existing hardcoded-config rule.
        src = 'x = call("http://localhost:8000/health")\n'
        assert _rules(src) == []

    def test_does_not_flag_comment_or_docstring(self) -> None:
        src = '# see https://api.github.com for the REST base\n"""https://api.linear.app/graphql"""\n'
        assert _rules(src) == []


@pytest.mark.unit
class TestEnvUrlRead:
    def test_flags_environ_subscript_url(self) -> None:
        src = 'x = os.environ["LLM_CODER_URL"]\n'
        assert "env-url-read" in _rules(src)

    def test_flags_environ_get_endpoint(self) -> None:
        src = 'x = os.environ.get("OPENROUTER_BASE_ENDPOINT", "")\n'
        assert "env-url-read" in _rules(src)

    def test_flags_environ_get_url_with_default(self) -> None:
        src = 'x = os.environ.get("LLM_GLM_URL", "")\n'
        assert "env-url-read" in _rules(src)

    def test_does_not_flag_api_key_env_read(self) -> None:
        # Secrets-by-name stay legal: the regex targets _URL/_ENDPOINT only.
        src = 'k = os.environ["LINEAR_API_KEY"]\nt = os.environ.get("GITHUB_TOKEN")\n'
        assert _rules(src) == []

    def test_does_not_flag_config_path_read(self) -> None:
        src = 'p = os.environ.get("BIFROST_OVERLAY_PATH", "")  # contract-config-ok: config\n'
        assert _rules(src) == []


@pytest.mark.unit
class TestConstAssignment:
    def test_flags_url_const_from_env(self) -> None:
        src = 'DASHBOARD_URL = os.environ["OMNIDASH_API_URL"]\n'
        # env-url-read matches first (os.environ of a _URL var) — still flagged.
        assert _rules(src)

    def test_flags_url_const_from_literal(self) -> None:
        src = 'LINEAR_API_URL = "https://api.linear.app/graphql"\n'
        assert "url-const-assignment" in _rules(src)


@pytest.mark.unit
class TestSuppressionAndAllowlist:
    def test_url_authority_ok_annotation_suppresses(self) -> None:
        src = 'x = "https://api.github.com"  # url-authority-ok: OAuth discovery constant\n'
        assert _rules(src) == []

    def test_authority_file_path_is_allowlisted(self) -> None:
        # Literals inside the bifrost/catalog authority files are canonical.
        src = '  base_url: "https://api.linear.app/graphql"\n'
        bifrost = "src/omnimarket/configs/bifrost_delegation.yaml"
        catalog = "src/omnibase_infra/contracts/integrations/catalog.yaml"
        assert scan_source(_REPO, bifrost, src) == []
        assert scan_source("omnibase_infra", catalog, src) == []

    def test_test_files_excluded(self) -> None:
        src = 'x = "https://api.github.com/graphql"\n'
        assert scan_source(_REPO, "tests/test_thing.py", src) == []


@pytest.mark.unit
class TestRatchet:
    def _violation_fp(self, source: str) -> str:
        violations = scan_source(_REPO, _SRC, source)
        assert violations, "expected a violation to fingerprint"
        return violations[0].fingerprint

    def test_grandfathered_violation_passes(self) -> None:
        src = 'URL = "https://api.linear.app/graphql"\n'
        fp = self._violation_fp(src)
        new, grandfathered = partition_against_baseline(
            scan_source(_REPO, _SRC, src), baseline_fingerprints={fp}
        )
        assert new == []
        assert len(grandfathered) == 1

    def test_new_violation_fails(self) -> None:
        src = 'URL = "https://api.linear.app/graphql"\n'
        new, grandfathered = partition_against_baseline(
            scan_source(_REPO, _SRC, src), baseline_fingerprints=set()
        )
        assert len(new) == 1, (
            "a violation absent from the baseline must be NEW (fails gate)"
        )
        assert grandfathered == []

    def test_fingerprint_survives_line_shift(self) -> None:
        # Same offending content on different lines → same fingerprint (line-independent).
        a = 'URL = "https://api.linear.app/graphql"\n'
        b = "import os\n\n\n" + a
        assert self._violation_fp(a) == self._violation_fp(b)

    def test_fingerprint_differs_by_content(self) -> None:
        fp_linear = make_fingerprint(
            _REPO, _SRC, 'URL = "https://api.linear.app/graphql"'
        )
        fp_github = make_fingerprint(
            _REPO, _SRC, 'URL = "https://api.github.com/graphql"'
        )
        assert fp_linear != fp_github

    def test_baseline_shrink_only_allows_removal(self) -> None:
        before = {"a", "b", "c"}
        after = {"a", "b"}  # burned one down
        assert_baseline_shrinks_only(before, after)  # no raise

    def test_baseline_shrink_only_rejects_growth(self) -> None:
        before = {"a", "b"}
        after = {"a", "b", "c"}  # tried to whitelist fresh debt
        with pytest.raises(ValueError, match="baseline grew"):
            assert_baseline_shrinks_only(before, after)


@pytest.mark.unit
class TestBaselineIO:
    def test_scan_tree_finds_violations_skips_tests(self, tmp_path: Path) -> None:
        src_dir = tmp_path / "src" / "pkg"
        src_dir.mkdir(parents=True)
        (src_dir / "handler.py").write_text(
            'URL = "https://api.linear.app/graphql"\n', encoding="utf-8"
        )
        (src_dir / "test_handler.py").write_text(
            'URL = "https://api.github.com"\n', encoding="utf-8"
        )
        violations = scan_tree("omnimarket", tmp_path)
        assert len(violations) == 1
        assert violations[0].path.endswith("handler.py")
        assert "test_handler" not in violations[0].path

    def test_serialize_and_load_roundtrip(self, tmp_path: Path) -> None:
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        (src_dir / "a.py").write_text(
            'URL = "https://api.linear.app/graphql"\n', encoding="utf-8"
        )
        violations = scan_tree("omnimarket", tmp_path)
        doc = serialize_baseline(violations)
        assert doc["count"] == 1
        baseline_path = tmp_path / "baseline.json"
        baseline_path.write_text(json.dumps(doc), encoding="utf-8")

        loaded = load_baseline(baseline_path)
        assert loaded == {violations[0].fingerprint}

    def test_load_missing_baseline_is_empty(self, tmp_path: Path) -> None:
        assert load_baseline(tmp_path / "does-not-exist.json") == set()

    def test_serialize_dedups_identical_content(self, tmp_path: Path) -> None:
        # Two files with the SAME offending content in the SAME repo-relative path
        # collapse — but distinct paths stay distinct.
        v = scan_source(
            "omnimarket", "src/a.py", 'URL = "https://api.linear.app/graphql"\n'
        )
        doc = serialize_baseline(v + v)  # duplicate the same violation
        assert doc["count"] == 1

    def test_serialized_baseline_is_deterministic(self, tmp_path: Path) -> None:
        v = scan_source(
            "omnimarket", "src/a.py", 'URL = "https://api.github.com/graphql"\n'
        )
        assert serialize_baseline(v) == serialize_baseline(v)
