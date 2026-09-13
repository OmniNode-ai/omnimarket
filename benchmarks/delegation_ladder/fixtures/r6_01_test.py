from subject import _is_occ_repo


def test_bare_canonical_name_is_the_occ_repo() -> None:
    assert _is_occ_repo("onex_change_control") is True


def test_qualified_canonical_name_is_the_occ_repo() -> None:
    assert _is_occ_repo("OmniNode-ai/onex_change_control") is True


def test_trailing_slash_and_padding_are_tolerated() -> None:
    assert _is_occ_repo("  OmniNode-ai/onex_change_control/ ") is True


def test_same_short_name_under_a_different_owner_is_not_the_occ_repo() -> None:
    assert _is_occ_repo("other-org/onex_change_control") is False


def test_an_unrelated_repo_is_not_the_occ_repo() -> None:
    assert _is_occ_repo("OmniNode-ai/omnibase_core") is False
