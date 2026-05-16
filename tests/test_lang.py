from __future__ import annotations

import pytest

from voicebridge.lang import LanguageRejected, validate


def test_validate_english_titlecase() -> None:
    assert validate("English") == "en"


def test_validate_english_uppercase() -> None:
    assert validate("ENGLISH") == "en"


def test_validate_spanish() -> None:
    assert validate("Spanish") == "es"


def test_validate_french_rejected() -> None:
    with pytest.raises(LanguageRejected) as exc_info:
        validate("French")
    assert "French" in str(exc_info.value)
