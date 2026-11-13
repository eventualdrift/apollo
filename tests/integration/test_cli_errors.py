"""What the terminal says when a command cannot run at all.

An exception's text can quote a refused row, a DSN or a provider's reply, so
the CLI prints a failure as its kind and never as a traceback. Configuration
errors are the exception: they are Apollo's own words about keys and variable
names, and they are what the person needs to read.

The owner commands (`migrate`, `provision`) connect with `APOLLO_ADMIN_DSN`
and must not ask for the runtime DSN as well: the README's first two commands
are run before a runtime role exists.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest

from apollo.cli import main as cli
from apollo.cli.main import main

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
SECRET = "zqxsecret7731"


@pytest.fixture()
def env(monkeypatch):
    monkeypatch.setenv("APOLLO_CONFIG", str(REPO_ROOT / "apollo.toml"))
    for name in ("APOLLO_DATABASE_DSN", "APOLLO_ADMIN_DSN", "APOLLO_RUNTIME_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    return monkeypatch


def _plain(err: str) -> list[str]:
    """What the person reads on stderr: everything that is not a JSON log line."""
    return [line for line in err.splitlines() if not line.startswith("{")]


def test_the_owner_commands_run_with_only_the_admin_dsn(env, fresh_database, capsys) -> None:
    env.setenv("APOLLO_ADMIN_DSN", fresh_database["owner_dsn"])
    env.setenv("APOLLO_RUNTIME_ROLE", fresh_database["role"])

    assert main(["migrate"]) == 0
    assert capsys.readouterr().out == "schema already up to date\n"

    assert main(["provision"]) == 0
    out = capsys.readouterr().out
    assert f"runtime role {fresh_database['role']}: updated" in out
    assert "superuser:              False" in out
    # The one check that needs the runtime connection says why it was skipped.
    assert (f"Couldn't check the PostgreSQL log settings as {fresh_database['role']}"
            " (APOLLO_DATABASE_DSN is not set)") in out


def test_an_owner_command_still_says_what_it_needs(env, capsys) -> None:
    assert main(["provision"]) == 2
    assert "APOLLO_ADMIN_DSN is not set" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [["new"], ["memory", "list"], ["proposal", "list"],
                                  ["turn", "list", "x"], ["doctor"]])
def test_a_command_that_needs_the_database_says_which_variable_is_missing(
    env, capsys, argv: list[str]
) -> None:
    assert main(argv) == 2
    out, err = capsys.readouterr()
    assert out == ""
    assert _plain(err) == [
        "apollo: APOLLO_DATABASE_DSN is not set (secrets come from the environment)"]


def test_a_missing_config_file_is_named(env, capsys, tmp_path) -> None:
    env.setenv("APOLLO_CONFIG", str(tmp_path / "nowhere.toml"))
    assert main(["new"]) == 2
    assert _plain(capsys.readouterr().err) == [
        f"apollo: config file not found: {tmp_path / 'nowhere.toml'}"]


def test_an_unexpected_failure_prints_its_kind_and_never_its_text(
    env, db, capsys
) -> None:
    env.setenv("APOLLO_DATABASE_DSN", db._dsn)

    def broken(config: Any) -> None:
        raise RuntimeError(f"refused row: ({SECRET})")

    env.setattr(cli, "_service", broken)
    assert main(["new"]) == 1
    out, err = capsys.readouterr()
    assert out == "" and _plain(err) == ["apollo: RuntimeError"]
    # The log line has the type and the frames, and still not the text.
    assert SECRET not in err and "Traceback" not in err
    assert '"exception_type": "RuntimeError"' in err and '"event": "cli.failed"' in err


def test_ctrl_c_is_not_swallowed(env, db) -> None:
    env.setenv("APOLLO_DATABASE_DSN", db._dsn)

    def interrupted(config: Any) -> None:
        raise KeyboardInterrupt

    env.setattr(cli, "_service", interrupted)
    with pytest.raises(KeyboardInterrupt):
        main(["new"])
