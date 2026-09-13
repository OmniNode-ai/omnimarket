from subject import detect_add_remove_conflicts


def test_differently_cased_values_conflict_by_default() -> None:
    assert detect_add_remove_conflicts(["Foo"], ["foo"], "handlers") == ["foo"]


def test_disjoint_lists_do_not_conflict() -> None:
    assert detect_add_remove_conflicts(["foo"], ["bar"], "handlers") == []
