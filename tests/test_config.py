from __future__ import annotations

import pytest

import dotenv
from voicebridge import config as config_module
from voicebridge.config import Config, load_config
from voicebridge.errors import InvalidTargetLang, MissingApiKey


@pytest.fixture(autouse=True)
def _disable_dotenv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **kw: False)
    monkeypatch.setattr(config_module.dotenv, "load_dotenv", lambda *a, **kw: False)


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("VOICEBRIDGE_TARGET_LANG", raising=False)


def test_missing_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOICEBRIDGE_TARGET_LANG", "English")
    with pytest.raises(MissingApiKey):
        load_config()


def test_empty_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.setenv("VOICEBRIDGE_TARGET_LANG", "English")
    with pytest.raises(MissingApiKey):
        load_config()


def test_missing_target_lang(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test1234")
    with pytest.raises(InvalidTargetLang) as exc_info:
        load_config()
    assert exc_info.value.value == ""


def test_empty_target_lang(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test1234")
    monkeypatch.setenv("VOICEBRIDGE_TARGET_LANG", "")
    with pytest.raises(InvalidTargetLang) as exc_info:
        load_config()
    assert exc_info.value.value == ""


def test_invalid_target_lang_french(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test1234")
    monkeypatch.setenv("VOICEBRIDGE_TARGET_LANG", "French")
    with pytest.raises(InvalidTargetLang) as exc_info:
        load_config()
    assert "French" in str(exc_info.value)


def test_valid_english(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test1234")
    monkeypatch.setenv("VOICEBRIDGE_TARGET_LANG", "English")
    cfg = load_config()
    assert cfg == Config(
        api_key="sk-test1234",
        target_lang_name="English",
        target_lang_iso="en",
    )


def test_valid_spanish(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test1234")
    monkeypatch.setenv("VOICEBRIDGE_TARGET_LANG", "Spanish")
    cfg = load_config()
    assert cfg.target_lang_iso == "es"
    assert cfg.target_lang_name == "Spanish"


def test_mixed_case_english_uppercase(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test1234")
    monkeypatch.setenv("VOICEBRIDGE_TARGET_LANG", "ENGLISH")
    cfg = load_config()
    assert cfg.target_lang_iso == "en"
    assert cfg.target_lang_name == "ENGLISH"


def test_mixed_case_spanish_lowercase(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test1234")
    monkeypatch.setenv("VOICEBRIDGE_TARGET_LANG", "spanish")
    cfg = load_config()
    assert cfg.target_lang_iso == "es"
    assert cfg.target_lang_name == "spanish"
