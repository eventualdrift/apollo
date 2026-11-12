"""The retrieval suite in CI (spec J.4, O.1/24): no model, fully deterministic.

Each case file's fixture is loaded into a database of its own, and its cases
run under every text-search configuration retrieval supports (spec E.4), at
the default floor. Each configuration must not get worse than its recorded
baseline: no metric drops, no case that passes there starts failing, and a
case that fails there fails in no new way. Recording both is also the
`english`/`simple` comparison O.1/24 asks for.
"""

from __future__ import annotations

import dataclasses
import json
import pathlib
from typing import Any

import pytest

from apollo.evals.retrieval import (
    SPLITS,
    CaseResult,
    corpus_digest,
    load_case_files,
    metrics,
    run_file,
    unexpected_failures,
)
from apollo.memory.retrieval import DEFAULT_SETTINGS
from apollo.storage.repositories.retrieval import TEXT_SEARCH_CONFIGS

pytestmark = pytest.mark.integration

ROOT = pathlib.Path(__file__).resolve().parents[2] / "evals" / "retrieval"
HIGHER_IS_BETTER = ("passed", "recall@5", "precision@5", "correct_empty_rate")


def test_the_retrieval_suite_does_not_regress(database_factory) -> None:
    baseline = json.loads((ROOT / "baseline.json").read_text())
    assert (baseline["lexical_floor"], baseline["default_text_search_config"]) == (
        DEFAULT_SETTINGS.lexical_floor, DEFAULT_SETTINGS.text_search_config,
    ), "the baseline was recorded at different settings; regenerate it deliberately"
    assert set(baseline["configs"]) == set(TEXT_SEARCH_CONFIGS), (
        "every supported text-search configuration needs a baseline"
    )

    case_files = load_case_files(ROOT / "cases")
    assert corpus_digest(case_files) == baseline["corpus_sha256"], (
        "the cases changed; regenerate the baseline deliberately, with the reason in the commit"
    )

    settings = {config: dataclasses.replace(DEFAULT_SETTINGS, text_search_config=config)
                for config in TEXT_SEARCH_CONFIGS}
    results: dict[str, list[CaseResult]] = {config: [] for config in settings}
    for case_file in case_files:
        by_settings = run_file(database_factory(), case_file, tuple(settings.values()))
        for config, value in settings.items():
            results[config] += by_settings[value]

    for config in TEXT_SEARCH_CONFIGS:
        _check(config, baseline["configs"][config], results[config])


def _check(config: str, recorded: dict[str, Any], results: list[CaseResult]) -> None:
    report = {split: metrics([r for r in results if r.case.split == split]).as_dict()
              for split in SPLITS}
    print(config, json.dumps(report, indent=2))  # visible with -s, and in CI logs on failure
    for split in SPLITS:
        before, now = recorded["splits"][split], report[split]
        assert now["cases"] == before["cases"], f"{config}/{split}: the corpus changed"
        for metric in HIGHER_IS_BETTER:
            assert now[metric] >= before[metric], (config, split, metric, now[metric],
                                                   before[metric])
        assert now["mean_rank_of_expected"] <= before["mean_rank_of_expected"], (config, split)

    known = {case_id: entry["failures"] for case_id, entry in recorded["known_failures"].items()}
    unexpected = unexpected_failures(results, known)
    assert not unexpected, (config, unexpected)
