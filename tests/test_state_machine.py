import pytest
from ai_employee.knowledge_api.store import (
    ALLOWED_TRANSITIONS,
    IllegalTransitionError,
    transition_parse_status,
)


def test_all_six_states_are_keys() -> None:
    assert set(ALLOWED_TRANSITIONS) == {
        "uploaded",
        "parsing",
        "parse_failed",
        "ready",
        "published",
        "archived",
    }


def test_legal_transitions() -> None:
    assert "parsing" in ALLOWED_TRANSITIONS["uploaded"]
    assert "ready" in ALLOWED_TRANSITIONS["parsing"]
    assert "parse_failed" in ALLOWED_TRANSITIONS["parsing"]
    assert "uploaded" in ALLOWED_TRANSITIONS["parse_failed"]
    assert "published" in ALLOWED_TRANSITIONS["ready"]
    assert "archived" in ALLOWED_TRANSITIONS["published"]
    assert "published" in ALLOWED_TRANSITIONS["archived"]


def test_published_cannot_reparse() -> None:
    assert "uploaded" not in ALLOWED_TRANSITIONS["published"]
    assert "parsing" not in ALLOWED_TRANSITIONS["published"]


def test_illegal_transition_raises() -> None:
    with pytest.raises(IllegalTransitionError):
        transition_parse_status("uploaded", "published")


def test_illegal_transition_from_ready_to_parsing() -> None:
    with pytest.raises(IllegalTransitionError):
        transition_parse_status("ready", "parsing")
