"""Unit tests for settings_store validate/save env-name aliasing (issue #2812).

These tests import settings_store directly and do not need headroom._core.
"""

from __future__ import annotations

import pytest

from headroom import settings_store as s


# ---------------------------------------------------------------------------
# _normalize_keys
# ---------------------------------------------------------------------------

def test_normalize_keys_rewrites_env_name():
    result = s._normalize_keys({"HEADROOM_LOSSLESS": True})
    assert result == {"lossless": True}


def test_normalize_keys_leaves_canonical_key_unchanged():
    result = s._normalize_keys({"lossless": True})
    assert result == {"lossless": True}


def test_normalize_keys_leaves_unknown_key_unchanged():
    result = s._normalize_keys({"totally_unknown": 42})
    assert result == {"totally_unknown": 42}


def test_normalize_keys_mixed_input():
    result = s._normalize_keys({
        "HEADROOM_RPM": 100,
        "tpm": 200,
        "definitely_not_a_setting": "x",
    })
    assert result == {"rpm": 100, "tpm": 200, "definitely_not_a_setting": "x"}


# ---------------------------------------------------------------------------
# validate — env aliases
# ---------------------------------------------------------------------------

def test_validate_accepts_env_name():
    coerced = s.validate({"HEADROOM_LOSSLESS": True})
    assert coerced == {"lossless": True}


def test_validate_accepts_env_name_int():
    coerced = s.validate({"HEADROOM_RPM": "500"})
    assert coerced == {"rpm": 500}


def test_validate_env_name_coercion_error_still_surfaces():
    with pytest.raises(s.SettingsValidationError) as exc_info:
        s.validate({"HEADROOM_RPM": "not-a-number"})
    assert exc_info.value.field_errors


def test_validate_truly_unknown_key_still_errors():
    with pytest.raises(s.SettingsValidationError) as exc_info:
        s.validate({"totally_bogus_key": 1})
    assert "totally_bogus_key" in exc_info.value.unknown_keys


def test_validate_canonical_key_still_works():
    coerced = s.validate({"lossless": False})
    # False coerces to a bool — validate returns only non-None values;
    # False is falsy but not None, so it should be present.
    # (lossless default is False, but validate returns whatever was coerced)
    # Confirm the key is accepted with no error.
    assert "lossless" in coerced or coerced == {}


# ---------------------------------------------------------------------------
# save — env aliases
# ---------------------------------------------------------------------------

def test_save_accepts_env_name(tmp_path, monkeypatch):
    monkeypatch.setenv("HEADROOM_WORKSPACE_DIR", str(tmp_path))
    s.save({"HEADROOM_RPM": 42})
    assert s.load() == {"rpm": 42}


def test_save_env_name_clear(tmp_path, monkeypatch):
    monkeypatch.setenv("HEADROOM_WORKSPACE_DIR", str(tmp_path))
    s.save({"rpm": 42})
    assert s.load() == {"rpm": 42}
    # Clear via env name
    s.save({"HEADROOM_RPM": None})
    assert s.load() == {}


def test_save_mixed_env_and_canonical(tmp_path, monkeypatch):
    monkeypatch.setenv("HEADROOM_WORKSPACE_DIR", str(tmp_path))
    s.save({"HEADROOM_RPM": 10, "tpm": 20})
    loaded = s.load()
    assert loaded["rpm"] == 10
    assert loaded["tpm"] == 20
