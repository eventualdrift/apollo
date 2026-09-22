# Running the persona suite

The suite answers one question: **does Apollo keep his behavioural contract when the reasoning model
changes?** Not which model is smartest. There is no score, no pass rate and no winner — spec J.2 —
because a number invites optimising the number, and what matters is that a human reads two responses
side by side and decides whether Apollo is still Apollo.

Everything below runs on the **eval surface**, which is the only surface that may resolve an
`eval_only` provider. Ordinary chat cannot reach one (spec K.2).

## The corpus

`evals/persona/cases/*.yaml`, thirty cases covering all twenty-five behavioural rules (B1–B25) and
the sixteen probes in spec J.1, plus inverse cases so the suite cannot quietly train the
overcorrection of each rule.

```sh
apollo eval corpus                 # validates every fixture and proves coverage
```

Coverage is mechanical: a case discharges a rule only if it declares it in `covers_rules`, and the
validator fails if any rule or probe has no case.

## A run

```sh
apollo eval run --brain brain.fake          # the offline brain: machinery only
apollo eval run --brain brain.local         # a real local model
apollo eval run --brain brain.reference     # the hosted reference model (eval-only)
```

Each run writes `evals/runs/<timestamp>-<brain>-<identity_hash[:8]>.json`, which is gitignored: it
contains model output verbatim. A run record carries everything needed to understand a behavioural
change months later — the fixture input, the visible response, every check result, the bundle hash,
the identity hash and version, the compiler version, the estimator, and the generation parameters.
Version 2 additionally records the complete semantic case definitions/check configuration, corpus
hash, context budget inputs, offline/provider provenance classification, and each generation's
rendered-prompt hash and finish reason. Acceptance reconstructs contexts with the existing compiler
and reruns the existing deterministic checks over the saved visible answers; it makes no model call.
Version-1 and partial runs remain diagnostic evidence, not acceptance-ready artifacts. Do not
backfill missing provenance or approval fields into old empirical records to make them pass.

It carries none of: hidden reasoning text, provider secrets, raw provider bodies, `Authorization`
headers, personal memory, or personal conversation history. That is asserted with sentinels in
`tests/integration/test_persona_runner.py`.

Manual cases run three times (`N=3`). They are recorded for a human to read and are never graded
automatically — there is no LLM judge anywhere in this system.

## Comparing runs

```sh
apollo eval diff evals/runs/<older>.json evals/runs/<newer>.json
apollo eval replaceability evals/runs/<fake>.json evals/runs/<local>.json evals/runs/<reference>.json
```

`diff` surfaces changed cases with both responses. Two mismatches are reported before anything else,
because they invalidate the comparison rather than participate in it: a different **identity hash**
(the two runs were different Apollos) and a different **bundle hash** for the same case (the models
were not asked the same thing). The suite pins `conservative-v1` for every brain so that the second
cannot happen by accident.

A brain with no run is reported `NOT RUN`, never `PASS`.

## The gates

```sh
apollo gate1 brain.local                    # protocol compatibility, including one generation
apollo eval gate2 evals/runs/<run>.json      # diagnostic only: no incumbent/review means nonzero
```

Gate 1 runs its static protocol and rendering checks first. Only when they pass, it creates a
benchmark conversation and makes one generation attempt through Apollo's recorded invocation path.
The configured runtime database must therefore be available. Provider failure, malformed generation
output or failure to record the probe makes the gate fail; there is no retry or fallback.

Gate 2 replacement acceptance requires all of the following (spec G.5, J, O.1; ADR-0012):

- Complete current repository corpus and sample structure for **both** runs: one sample per
  deterministic-only case, three per case with a manual check. Counts come from the case definitions,
  not a fixed case total. Missing/extra/duplicate cases, samples, checks or manual observations fail.
- Current identity version/hash, case definitions and check settings, compiler version and the common
  `conservative-v1` estimator. Every sample must have completed; no supplied overall PASS is trusted.
- An explicitly supplied, distinct incumbent, with a prior replacement-acceptance receipt bound to
  that incumbent artifact. Two arbitrary run files do not authorise a baseline. Reused invocation,
  turn or conversation evidence is rejected. Known fake/offline/replay evidence cannot qualify.
- Equal generation parameters and per-case compiled bundle hashes. Provider, model, adapter,
  renderer and capacity may differ; rendered-prompt hashes need not match across adapters. Capacity
  differences are permissible only when the compiled inputs still match.
- Every deterministic check passes, or its exact failure has a valid scoped human waiver.
- The internally computed diff is explicitly reviewed. Every changed case, including response-only
  changes, and every manual case (even unchanged) has a persisted case decision covering **all** its
  samples and manual rubrics. Failed deterministic cases also require a deviation decision.

Reports distinguish `candidate_valid`, `comparison_valid`, `incumbent_authorised`, `review_complete`,
mechanical eligibility, and final acceptance. Exit 0 means the complete replacement gate passed;
exit 1 means non-passing evidence; exit 2 means an input/output error. A completed run alone is not
acceptance. `diff` and `replaceability` remain inspection tools, not approval mechanisms.

### Review workflow (once an authorised incumbent exists)

Run from the configured Apollo checkout. Gate 2 itself uses no database or provider, although the
shared CLI configuration loader still requires its usual environment settings. Keep these artifacts
in private scratch storage or the ignored `evals/runs/` directory, not in source control.

