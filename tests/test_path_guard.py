from pathlib import Path

import pytest

from ai_employee.common_schemas.security import (
    UnsafeSourceUriError,
    assert_safe_source_uri,
)


def test_absolute_path_inside_raw_is_allowed(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    raw.mkdir()
    p = raw / "x.md"
    p.write_text("x", encoding="utf-8")
    out = assert_safe_source_uri(str(p), str(tmp_path))
    assert out == str(p.resolve())


def test_relative_path_rejected(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    raw.mkdir()
    with pytest.raises(UnsafeSourceUriError) as exc:
        assert_safe_source_uri("x.md", str(tmp_path))
    assert "absolute" in str(exc.value)


def test_path_outside_raw_rejected(tmp_path: Path) -> None:
    p = tmp_path / "x.md"
    p.write_text("x", encoding="utf-8")
    with pytest.raises(UnsafeSourceUriError) as exc:
        assert_safe_source_uri(str(p), str(tmp_path))
    assert "outside" in str(exc.value).lower()


def test_parent_traversal_rejected(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    raw.mkdir()
    p = raw / "x.md"
    p.write_text("x", encoding="utf-8")
    evil = raw / ".." / "raw_sub" / "x.md"
    with pytest.raises(UnsafeSourceUriError):
        assert_safe_source_uri(str(evil), str(tmp_path))


def test_raw_subdirectory_outside_raw_rejected(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    raw.mkdir()
    raw_sub = tmp_path / "raw_sub"
    raw_sub.mkdir()
    p = raw_sub / "x.md"
    p.write_text("x", encoding="utf-8")
    with pytest.raises(UnsafeSourceUriError):
        assert_safe_source_uri(str(p), str(tmp_path))


def test_symlink_escaping_raw_rejected(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    raw.mkdir()
    target = tmp_path / "secret.md"
    target.write_text("secret", encoding="utf-8")
    link = raw / "link.md"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlink not supported on this platform")
    with pytest.raises(UnsafeSourceUriError) as exc:
        assert_safe_source_uri(str(link), str(tmp_path))
    assert "outside" in str(exc.value).lower() or "symlink" in str(exc.value).lower()


def test_empty_data_dir_rejected(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    raw.mkdir()
    p = raw / "x.md"
    p.write_text("x", encoding="utf-8")
    with pytest.raises(UnsafeSourceUriError) as exc:
        assert_safe_source_uri(str(p), "")
    assert "data_dir" in str(exc.value).lower()


def test_nonexistent_data_dir_rejected(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    raw.mkdir()
    p = raw / "x.md"
    p.write_text("x", encoding="utf-8")
    fake_data_dir = str(tmp_path / "does_not_exist_xyz")
    with pytest.raises(UnsafeSourceUriError):
        assert_safe_source_uri(str(p), fake_data_dir)


def test_returns_resolved_absolute_path(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    raw.mkdir()
    p = raw / "x.md"
    p.write_text("x", encoding="utf-8")
    out = assert_safe_source_uri(str(p), str(tmp_path))
    assert Path(out).is_absolute()
    assert Path(out).resolve() == Path(p).resolve()
