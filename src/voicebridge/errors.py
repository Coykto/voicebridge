"""Map Swift-side startup error codes to user-facing messages and exit codes.

The closed set of codes emitted by the Swift capture binary lives in
``HotkeyMonitor.swift``: ``accessibility_denied``, ``conflict``, ``param_err``,
``unknown_key:<k>``, ``unknown_osstatus_<n>``. Anything outside that set is
still handled with the generic catch-all so the orchestrator never silently
hangs on an unrecognized token.
"""

from __future__ import annotations

_ACCESSIBILITY_MESSAGE = (
    "Accessibility permission is required for the push-to-talk hotkey. "
    "Grant it in System Settings → Privacy & Security → Accessibility, "
    "then re-run."
)

_CONFLICT_MESSAGE = (
    "Another app is already using ⌥⌘T as a global shortcut. "
    "Quit it or unbind the shortcut, then re-run."
)


def lookup(code: str) -> tuple[str, int]:
    """Return ``(message, exit_code)`` for a Swift-side startup error token."""

    match code:
        case "accessibility_denied":
            return _ACCESSIBILITY_MESSAGE, 2
        case "conflict":
            return _CONFLICT_MESSAGE, 1
        case _:
            return f"hotkey could not be registered: {code}", 1
