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
apollo gate1 brain.local                    # protocol compatibility, mechanical
apollo eval gate2 evals/runs/<run>.json     # behavioural compatibility, empirical
```

Gate 2 passes only when a run exists at the current identity hash, bundle equality holds, and every
deterministic check passes **or** carries an explicit waiver. Waivers live in
`evals/persona/waivers.yaml` and must name the case, the date, the brain, the identity hash and a
written reason. Nothing generates a waiver reason; a waiver is a person saying they looked at the
change and accept it.

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
apollo gate1 brain.local                  # protocol first
apollo eval run --brain brain.local       # then behaviour
apollo eval diff <fake-run>.json <local-run>.json
apollo eval gate2 <local-run>.json
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
