"""Small rules that keep a failure's record truthful and its text out of sight."""

from __future__ import annotations

import io
import logging

from apollo.errors import ErrorKind
from apollo.logging_setup import configure_logging, redaction_installed
from apollo.sanitise import error_detail, error_kind


def test_a_keyboard_interrupt_is_an_interruption_not_an_internal_error() -> None:
    assert error_kind(KeyboardInterrupt()) is ErrorKind.INTERRUPTED
    assert error_detail(KeyboardInterrupt("typed text")) == "exception_type=KeyboardInterrupt"
    # Anything else that is not Apollo's own error is still internal.
    assert error_kind(RuntimeError("x")) is ErrorKind.INTERNAL
    assert error_kind(SystemExit(1)) is ErrorKind.INTERNAL


def test_redaction_is_known_to_be_installed_only_after_logging_is_configured() -> None:
    """Before that, Python's last-resort handler would print an exception's text."""
    root = logging.getLogger()
    saved = list(root.handlers)
    try:
        for handler in saved:
            root.removeHandler(handler)
        assert not redaction_installed()
        root.addHandler(logging.StreamHandler(io.StringIO()))  # someone else's handler
        assert not redaction_installed()
        configure_logging("INFO", stream=io.StringIO())
        assert redaction_installed()
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
        for handler in saved:
            root.addHandler(handler)
