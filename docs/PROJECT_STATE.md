# Apollo project state

This is the repository's concise continuity entry. It records verified working state and links to
the governing documents; it does not replace the frozen specification or accepted ADRs.

## Authority

- Normative architecture: [`architecture/phase-zero-spec.md`](architecture/phase-zero-spec.md)
- Behaviour contract: [`architecture/behaviour-contract.md`](architecture/behaviour-contract.md)
- Accepted decisions: [`adr/`](adr/)
- Implementation sequence: [`architecture/implementation-plan.md`](architecture/implementation-plan.md)

## Current checkpoint: Gate 2 contract correction (2026-09-22)

This separately authorised task started at `8b60216c5ae6740d8e93913512a8925f97ca8de1` on
`claude/apollo-m2-real-models`, clean, in lowercase `/home/jvm/apollo`. The existing Gate 1 recovery
bundle was verified unchanged (SHA-256
`48f142a6cc6f3b3e98c33f6ddc0c721b4fd7fecac33b6775062e06ff3c746779`). No reset or recovery replay
was needed. Earlier Gate 1 evidence below is historical, not a claim that Gate 2 remains unfixed.

Five tests against that checkpoint reproduced false PASS for a single run, partial corpus,
outstanding manual review, response-only changes without review, and omitted checks. The pre-fix
log is preserved with the task verification evidence. The corrected gate now requires:

- Version-2 complete run evidence for candidate and explicit, distinct incumbent: current identity,
  semantic case definitions/check configuration and corpus hash, compiler, common estimator, all
  required samples/checks/manual observations, complete generation metadata, reconstructed context
  hashes and recomputed deterministic checks. The runner records the additional metadata through
  its unchanged shared invocation path; model parameters and sampling policy are unchanged.
- Compatible generation settings and compiled inputs; provider/model/adapter/rendering may differ.
  The gate computes the diff itself and rejects reused invocation/turn/conversation evidence.
- An explicit prior acceptance receipt bound to the incumbent; two arbitrary files cannot establish
  an authorised baseline. No root receipt issuer, fake incumbent or first-run bypass was added.
- Versioned persisted human review bound to both complete run documents, comparison, identity,
  corpus, waivers and prior receipt. Every changed case (including response-only changes) and every
  manual case requires a dated decision covering all samples/rubrics. Templates leave decisions
  unset. Deterministic waivers require exact artifact/model/sample/check scope; deviations require
  a reason and date and remain recorded, not erased.

See [`operations/persona-eval.md`](operations/persona-eval.md) for the CLI workflow, schema/index
conventions, exit codes and trust limitations. Version-1 evidence remains diagnostic only; it was
not rewritten or retroactively qualified. Hashes bind canonical JSON values, not human authorship or
provider authenticity; these are operator-trusted artifacts, not signed attestations.

### Gate 2 task verification

All tests imported this checkout using explicit `PYTHONPATH=/home/jvm/apollo/src`, disabled bytecode
and pytest caching, and used controlled synthetic adapters/evidence only. PostgreSQL was rechecked:
the same `a00d25f0ff76…` disposable container, version 16.15, loopback `127.0.0.1:32768`, tmpfs data,
no host mounts. Relevant executions explicitly set `APOLLO_TEST_ADMIN_DSN` to that scratch instance.

| Check | Result | Exit |
| --- | --- | --- |
| Pre-fix false-acceptance reproduction | 5 expected regression failures | 1 |
| Gate 2 unit/diff/record + PostgreSQL persona runner | 185 passed, 0 failed/skipped | 0 |
| Gate 1 unit + PostgreSQL probe tests separately | 19 passed, 0 failed/skipped | 0 |
| Gate 1 + shared invocation/reply/policy/privacy/failure/lifecycle regressions | 90 passed, 0 failed/skipped | 0 |
| Architecture | 56 passed, 0 failed/skipped | 0 |
| Full suite with disposable PostgreSQL | 627 passed, 0 failed/skipped | 0 |
| Ruff lint, changed-scope formatting, diff checks | passed | 0 |
| Mypy, current tree and clean `8b60216` snapshot with same toolchain | same two storage errors | 1 each |

