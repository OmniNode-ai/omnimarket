# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
#
# SPDX-License-Identifier: MIT

import os

import pytest

from omnimarket.nodes.node_onex_setup_effect.models.model_setup_banner import (
    ModelSetupBanner,
)


def test_banner_contains_exact_title_once():
    """Ensure the literal title appears exactly once for accessibility."""
    banner = ModelSetupBanner.build(width=80)
    text = banner.text
    assert "Welcome To OmniNode Delegation Onboarding" in text
    assert text.count("Welcome To OmniNode Delegation Onboarding") == 1


def test_banner_ascii_only_no_ansi():
    """Reject accidental Unicode or ANSI escape sequences."""
    banner = ModelSetupBanner.build(width=80)
    text = banner.text
    # All characters must be printable ASCII
    for ch in text:
        assert ord(ch) < 128, f"Non-ASCII character found: {ch!r}"
    # No ANSI escape sequences
    assert "\x1b[" not in text
    assert "\x1b" not in text


def test_banner_max_line_length_respected():
    """Every line must not exceed the requested width."""
    for width in [24, 60, 80, 120]:
        banner = ModelSetupBanner.build(width=width)
        for line in banner.text.splitlines():
            assert len(line) <= width, f"Line exceeds width {width}: {line!r}"


def test_width_80_has_sufficient_lines():
    """Width 80 should render large ASCII art with >15 non-blank lines."""
    banner = ModelSetupBanner.build(width=80)
    non_blank = [line for line in banner.text.splitlines() if line.strip()]
    assert len(non_blank) > 15


def test_narrow_width_24_retains_all_title_words_in_order():
    """At width 24, all title words must appear individually in the text."""
    banner = ModelSetupBanner.build(width=24)
    text = banner.text
    words = ["Welcome", "To", "OmniNode", "Delegation", "Onboarding"]
    positions = []
    for word in words:
        idx = text.find(word)
        assert idx != -1, f"Word {word!r} not found in narrow banner"
        positions.append(idx)
    # Words must appear in order
    assert positions == sorted(positions)
    # Max line length
    for line in text.splitlines():
        assert len(line) <= 24


def test_width_below_16_raises_value_error():
    """Widths 0 and 15 cannot fit 'Onboarding' (10 chars) + box padding (4)."""
    for width in [0, 15]:
        with pytest.raises(ValueError, match="width"):
            ModelSetupBanner.build(width=width)


def test_deterministic_render():
    """Repeated builds at the same width must produce byte-identical output."""
    b1 = ModelSetupBanner.build(width=80)
    b2 = ModelSetupBanner.build(width=80)
    assert b1.text == b2.text


def test_no_file_side_effects(tmp_path, monkeypatch):
    """Module must be pure: no file writes to cwd or tmp_path."""
    monkeypatch.chdir(tmp_path)
    before = set(os.listdir(tmp_path))
    ModelSetupBanner.build(width=80)
    after = set(os.listdir(tmp_path))
    assert before == after
