"""Map Swift-side startup error codes to user-facing messages and exit codes.

The closed set of codes emitted by the Swift capture binary lives in
``HotkeyMonitor.swift`` and ``AudioCapture.swift``: ``accessibility_denied``,
``conflict``, ``param_err``, ``unknown_key:<k>``, ``unknown_osstatus_<n>``,
``microphone_denied``, ``mic_lost``. Anything outside that set is still handled
with the generic catch-all so the orchestrator never silently hangs on an
unrecognized token.
"""

from __future__ import annotations

import sys
from typing import NoReturn

_ACCESSIBILITY_MESSAGE = (
    "Accessibility permission is required for the push-to-talk hotkey. "
    "Grant it in System Settings → Privacy & Security → Accessibility, "
    "then re-run."
)

_CONFLICT_MESSAGE = (
    "Another app is already using ⌥⌘T as a global shortcut. "
    "Quit it or unbind the shortcut, then re-run."
)

_MICROPHONE_DENIED_MESSAGE = (
    "Microphone access is required. Grant it in System Settings → "
    "Privacy & Security → Microphone, then re-launch."
)


def lookup(code: str, *, message: str | None = None) -> tuple[str, int]:
    """Return ``(message, exit_code)`` for a Swift-side error token.

    ``message`` is the optional free-form text from the Swift ``error`` event;
    used by ``mic_lost`` to substitute the underlying reason into the printed
    message. Ignored for codes that don't carry a reason.
    """

    match code:
        case "accessibility_denied":
            return _ACCESSIBILITY_MESSAGE, 2
        case "conflict":
            return _CONFLICT_MESSAGE, 1
        case "microphone_denied":
            return _MICROPHONE_DENIED_MESSAGE, 4
        case "mic_lost":
            reason = message or "device removed"
            return (
                f"Microphone disconnected ({reason}). Restart the tool to "
                "use the current default input."
            ), 5
        case _:
            return f"hotkey could not be registered: {code}", 1


class ConfigError(Exception):
    pass


class MissingApiKey(ConfigError):
    code = "config_missing_api_key"


class InvalidTargetLang(ConfigError):
    code = "config_invalid_target_lang"

    def __init__(self, value: str) -> None:
        super().__init__(value)
        self.value = value

    def __str__(self) -> str:
        return self.value


class RealtimeError(Exception):
    pass


class NetworkUnreachable(RealtimeError):
    code = "network_unreachable"


class ApiKeyRejected(RealtimeError):
    code = "api_key_rejected"


class RealtimeServerError(RealtimeError):
    code = "realtime_server_error"


class ConnectionLost(RealtimeError):
    code = "connection_lost"


_EXIT_CODES: dict[str, int] = {
    MissingApiKey.code: 2,
    InvalidTargetLang.code: 2,
    NetworkUnreachable.code: 1,
    ApiKeyRejected.code: 1,
    RealtimeServerError.code: 1,
    ConnectionLost.code: 1,
}


def _message(exc: Exception) -> str:
    if isinstance(exc, MissingApiKey):
        return "error: OPENAI_API_KEY is not set (add it to .env)"
    if isinstance(exc, InvalidTargetLang):
        if not exc.value:
            return "error: VOICEBRIDGE_TARGET_LANG is not set"
        return (
            f"error: target language {exc.value!r} is not supported "
            f"(allowed: English, Spanish)"
        )
    if isinstance(exc, NetworkUnreachable):
        return "error: cannot reach OpenAI realtime endpoint (network unreachable)"
    if isinstance(exc, ApiKeyRejected):
        return "error: OpenAI rejected the API key"
    if isinstance(exc, RealtimeServerError):
        detail = str(exc).strip()
        if detail:
            return f"error: OpenAI realtime server returned an error: {detail}"
        return "error: OpenAI realtime server returned an error"
    if isinstance(exc, ConnectionLost):
        return "error: connection to OpenAI realtime endpoint was lost"
    raise AssertionError(f"no message for {type(exc).__name__}")


def handle(exc: Exception) -> NoReturn:
    if not isinstance(exc, (ConfigError, RealtimeError)):
        raise exc
    code = getattr(exc, "code", None)
    if code not in _EXIT_CODES:
        raise exc
    print(_message(exc), file=sys.stderr)
    sys.exit(_EXIT_CODES[code])