```sh
# Writes a NEW template with all reviewer/decision fields null; returns nonzero while pending.
apollo eval gate2 candidate.json --incumbent incumbent.json \
  --incumbent-acceptance incumbent-acceptance.json --review-template review.json --json

# Optional readable inspection; the gate computes this diff itself as well.
apollo eval diff incumbent.json candidate.json

# After a HUMAN has read both runs, every required sample/rubric and the computed diff,
# and completed review.json (never have an assistant invent approvals):
apollo eval gate2 candidate.json --incumbent incumbent.json \
  --incumbent-acceptance incumbent-acceptance.json --review review.json \
  --write-acceptance candidate-acceptance.json --json
```

Templates and receipts are created exclusively: existing files are never overwritten. A receipt is
written only on PASS. Missing or invalid comparable run evidence cannot produce a review template.
Use new filenames when material changes; old review decisions do not carry forward automatically.

The version-1 review contains run IDs and canonical SHA-256 hashes of **both entire run documents**,
identity/corpus hashes, computed comparison hash, waiver-list hash and prior incumbent receipt hash.
Canonical JSON uses sorted keys, compact separators, Unicode and finite numbers: whitespace and
object-key ordering alone do not alter the evidence; changes to document values do. Duplicate JSON
keys are rejected. Requirements list change reasons, all sample indexes (one-based) and manual
rubrics/check indexes (zero-based). The human supplies `reviewer`, ISO `review_date`, `diff_reviewed:
true`, and one `accept`, `waive` or `reject` decision per required case, with an ISO `date`.
Pending/rejected, missing, duplicate, stale or out-of-scope decisions fail. Dates cannot be in the
future or after the overall review date.

`accept` says the behaviour meets the contract; `waive` acknowledges a deviation and additionally
requires an explicit written `reason` (at least 12 characters). For deterministic failures, also
explicitly supply `--waivers waivers.yaml` on **both** template and final commands. This extends the
existing `waivers:` list: each entry requires case/date/reason/brain/identity, exact
`model_identifier`, `candidate_hash`, `incumbent_hash`, `sample_indexes` and `check_indexes`.
Every scoped sample must have exactly those failing checks; extra, stale, overlapping or conflicting
waivers fail. Legacy broadly scoped waivers cannot authorise acceptance. A case decision alone
cannot waive a deterministic failure, and a deterministic waiver alone cannot complete human review.
No waiver or decision is generated automatically; the repository's waiver file is not auto-selected.

Hashes bind reviewed evidence, not authorship or truth. These are operator-trusted local artifacts,
not signatures or remote attestations. Declared provenance, invocation metadata and consistency checks
reject known replay/fake or reused evidence; they cannot prove that a deliberately forged file came
from a live provider or that its reviewer name was supplied by a human. Operators must preserve the
original run, invocation and review evidence and control who can write acceptance records.

### First incumbent: unresolved policy, no bypass

The frozen specification requires an incumbent comparison but defines no first-baseline procedure.
The replacement receipt format records an already accepted replacement and references its prior
incumbent receipt. This patch deliberately provides **no root receipt issuer** or first-run exception.
No incumbent means no acceptance; fake runs and Run A are not promoted. Real acceptance therefore
remains blocked until a separately approved first-baseline policy establishes an authorised starting
point. A minimal proposal, not approval: require a complete current real-provider run, every required
manual review, deterministic pass or explicitly scoped waivers, and a dated operator designation
bound to that evidence, explicitly acknowledging the absence of an incumbent comparison. Its policy
and representation need separate approval before implementation. Do not fabricate a replacement
receipt to stand in for that decision.

A run against `brain.fake` reports `infrastructure_only`, never `passed`: the offline brain proves
the harness, not Apollo.

## Binding a local model

Any OpenAI-compatible server works — llama.cpp's `server`, vLLM, SGLang, Ollama's compatible
endpoint, another machine on the network. Core knows none of them; the adapter speaks
`POST {base_url}/chat/completions`. Add to `apollo.toml`:

```toml
[brains]
local = "local"

[providers.local]
kind = "openai_compatible"
base_url = "http://127.0.0.1:8080/v1"   # the server's OpenAI-compatible base URL
model = "<the model id the server reports>"
context_budget = 8000                    # tokens of context Apollo may compile
reserved_output = 1024                   # tokens reserved for the reply
# max_context = 9024                     # optional; defaults to budget + reserved output
allowed_modes = ["personal", "benchmark"]
eval_only = false
# api_key_env = "APOLLO_LOCAL_API_KEY"   # only if the server requires a key
```

Then:

```sh
apollo gate1 brain.local                  # protocol plus one recorded generation
apollo eval run --brain brain.local       # then behaviour
apollo eval gate2 <local-run>.json         # non-passing without the evidence described above
```

`api_key_env` names the **environment variable**, never the key. A key in a committed file is a key
in git history for ever (spec K.3).

## Before a hosted run

A hosted provider is somebody else's server. Run the pre-flight first; it makes no request:

```sh
apollo eval preflight --brain brain.reference
```

It reports the provider alias, the base URL **host**, the model identifier, the mode, `eval_only`,
`allowed_modes`, the identity version and hash, and — derived from the actual compiled bundle —
whether canonical identity, fixture history, stored personal history, stored personal memory or
connector data are included. Expected: `mode = benchmark`, `eval_only = true`, fixture history only,
no stored personal history, no stored personal memory, no connector data.

Canonical identity **is** included, and is not removed to make the call feel less personal: a
benchmark of a stripped identity is a benchmark of a different Apollo.

## Recordings

`evals/recordings/` holds replay recordings for `brain.fake` in `replay` mode, filed under the
`bundle_hash` they answer and matched on the rendered prompt hash — so a `render_version` change
retires a recording instead of silently answering for bytes that were never sent. A recording holds
the visible generation only: text, finish reason, model identifier, counts. No reasoning text, no
secrets, no raw HTTP body.