Mypy checked 53 source files versus 51 at the checkpoint; its unchanged diagnostics are
`storage/db.py:67` and `:231`. Initial development checks exposed test-fixture budget/config mistakes,
formatting and three new typing diagnostics; these were corrected before final verification.
No skips or known typing errors are presented as passes. Existing whole-file formatting drift in
unchanged code was not repaired. Verification logs originate at
`/tmp/apollo-gate2-verification-PQv5jS` and are copied into the new private recovery directory.

### Remaining boundary and acceptance state

The frozen specification and ADR-0012 require an incumbent comparison but define no first-baseline
procedure. **Proposal only, for separate approval:** a complete current real-provider run, all manual
review, deterministic pass or scoped human waivers, and a dated operator designation bound to the
exact evidence, explicitly acknowledging the absence of an incumbent comparison. Agree its policy
and representation before implementing a root-authorisation mechanism. This task does not approve
or implement that exception; do not fabricate a replacement receipt to bootstrap it.

Gate 1 implementation remains verified by PostgreSQL-backed tests; live-provider Gate 1 was not
run. Gate 2 implementation is verified with synthetic evidence, not real model behaviour. No real
incumbent or review was established. Run A remains unchanged empirical evidence with incomplete
samples/deterministic failures, not acceptance. M2 remains **unaccepted**. No identity, persona case,
real waiver, model configuration, frozen spec/ADR or external Run A changes; no model server,
persona model experiment, M3 work, push or publication. No environment blocker remains for these
checks; first-incumbent policy and real reviewed evidence remain acceptance blockers.

## Historical Gate 1 checkpoint

Bounded Gate 1 verification completed 2026-09-22 in `/home/jvm/apollo`:

- branch: `claude/apollo-m2-real-models`
- verification base: `295d77cbdf975f8387cab470412566bd0c39f495`
- starting tree for this verification: six modified tracked files and four untracked Gate 1 /
  continuity files; all matched the existing verified patch backup and were preserved
- `origin` points to a complete local Git bundle with SHA-256
  `ca93cfcec1356bf1c9cfb710b83b087e0de0eeae6c6e96714f02b55ab586183b`; the bundle contains this HEAD;
  ignored evaluation artifacts and PostgreSQL data are separate evidence and are not covered by that
  source-control backup
- external Run A remains at `/home/jvm/apollo-scratch/runA.json`, SHA-256
  `79e4400e43482b8189ac9b1a6c592742c25fa4980de959c66823c76a2c032470`; its runtime snapshot has
  SHA-256 `88a47fcafa40173745cb9e0533393a42d5b13c15f122147c1d479a2b8a5e5927`; neither was changed

At the Gate 1 checkpoint, M1 foundations and M2 implementation were present and M2 acceptance was
**not established**. External
Run A reconciles to the current identity and corpus, but contains incomplete and deterministic
failures. Gate 2 did not yet enforce the frozen incumbent-diff and human-review contract.

## Gate 1 implementation proof

The pre-change harness could report full Gate 1 PASS after static checks with zero generation calls.
That defect was reproduced again directly from the committed baseline. The correction makes
static-only reports non-passing and adds one synthetic
benchmark generation through `open_invocation()` and `invoke()`. It validates the returned
`Generation`, records success or failure, uses no retry or fallback and leaves adapter import
boundaries intact.

The original 14 targeted cases passed. Five additional PostgreSQL cases reproduced three defects
before correction (three failures, twelve passes):

- a valid static render could be followed by an invalid actual probe render and still pass;
  `invoke()` now validates the actual request before generation
- probe failure omitted the system note; failure status, fixed safe note and audit now share one
  transaction
- response-storage failure could leave the synthetic turn started; the guarded lifecycle now
  includes turn finalisation and attempts safe failure finalisation if it fails

The additional cases also cover metadata/exception privacy sentinels. The success test observes
the committed started invocation from an independent connection and checks both transaction depth
and closed production connections at adapter entry. A response-storage failure retains the truthful
completed invocation while failing the turn, with no partial response. An unavailable database
cannot produce PASS; finalisation remains best-effort if database access itself is lost.

The earlier policy failure was a brittle whole-CLI docstring text scan, not a runtime policy defect.
Runtime mode/surface refusal and zero-call policy tests pass independently; text-scan success is not
treated as policy proof.

## Verification results

