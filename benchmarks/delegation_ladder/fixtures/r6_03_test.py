import pytest
from subject import DatabaseAdapter, HandlerProjectionDelegation


def _payload(**extra: object) -> dict[str, object]:
    base: dict[str, object] = {
        "_db": DatabaseAdapter(),
        "_event_type": "onex.evt.delegation-judge-verdict.v1",
        "delegation_id": "d-1",
        "verdict": "accept",
        "score": 0.9,
    }
    base.update(extra)
    return base


def test_a_clean_verdict_event_projects() -> None:
    result = HandlerProjectionDelegation().handle(_payload())
    assert result == {"projected": "d-1", "verdict": "accept"}


def test_an_envelope_only_topic_key_does_not_break_projection() -> None:
    result = HandlerProjectionDelegation().handle(
        _payload(_topic="onex.evt.delegation-judge-verdict.v1")
    )
    assert result == {"projected": "d-1", "verdict": "accept"}


def test_a_missing_database_adapter_is_still_a_type_error() -> None:
    payload = _payload()
    payload["_db"] = "not an adapter"
    with pytest.raises(TypeError):
        HandlerProjectionDelegation().handle(payload)


def test_a_genuinely_unknown_field_is_still_rejected() -> None:
    with pytest.raises(Exception):
        HandlerProjectionDelegation().handle(_payload(not_a_real_field="x"))


def test_an_unroutable_event_type_is_still_refused() -> None:
    payload = _payload()
    payload["_event_type"] = "onex.evt.something-else.v1"
    with pytest.raises(ValueError):
        HandlerProjectionDelegation().handle(payload)
