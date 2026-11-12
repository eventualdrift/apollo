"""The retrieval suite in CI (spec J.4, O.1/24): no model, fully deterministic.

Each case file's fixture is loaded into a database of its own. The suite runs
at the default settings and must not get worse than the recorded baseline:
no metric drops and no case that passes there starts failing.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from apollo.evals.retrieval import (
    SPLITS,
    corpus_digest,
    load_case_files,
    metrics,
    run_file,
)
from apollo.memory.retrieval import DEFAULT_SETTINGS

pytestmark = pytest.mark.integration

ROOT = pathlib.Path(__file__).resolve().parents[2] / "evals" / "retrieval"
HIGHER_IS_BETTER = ("passed", "recall@5", "precision@5", "correct_empty_rate")


def test_the_retrieval_suite_does_not_regress(database_factory) -> None:
    baseline = json.loads((ROOT / "baseline.json").read_text())
    assert baseline["settings"] == {
        "text_search_config": DEFAULT_SETTINGS.text_search_config,
        "lexical_floor": DEFAULT_SETTINGS.lexical_floor,
    }, "the baseline was recorded at different settings; regenerate it deliberately"

    case_files = load_case_files(ROOT / "cases")
    assert corpus_digest(case_files) == baseline["corpus_sha256"], (
        "the cases changed; regenerate the baseline deliberately, with the reason in the commit"
    )

    results = []
    for case_file in case_files:
        results += run_file(database_factory(), case_file, (DEFAULT_SETTINGS,))[DEFAULT_SETTINGS]

    report = {split: metrics([r for r in results if r.case.split == split]).as_dict()
              for split in SPLITS}
    print(json.dumps(report, indent=2))  # visible with -s, and in CI logs on failure
    for split in SPLITS:
        recorded, now = baseline["splits"][split], report[split]
        assert now["cases"] == recorded["cases"], f"{split}: the corpus changed"
        for metric in HIGHER_IS_BETTER:
            assert now[metric] >= recorded[metric], (split, metric, now[metric], recorded[metric])
        assert now["mean_rank_of_expected"] <= recorded["mean_rank_of_expected"], split

    failing = {r.case.id: r.failures for r in results if not r.passed}
    unexpected = {k: v for k, v in failing.items() if k not in baseline["known_failures"]}
    assert not unexpected, unexpected