All pytest commands used `-p no:cacheprovider`; no real model was invoked.

| Check | Result | Exit |
| --- | --- | --- |
| Gate 1 unit + PostgreSQL tests, after corrections | 19 passed, 0 failed/skipped | 0 |
| Existing shared invocation/reply/evaluation/policy/privacy/lifecycle subset | 91 passed, 0 failed/skipped | 0 |
| Unit suite | 306 passed, 0 failed/skipped | 0 |
| Architecture suite | 54 passed, 0 failed/skipped | 0 |
| Full suite, disposable PostgreSQL configured | 502 passed, 0 failed/skipped | 0 |
| `ruff check --no-cache src tests` | passed | 0 |
| Ruff format check: six changed Python files + changed CLI function | passed | 0 |
| `git diff --check` | passed | 0 |
| Mypy: current tree and clean baseline snapshot, same toolchain | same two existing diagnostics | 1 each |

The unchanged mypy diagnostics are `storage/db.py:67` (connection yield type) and `:231` (optional
dictionary indexing). Current analysis covered 51 source files; baseline covered 50. No new typing
diagnostic was introduced. Whole-file CLI formatting still fails on unchanged sections that also
fail at the baseline; broad formatting drift was not repaired. The changed function passes on its
own (excluding the separator blank lines belonging between functions).

## Environment and recovery

- Python: `/home/jvm/Claude stuff'/Claude Code/.venv/bin/python`, version 3.14.7; pytest 9.1.1,
  Ruff 0.16.8, mypy 2.3.1. Apollo imports were verified from this lowercase checkout.
- Disposable PostgreSQL: `apollo-gate1-test-20260922`, container ID prefix `a00d25f0ff76`, PostgreSQL
  16.15; loopback-only endpoint `127.0.0.1:32768`, tmpfs database storage, no host mounts.
- Each relevant execution explicitly supplied `PYTHONPATH=/home/jvm/apollo/src` and
  `APOLLO_TEST_ADMIN_DSN='host=127.0.0.1 port=32768 user=postgres dbname=postgres'`. Fixtures create
  and remove unique test databases/roles only on that disposable instance, never the personal DB.
- The task still defaults to uppercase `/home/jvm/Apollo`; commands explicitly target lowercase.
  Sandbox restrictions are distinct from OS permissions. Normal approved host execution provides
  PostgreSQL and Git access; no ownership or socket permissions were changed.
- Original verified patch, unchanged: `/home/jvm/apollo-scratch/gate1-patch-20260922-TVsAXV`.
- Verified pre-commit snapshot: `/home/jvm/apollo-scratch/gate1-verified-20260922-8g3t73`.
- Pre-commit recovery snapshot, including the staged files, verification logs and original commit
  blocker (subsequently resolved):
  `/home/jvm/apollo-scratch/gate1-verified-20260922-BJy1Mu`.
- Working verification logs: `/tmp/apollo-gate1-verification-8E1Gjf`.

## Remaining limits and stopping point

Gate 1 implementation verification passed with controlled adapters and real PostgreSQL.
Live-provider Gate 1 evidence was **not run** and is not claimed. The two baseline typing errors and
old formatting drift remain. No environment blocker remains for the bounded checks above.

The initial commit attempt exited 128 with `Author identity unknown`. That attribution blocker is
resolved: the user authorised repository-local Git settings for `eventualdrift`
(`134446295+eventualdrift@users.noreply.github.com`); global settings were not changed. Before the
local Gate 1 commit, every staged application and test file was verified byte-for-byte against the
tested recovery snapshot. Only this continuity note changed, so the full suite was not rerun solely
for Git attribution. The verification results and remaining limitations above still apply.

Identity, persona cases, waivers, model configuration, Gate 2 code and Run A were not changed. No
model server was installed or started, no M3 work was done, and nothing was pushed or published.

At that historical stopping point, M2 remained **unaccepted**. Run A's incomplete/deterministic failures and the unchanged Gate 2
full-corpus, incumbent comparison and explicit human-review evidence contract remain to be addressed
under a separately authorised task, including a deliberate first-incumbent decision. This checkpoint
did not create an approval or waiver or authorise starting Gate 2. The separately authorised
correction and its current stopping point are recorded above.
