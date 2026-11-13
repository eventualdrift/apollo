# ADR-0014 — The first accepted model is designated by a person, with no comparison

Status: accepted · 2026-11-13 (Janu)

## Context

ADR-0012 and spec §G.5 accept a model through Gate 2: the persona suite runs against the candidate,
and a person reviews its diff against the incumbent. As built, every acceptance receipt names the
receipt that authorised its incumbent. That makes a receipt impossible to forge by pointing at two
arbitrary runs, and it also means the chain has no first link.

The spec says how a model replaces another. It does not say how the first one is chosen. Since
September no model could be accepted at all, and the M2 runs were recorded as measurement, with
"no brain accepted".

## Decision

The first accepted model is a **first baseline**: one run, designated by a person, with nothing to
compare it to. It is issued only when all of this holds:

1. **A complete run on a real model.** The whole current corpus, at the current identity, compiler
   version and token estimator. Offline, fake and replayed runs never qualify.
2. **Every deterministic failure is fixed or waived.** A waiver is written by a person, dated,
   reasoned, and scoped to this run, this model, these samples and these checks. There is still no
   pass rate.
3. **Every manual case has been read.** The reviewer records a decision for each case that has a
   manual rubric, covering every sample and every rubric, and for each failing case.
4. **A dated designation, in the reviewer's own words.** They acknowledge explicitly that no
   incumbent comparison was made, and write a note naming this run the first baseline.

The receipt is its own kind, `gate2_first_baseline`. It binds the run, the identity, the corpus and
the waivers. A later replacement is accepted against it exactly as against any accepted incumbent,
and that replacement's receipt names it.

What does not change:

- No code decides anything. A review template has every decision and the designation blank.
- Replacement acceptance is as strict as before. Leaving the incumbent out of the ordinary gate is
  still a failure; designation is a separate, explicit command.
- A receipt of one kind cannot stand in for the other, and a waiver written for a comparison is
  not a waiver for a baseline.

## Consequences

- Gate 2 can pass for the first time.
- The first model's acceptance says less than a later one's. It says a person read this run in
  full and chose it as the starting point. It does not say the model matches anything.
- **A baseline is bound to one identity, one corpus and one compiler version**, as every receipt
  already is. When any of them changes, the accepted run can no longer be compared with a current
  one. The same model then has to be run again and designated again, by this same procedure. That
  is a consequence of how strictly Gate 2 binds its evidence, and it is the cost of an identity
  edit (§J.5).
- **Nothing stops a second designation.** Receipts are local files the operator is trusted with,
  and Gate 2 keeps no state. What guards the comparison is that designating is a deliberate act
  with a written, dated acknowledgement, not a default. It is no easier to misuse than a forged
  replacement receipt already was.

## Reversal cost

Low. The receipt kind and one command. Removing them returns Gate 2 to accepting nothing.
