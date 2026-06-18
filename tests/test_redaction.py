"""Sensitive field redaction tests."""
from __future__ import annotations

from ai_employee.common_schemas.redaction import RedactionConfig, redact_dict, redact_text


def test_redacts_chinese_phone() -> None:
    out = redact_text("联系运维: 13800138000 紧急")
    assert "13800138000" not in out
    assert "***" in out


def test_redacts_international_phone() -> None:
    out = redact_text("hotline: +1-202-555-0184")
    assert "202-555-0184" not in out


def test_redacts_email() -> None:
    out = redact_text("admin@example.com 看到告警")
    assert "admin@example.com" not in out
    assert "***" in out


def test_redacts_chinese_id_card() -> None:
    out = redact_text("身份证 110101199003078811")
    assert "110101199003078811" not in out


def test_redacts_ipv4() -> None:
    out = redact_text("来源 IP: 192.168.1.100")
    assert "192.168.1.100" not in out


def test_redacts_bearer_token() -> None:
    out = redact_text("Authorization: Bearer abc123def456ghi789")
    assert "abc123def456ghi789" not in out


def test_preserves_non_sensitive_text() -> None:
    text = "5G 小区 RRC 失败，请检查告警 KPI"
    out = redact_text(text)
    assert out == text


def test_config_can_disable_patterns() -> None:
    cfg = RedactionConfig(
        redact_phone=False, redact_email=False, redact_id_card=False,
        redact_ip=False, redact_token=False,
    )
    text = "13800138000 admin@example.com 110101199003078811 192.168.1.100"
    out = redact_text(text, cfg)
    assert out == text


def test_custom_pattern_overrides() -> None:
    cfg = RedactionConfig(custom_patterns=[r"SECRET-\w+"])
    out = redact_text("server SECRET-ABC123 crashed", cfg)
    assert "SECRET-ABC123" not in out
    assert "***" in out


def test_redact_dict_masks_selected_fields() -> None:
    data = {
        "ticket_id": "T-001",
        "contact_email": "user@example.com",
        "summary": "5G 小区 RRC 失败",
    }
    out = redact_dict(data, fields=["contact_email"])
    assert out["contact_email"] != "user@example.com"
    assert "***" in out["contact_email"]
    assert out["ticket_id"] == "T-001"
    assert out["summary"] == "5G 小区 RRC 失败"


def test_empty_text_returns_empty() -> None:
    assert redact_text("") == ""


def test_redaction_idempotent() -> None:
    """Redacting twice produces the same output as redacting once."""
    text = "email user@example.com phone 13800138000"
    once = redact_text(text)
    twice = redact_text(once)
    assert once == twice
