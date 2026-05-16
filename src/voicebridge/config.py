from __future__ import annotations

import os
from dataclasses import dataclass

import dotenv

from voicebridge import errors, lang


@dataclass(frozen=True)
class Config:
    api_key: str
    target_lang_name: str
    target_lang_iso: str


def load_config() -> Config:
    dotenv.load_dotenv()

    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        raise errors.MissingApiKey()

    target_lang_name = os.environ.get("VOICEBRIDGE_TARGET_LANG", "")
    if not target_lang_name:
        raise errors.InvalidTargetLang("")

    try:
        iso = lang.validate(target_lang_name)
    except lang.LanguageRejected as exc:
        raise errors.InvalidTargetLang(exc.value) from exc

    return Config(
        api_key=api_key,
        target_lang_name=target_lang_name,
        target_lang_iso=iso,
    )


def _redact(api_key: str) -> str:
    if len(api_key) < 4:
        return "sk-…****"
    return f"sk-…{api_key[-4:]}"


def main() -> None:
    try:
        cfg = load_config()
    except errors.ConfigError as exc:
        errors.handle(exc)
    print(
        f"Config(target_lang_name={cfg.target_lang_name!r}, "
        f"target_lang_iso={cfg.target_lang_iso!r}, "
        f"api_key={_redact(cfg.api_key)!r})"
    )


if __name__ == "__main__":
    main()
