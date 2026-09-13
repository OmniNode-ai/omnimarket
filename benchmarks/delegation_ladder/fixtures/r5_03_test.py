from subject import validate_import_path_format


def test_single_segment_path_is_rejected() -> None:
    valid, message = validate_import_path_format("singlemodule")
    assert valid is False
    assert message == "Import path must include module and class (at least 2 segments)"


def test_two_segment_path_is_accepted() -> None:
    assert validate_import_path_format("mypackage.Handler") == (True, None)
