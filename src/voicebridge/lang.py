from __future__ import annotations

SUPPORTED: dict[str, str] = {"english": "en", "spanish": "es"}


class LanguageRejected(Exception):
    def __init__(self, value: str) -> None:
        super().__init__(value)
        self.value = value

    def __str__(self) -> str:
        return self.value


def validate(name: str) -> str:
    if not name:
        raise LanguageRejected(name)
    iso = SUPPORTED.get(name.strip().lower())
    if iso is None:
        raise LanguageRejected(name)
    return iso
