from subject import is_valid_onex_name


def test_empty_name_is_rejected() -> None:
    assert is_valid_onex_name("") is False


def test_snake_case_name_is_accepted() -> None:
    assert is_valid_onex_name("http_client") is True
