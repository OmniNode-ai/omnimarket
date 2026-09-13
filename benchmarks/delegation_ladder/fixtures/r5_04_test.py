from r4_04_subject import extract_imports


def test_full_dotted_paths_are_returned() -> None:
    source = "import foo.bar\nfrom baz.qux import thing\n"
    assert extract_imports(source) == ["foo.bar", "baz.qux"]


def test_invalid_source_returns_empty_list() -> None:
    assert extract_imports("def f(:\n  pass") == []
