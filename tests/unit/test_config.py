"""`load_config`: what `apollo.toml` may say, and what is refused before anything runs."""

from __future__ import annotations

import pathlib

import pytest

from apollo.config import load_config
from apollo.errors import ConfigError

REPO = pathlib.Path(__file__).resolve().parents[2]
ENV = {"APOLLO_DATABASE_DSN": "host=example dbname=apollo user=apollo_app"}
MINIMAL = '[brains]\ndefault = "fake"\n\n[providers.fake]\nkind = "fake"\n'


def _config(tmp_path: pathlib.Path, core: str = ""):
    path = tmp_path / "apollo.toml"
    path.write_text(f"[core]\n{core}\n{MINIMAL}", encoding="utf-8")
    return load_config(path, ENV)


def test_the_committed_config_loads_and_holds_no_secret() -> None:
    config = load_config(REPO / "apollo.toml", ENV)
    assert config.runtime_role == "apollo_app"
    assert config.proposal_expiry_days == 7
    text = (REPO / "apollo.toml").read_text(encoding="utf-8")
    # The DSN and any key come from the environment (spec K.3).
    assert "password" not in text.lower() and "dsn" not in text.lower()


def test_a_proposal_expires_after_seven_days_unless_the_config_says_otherwise(tmp_path) -> None:
    """Spec D.3/7: default seven days."""
    assert _config(tmp_path).proposal_expiry_days == 7
    assert _config(tmp_path, "proposal_expiry_days = 3").proposal_expiry_days == 3


@pytest.mark.parametrize("value", ["0", "-1", "2.5", '"7"', "true"])
def test_an_expiry_that_is_not_a_whole_number_of_days_is_refused(tmp_path, value: str) -> None:
    """Zero would expire a proposal before it could be answered; refuse it at start."""
    with pytest.raises(ConfigError, match="proposal_expiry_days"):
        _config(tmp_path, f"proposal_expiry_days = {value}")
