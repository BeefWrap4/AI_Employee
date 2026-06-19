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
        redact_phone=False,
        redact_email=False,
        redact_id_card=False,
        redact_ip=False,
        redact_token=False,
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


# --------------------------------------------------------------------------- #
# R24-C: IMSI + password field redaction
# --------------------------------------------------------------------------- #


def test_redacts_china_imsi() -> None:
    """China mobile IMSI starts with 4600 + 11 digits (MCC+MNC+MSIN)."""
    out = redact_text("subscriber IMSI 460001234567890 attached")
    assert "460001234567890" not in out
    assert "***" in out


def test_redacts_imsi_with_label() -> None:
    """IMSI field labels should also be masked."""
    out = redact_text("imsi=460020000000123 fault")
    assert "460020000000123" not in out


def test_config_can_disable_imsi() -> None:
    cfg = RedactionConfig(redact_imsi=False)
    out = redact_text("IMSI 460001234567890", cfg)
    assert "460001234567890" in out


def test_redacts_password_field_dict() -> None:
    """Password/passwd/secret/token field values replaced by ***REDACTED***."""
    data = {
        "username": "alice",
        "password": "hunter2",
        "api_token": "tk_abc123def456ghi789",
        "secret": "shh",
    }
    out = redact_dict(data, fields=["password", "api_token", "secret"])
    assert out["password"] == "***REDACTED***"
    assert out["api_token"] == "***REDACTED***"
    assert out["secret"] == "***REDACTED***"
    assert out["username"] == "alice"


def test_redacts_password_case_insensitive() -> None:
    data = {"Passwd": "x", "API_SECRET": "y"}
    out = redact_dict(data, fields=["Passwd", "API_SECRET"])
    assert out["Passwd"] == "***REDACTED***"
    assert out["API_SECRET"] == "***REDACTED***"


def test_config_can_disable_password_redaction() -> None:
    cfg = RedactionConfig(redact_password=False)
    data = {"password": "hunter2"}
    out = redact_dict(data, fields=["password"], cfg=cfg)
    assert out["password"] == "hunter2"


def test_redact_dict_recursive_nested() -> None:
    """redact_dict must descend into nested dicts for matching field names."""
    data = {
        "ticket_id": "T-001",
        "contact": {
            "email": "user@example.com",
            "phone": "13800138000",
        },
    }
    out = redact_dict(data, fields=["email", "phone", "contact_email"])
    assert "user@example.com" not in str(out)
    assert "13800138000" not in str(out)
    assert out["contact"]["email"] != "user@example.com"
    assert "***" in out["contact"]["email"]


def test_redact_dict_recursive_list_of_dicts() -> None:
    """redact_dict must also walk into lists of dicts."""
    data = {
        "users": [
            {"name": "alice", "phone": "13800138000"},
            {"name": "bob", "phone": "13900139000"},
        ]
    }
    out = redact_dict(data, fields=["phone"])
    assert "13800138000" not in str(out)
    assert "13900139000" not in str(out)
    assert out["users"][0]["name"] == "alice"


def test_redact_dict_default_keeps_backward_compat() -> None:
    """Existing call style (single-level dict) must still work."""
    data = {"contact_email": "user@example.com", "summary": "ok"}
    out = redact_dict(data, fields=["contact_email"])
    assert out["contact_email"] != "user@example.com"
    assert out["summary"] == "ok"
