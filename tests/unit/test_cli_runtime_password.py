"""How `apollo provision` obtains the runtime role password: never from argv."""

from __future__ import annotations

import io

import pytest

from apollo.cli.main import _read_runtime_password, main
from apollo.errors import ConfigError


class _Terminal(io.StringIO):
    def isatty(self) -> bool:
        return True


def _no_prompt(prompt: str = "") -> str:
    raise AssertionError("getpass must not be called")


def test_omitted_means_no_password(monkeypatch) -> None:
    monkeypatch.setattr("getpass.getpass", _no_prompt)
    assert _read_runtime_password(False, io.StringIO("ignored\n"), {}) is None


def test_a_pipe_supplies_the_first_line_without_its_newline(monkeypatch) -> None:
    monkeypatch.setattr("getpass.getpass", _no_prompt)
    stdin = io.StringIO("s3cret with spaces \r\nsecond line\n")
    assert _read_runtime_password(True, stdin, {}) == "s3cret with spaces "


def test_a_terminal_gets_a_no_echo_prompt(monkeypatch) -> None:
    prompts: list[str] = []

    def fake_getpass(prompt: str = "") -> str:
        prompts.append(prompt)
        return "typed"

    monkeypatch.setattr("getpass.getpass", fake_getpass)
    assert _read_runtime_password(True, _Terminal("not read\n"), {}) == "typed"
    assert prompts == ["runtime role password: "]


def test_an_empty_pipe_yields_an_empty_password_for_provisioning_to_refuse() -> None:
    assert _read_runtime_password(True, io.StringIO(""), {}) == ""


def test_end_of_input_at_the_prompt_yields_an_empty_password(monkeypatch) -> None:
    def eof(prompt: str = "") -> str:
        raise EOFError

    monkeypatch.setattr("getpass.getpass", eof)
    assert _read_runtime_password(True, _Terminal(""), {}) == ""


def test_the_old_environment_variable_is_refused_not_ignored() -> None:
    with pytest.raises(ConfigError, match="--password-stdin"):
        _read_runtime_password(False, io.StringIO(""), {"APOLLO_RUNTIME_PASSWORD": "x"})


def test_password_is_no_longer_a_command_line_option(capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["provision", "--password", "hunter2"])
    assert exc.value.code == 2
    assert "unrecognized arguments" in capsys.readouterr().err
