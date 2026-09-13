import pytest
from subject import extract_content


def test_real_content_is_returned_with_its_usage_block() -> None:
    body = {
        "choices": [{"message": {"content": "the answer"}}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 2},
    }
    content, usage = extract_content(body)
    assert content == "the answer"
    assert usage == {"prompt_tokens": 5, "completion_tokens": 2}


def test_surrounding_whitespace_is_stripped() -> None:
    body = {"choices": [{"message": {"content": "  spaced  "}}]}
    content, _ = extract_content(body)
    assert content == "spaced"


def test_absent_choices_is_a_failure() -> None:
    with pytest.raises(ValueError):
        extract_content({"choices": []})


def test_null_content_is_a_failure_not_an_empty_success() -> None:
    with pytest.raises(ValueError):
        extract_content({"choices": [{"message": {"content": None}}]})


def test_whitespace_only_content_is_a_failure_not_an_empty_success() -> None:
    with pytest.raises(ValueError):
        extract_content({"choices": [{"message": {"content": "   "}}]})
