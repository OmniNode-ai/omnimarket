from omnimarket.scratch_s3_probe import answer


def test_answer_is_42() -> None:
    assert answer() == 42
