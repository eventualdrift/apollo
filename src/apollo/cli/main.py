"""A thin terminal client. Captures input and renders output; owns nothing."""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from datetime import UTC, datetime

from apollo.brains.registry import BrainRegistry
from apollo.config import load_config
from apollo.core.conversations import create_conversation, get_conversation, transcript
from apollo.core.identity import IdentityLoader
from apollo.core.turns import TurnService
from apollo.errors import ApolloError, ConfigError
from apollo.logging_setup import configure_logging
from apollo.storage.db import (
    Database,
    RoleProvisioningError,
    describe_role_privileges,
    provision_runtime_role,
)

DEFAULT_CASES_DIR = "evals/persona/cases"
DEFAULT_RUNS_DIR = "evals/runs"


def _service(config) -> tuple[Database, TurnService]:  # type: ignore[no-untyped-def]
    db = Database(config.database_dsn)
    service = TurnService(
        db,
        config,
        BrainRegistry(config),
        IdentityLoader(config.identity_dir),
        clock=lambda: datetime.now(UTC),
    )
    return db, service


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="apollo")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("migrate", help="apply pending migrations (needs APOLLO_ADMIN_DSN)")
    p_prov = sub.add_parser(
        "provision",
        help="migrate, then create and grant the least-privilege runtime role "
             "(needs APOLLO_ADMIN_DSN)",
    )
    p_prov.add_argument("--password", default=None,
                        help="runtime role password; omit for trust/peer auth")
    sub.add_parser("doctor", help="report the runtime role's actual privileges")
    p_gate = sub.add_parser("gate1", help="run Gate 1 protocol checks against a brain alias")
    p_gate.add_argument("alias", nargs="?", default="brain.default")
    p_gate.add_argument("--eval-surface", action="store_true",
                        help="resolve on the eval surface, for an eval-only provider")

    p_eval = sub.add_parser("eval", help="persona regression suite (spec J)")
    eval_sub = p_eval.add_subparsers(dest="eval_command", required=True)

    p_corpus = eval_sub.add_parser("corpus", help="validate the case corpus and its coverage")
    p_corpus.add_argument("--cases", default=str(DEFAULT_CASES_DIR))

    p_run = eval_sub.add_parser("run", help="run the persona suite against a brain alias")
    p_run.add_argument("--brain", default="brain.fake")
    p_run.add_argument("--cases", default=str(DEFAULT_CASES_DIR))
    p_run.add_argument("--runs-dir", default=str(DEFAULT_RUNS_DIR))
    p_run.add_argument("--case", action="append", default=None,
                       help="run only these case ids (repeatable)")

    p_diff = eval_sub.add_parser("diff", help="compare two run records")
    p_diff.add_argument("run_a")
    p_diff.add_argument("run_b")
    p_diff.add_argument("--json", action="store_true")

    p_gate2 = eval_sub.add_parser("gate2", help="evaluate Gate 2 for a run record")
    p_gate2.add_argument("run")
    p_gate2.add_argument("--incumbent", help="explicit distinct incumbent run")
    p_gate2.add_argument("--incumbent-acceptance", help="prior incumbent acceptance receipt")
    p_gate2.add_argument("--review", help="persisted human review JSON")
    p_gate2.add_argument("--review-template", help="write a new pending template; never overwrite")
    p_gate2.add_argument("--write-acceptance", help="write a new receipt only if the gate passes")
    p_gate2.add_argument("--waivers", default=None)
    p_gate2.add_argument("--json", action="store_true")

    p_pre = eval_sub.add_parser(
        "preflight",
        help="report what a hosted run would send, before it sends it; makes no call",
    )
    p_pre.add_argument("--brain", default="brain.reference")
    p_pre.add_argument("--cases", default=str(DEFAULT_CASES_DIR))
    p_pre.add_argument("--case", default=None, help="which case to compile; default the first")
    p_pre.add_argument("--json", action="store_true")

    p_repl = eval_sub.add_parser(
        "replaceability", help="per-case comparison across brains; no winner"
    )
    p_repl.add_argument("runs", nargs="+")
    p_repl.add_argument("--json", action="store_true")

    p_abl = eval_sub.add_parser(
        "notice-ablation",
        help="retrieval-notice-ablation-1: eval-only, five cases, at most 10 generations",
    )
    abl_sub = p_abl.add_subparsers(dest="ablation_command", required=True)
    p_abl_diff = abl_sub.add_parser("diff", help="offline one-variable proof (no DB, no model)")
    p_abl_diff.add_argument("--out", default=None, help="write the proof here (never overwrite)")
    p_abl_plan = abl_sub.add_parser("plan", help="seal the local plan (no DB, no model)")
    p_abl_plan.add_argument("--gpt-oss-run", required=True)
    p_abl_plan.add_argument("--qwen-run", required=True)
    p_abl_plan.add_argument("--out", required=True, help="a new experiment directory")
    for name, helptext in (("preflight", "every check except a generation"),
                           ("run", "preflight, reserve, then at most 5 generations")):
        p_abl_x = abl_sub.add_parser(name, help=helptext)
        p_abl_x.add_argument("--plan", required=True, help="the sealed experiment directory")
        p_abl_x.add_argument("--model", required=True, choices=["gpt-oss", "qwen"])
    for p_abl_any in (p_abl_diff, p_abl_plan):
        p_abl_any.add_argument("--cases", default=str(DEFAULT_CASES_DIR))
        p_abl_any.add_argument("--identity-dir", default="identity")

    p_new = sub.add_parser("new", help="start a conversation")
    p_new.add_argument("--title", default=None)

    p_say = sub.add_parser("say", help="send one message and print the reply")
    p_say.add_argument("conversation")
    p_say.add_argument("text")
    p_say.add_argument("--idempotency-key", default=None)

    p_chat = sub.add_parser("chat", help="interactive loop")
    p_chat.add_argument("conversation")

    p_show = sub.add_parser("show", help="print a conversation transcript")
    p_show.add_argument("conversation")

    args = parser.parse_args(argv)
    if (
        args.command == "eval"
        and args.eval_command == "notice-ablation"
        and args.ablation_command in {"diff", "plan"}
    ):
        # Offline by construction: no configuration, database or provider is loaded.
        return _run_notice_ablation_offline(args)
    config = load_config()
    configure_logging(config.log_level)

    if args.command in {"migrate", "provision"}:
        if not config.admin_dsn:
            print(
                "APOLLO_ADMIN_DSN is not set. Migrations and provisioning run as the"
                " database owner; the runtime DSN is the unprivileged application role.",
                file=sys.stderr,
            )
            return 2
        admin = Database(config.admin_dsn)
        applied = admin.migrate()
        print("\n".join(applied) if applied else "schema already up to date")
        if args.command == "provision":
            password = args.password or os.environ.get("APOLLO_RUNTIME_PASSWORD")
            try:
                with admin.connect() as conn:
                    created = provision_runtime_role(conn, config.runtime_role, password=password)
                    facts = describe_role_privileges(conn, config.runtime_role)
            except RoleProvisioningError as exc:
                print(str(exc), file=sys.stderr)
                return 2
            print(f"runtime role {config.runtime_role}: {'created' if created else 'updated'}")
            print(f"  superuser:              {facts.is_superuser}")
            print(f"  owns audit_event:       {facts.owns_audit_event}")
            print(f"  audit_event privileges: {', '.join(facts.audit_event_privileges)}")
            if not facts.is_least_privilege:
                print("  WARNING: runtime role is not least-privilege", file=sys.stderr)
                return 1
        return 0

    if args.command == "doctor":
        target = Database(config.admin_dsn or config.database_dsn)
        with target.connect() as conn:
            facts = describe_role_privileges(conn, config.runtime_role)
        for line in facts.as_lines():
            print(line)
        print("least privilege:", "OK" if facts.is_least_privilege else "VIOLATED")
        return 0 if facts.is_least_privilege else 1

    if args.command == "gate1":
        return _run_gate1(config, args.alias, eval_surface=args.eval_surface)

    if args.command == "eval":
        return _run_eval(config, args)

    db, service = _service(config)

    if args.command == "new":
        conversation_id = create_conversation(db, now=datetime.now(UTC), title=args.title)
        print(conversation_id)
        return 0

    conversation_id = uuid.UUID(args.conversation)

    if args.command == "show":
        if get_conversation(db, conversation_id) is None:
            print(f"no such conversation: {conversation_id}", file=sys.stderr)
            return 1
        for row in transcript(db, conversation_id):
            marker = {"user": "janu", "apollo": "apollo", "system_note": "----"}[row["role"]]
            print(f"[{row['seq']:>3}] {marker}: {row['content']}")
        return 0

    service.recover_orphans()

    if args.command == "say":
        return _emit(service.submit(
            conversation_id=conversation_id,
            text=args.text,
            idempotency_key=args.idempotency_key,
        ))

    if args.command == "chat":
        print("apollo — ctrl-d to leave")
        while True:
            try:
                text = input("janu> ").strip()
            except EOFError:
                print()
                return 0
            if not text:
                continue
            _emit(service.submit(conversation_id=conversation_id, text=text))
    return 0


def _run_eval(config, args) -> int:  # type: ignore[no-untyped-def]
    """The eval surface. Every path here runs on `SURFACE_EVAL` deliberately.

    Nothing in this function can be reached from `apollo say` or `apollo chat`:
    the eval-only providers it may resolve stay unreachable from the
    interactive surface, which is the separation spec K.2 requires.
    """
    import json
    import pathlib
    from dataclasses import replace

    from apollo.config import SURFACE_EVAL
    from apollo.core.identity import IdentityLoader
    from apollo.evals import corpus as corpus_module
    from apollo.evals import gate2 as gate2_module
    from apollo.evals.diff import diff_runs, load_run_document
    from apollo.evals.evidence import DEFAULT_CASES_DIR as ACCEPTANCE_CASES_DIR
    from apollo.evals.evidence import load_document
    from apollo.evals.loader import load_cases
    from apollo.evals.preflight import preflight
    from apollo.evals.replaceability import BRAIN_ORDER, build_report
    from apollo.evals.runner import PersonaRunner, compile_case_bundle

    if args.eval_command == "corpus":
        cases = load_cases(pathlib.Path(args.cases))
        report = corpus_module.coverage(cases)
        print("\n".join(report.as_lines()))
        return 0 if report.complete else 1

    if args.eval_command == "run":
        cases = load_cases(pathlib.Path(args.cases))
        if args.case:
            wanted = set(args.case)
            cases = [case for case in cases if case.id in wanted]
            if not cases:
                print(f"no cases matched {sorted(wanted)}", file=sys.stderr)
                return 2
        db = Database(config.database_dsn)
        runner = PersonaRunner(
            db,
            config,
            BrainRegistry(config, surface=SURFACE_EVAL),
            IdentityLoader(config.identity_dir),
            clock=lambda: datetime.now(UTC),
            runs_dir=pathlib.Path(args.runs_dir),
        )
        record = runner.run(cases, brain_alias=args.brain)
        path = runner.write(record)
        completed = sum(
            1 for case in record.cases for s in case["samples"] if s["status"] == "completed"
        )
        samples = sum(len(case["samples"]) for case in record.cases)
        failures = [c["case_id"] for c in record.cases if c["deterministic_status"] == "fail"]
        print(f"EVAL RUN COMPLETED: {completed}/{samples} samples, {len(record.cases)} cases")
        print(f"run record: {path}")
        print(f"deterministic failures: {len(failures)} {failures}")
        print("A completed run is not a passed gate. Run `apollo eval gate2` for that.")
        return 0

    if args.eval_command == "diff":
        result = diff_runs(
            load_run_document(pathlib.Path(args.run_a)),
            load_run_document(pathlib.Path(args.run_b)),
        )
        if args.json:
            print(json.dumps(result.as_dict(), indent=2))
        else:
            print("\n".join(result.as_lines()))
        return 0

    if args.eval_command == "gate2":
        try:
            run = load_document(pathlib.Path(args.run))
            incumbent = load_document(pathlib.Path(args.incumbent)) if args.incumbent else None
            prior = (
                load_document(pathlib.Path(args.incumbent_acceptance))
                if args.incumbent_acceptance
                else None
            )
            review = load_document(pathlib.Path(args.review)) if args.review else None
            waivers: tuple[gate2_module.Waiver, ...] = ()
            if args.waivers:
                waiver_path = pathlib.Path(args.waivers)
                if not waiver_path.is_file():
                    raise FileNotFoundError
                waivers = gate2_module.load_waivers(waiver_path)
            identity = IdentityLoader(config.identity_dir).load()
            cases = load_cases(ACCEPTANCE_CASES_DIR)
            if not corpus_module.coverage(cases).complete:
                print("Gate 2: repository corpus coverage is incomplete", file=sys.stderr)
                return 1
            gate_report = gate2_module.evaluate_gate2(
                run,
                identity_hash=identity.content_hash,
                identity=identity,
                cases=cases,
                waivers=waivers,
                incumbent=incumbent,
                incumbent_acceptance=prior,
                review=review,
            )
            if args.review_template and gate_report.review_template is not None:
                with pathlib.Path(args.review_template).open("x", encoding="utf-8") as out:
                    out.write(json.dumps(gate_report.review_template, indent=2) + "\n")
            if args.write_acceptance and gate_report.passed:
                with pathlib.Path(args.write_acceptance).open("x", encoding="utf-8") as out:
                    out.write(json.dumps(gate_report.acceptance_record, indent=2) + "\n")
        except (ApolloError, OSError, ValueError) as exc:
            # Evidence errors must not echo file contents, secrets or provider bodies.
            print(f"Gate 2 evidence error: {type(exc).__name__}", file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps(gate_report.as_dict(), indent=2))
        else:
            print("\n".join(gate_report.as_lines()))
        return 0 if gate_report.passed else 1

    if args.eval_command == "preflight":
        cases = load_cases(pathlib.Path(args.cases))
        case = next((c for c in cases if c.id == args.case), None) if args.case else cases[0]
        if case is None:
            print(f"no such case: {args.case}", file=sys.stderr)
            return 2
        try:
            provider = config.provider_for_alias(args.brain)
        except ConfigError as exc:
            print(f"{exc}. Nothing to pre-flight.", file=sys.stderr)
            return 2
        identity = IdentityLoader(config.identity_dir).load()
        registry = BrainRegistry(config, surface=SURFACE_EVAL)
        extra: list[str] = []
        try:
            max_context = registry.get(args.brain).capabilities().max_context
        except ApolloError as exc:
            # A pre-flight that cannot build the adapter is still informative:
            # it names what is missing before anything is sent.
            max_context = provider.context_budget + provider.reserved_output
            extra.append(f"adapter could not be constructed: {exc}")
        bundle = compile_case_bundle(
            case,
            identity=identity,
            provider=provider,
            max_context=max_context,
            identity_cap=config.identity_token_cap,
            now=datetime.now(UTC),
        )
        pre_report = preflight(brain_alias=args.brain, provider=provider, bundle=bundle)
        if extra:
            pre_report = replace(pre_report, unexpected=pre_report.unexpected + tuple(extra))
        if args.json:
            print(json.dumps(pre_report.as_dict(), indent=2))
        else:
            print("\n".join(pre_report.as_lines()))
            print(f"(compiled from case {case.id})")
        return 0 if pre_report.expectations_hold else 1

    if args.eval_command == "replaceability":
        runs = {}
        for raw in args.runs:
            document = load_run_document(pathlib.Path(raw))
            runs[str(document.get("brain_alias"))] = document
        # Say *why* a brain did not run. "NOT RUN" with no reason invites the
        # reader to assume it was skipped for a good one.
        unavailable = {}
        for alias in BRAIN_ORDER:
            if alias in runs:
                continue
            short = alias.removeprefix("brain.")
            if short not in config.brain_aliases:
                unavailable[alias] = "no such brain alias configured"
                continue
            try:
                config.provider_for_alias(alias)
            except ConfigError:
                unavailable[alias] = "alias declared, provider not configured"
            else:
                unavailable[alias] = "configured, but no run record was supplied"
        repl_report = build_report(runs, unavailable=unavailable)
        if args.json:
            print(json.dumps(repl_report.as_dict(), indent=2))
        else:
            print("\n".join(repl_report.as_lines()))
        return 0

    if args.eval_command == "notice-ablation":
        return _run_notice_ablation_live(config, args)

    print(f"unknown eval command {args.eval_command!r}", file=sys.stderr)
    return 2


def _run_notice_ablation_offline(args) -> int:  # type: ignore[no-untyped-def]
    """`diff` and `plan`: compile and bind evidence; never a database or provider."""
    import json
    import pathlib

    from apollo.core.identity import compose_identity
    from apollo.evals import notice_ablation as ablation
    from apollo.evals.loader import load_cases

    cases = load_cases(pathlib.Path(args.cases))
    identity = compose_identity(pathlib.Path(args.identity_dir))
    try:
        if args.ablation_command == "diff":
            document = ablation.structural_diff_document(cases, identity)
            text = json.dumps(document, indent=2, ensure_ascii=False) + "\n"
            if args.out:
                with pathlib.Path(args.out).open("x", encoding="utf-8") as handle:
                    handle.write(text)
            else:
                print(text, end="")
            print("ONE-VARIABLE PROOF PASS: only the retrieval notice differs", file=sys.stderr)
            return 0

        out = pathlib.Path(args.out)
        if out.exists():
            raise FileExistsError(f"{out} already exists; a sealed plan is never overwritten")
        plan = ablation.build_plan(
            cases=cases,
            identity=identity,
            historical={"gpt-oss": pathlib.Path(args.gpt_oss_run),
                        "qwen": pathlib.Path(args.qwen_run)},
        )
        sealed = ablation.seal(plan)
        out.mkdir(parents=True, exist_ok=False)  # only once the plan has validated
        (out / "plan.json").write_text(json.dumps(plan, indent=2, ensure_ascii=False) + "\n",
                                       encoding="utf-8")
        (out / "plan.json.sha256").write_text(sealed + "\n", encoding="utf-8")
    except (ApolloError, OSError, ValueError) as exc:
        print(f"notice-ablation {args.ablation_command}: STOP — {exc}", file=sys.stderr)
        return 1
    print(f"PLAN SEALED: {out / 'plan.json'} sha256={sealed}")
    print(f"ceiling: {ablation.MAX_NEW_GENERATIONS} generations "
          f"({ablation.MAX_GENERATIONS_PER_MODEL} per model); no generation has been made")
    return 0


def _run_notice_ablation_live(config, args) -> int:  # type: ignore[no-untyped-def]
    """`preflight` and `run`, on the eval surface only."""
    import pathlib

    from apollo.config import SURFACE_EVAL
    from apollo.evals import notice_ablation as ablation
    from apollo.evals.evidence import DEFAULT_CASES_DIR as FROZEN_CASES_DIR
    from apollo.evals.evidence import load_document
    from apollo.evals.loader import load_cases

    plan_dir = pathlib.Path(args.plan)
    registry = BrainRegistry(config, surface=SURFACE_EVAL)
    loader = IdentityLoader(config.identity_dir)
    try:
        plan = load_document(plan_dir / "plan.json")
        ablation.verify_plan(plan, (plan_dir / "plan.json.sha256").read_text().strip())
        cases = load_cases(FROZEN_CASES_DIR)
        if args.ablation_command == "preflight":
            failures = ablation.preflight(
                config=config, registry=registry, identity=loader.load(), plan=plan,
                model_key=args.model, out_dir=plan_dir, cases=cases,
            )
            for failure in failures:
                print(f"  FAIL  {failure}")
            print("PREFLIGHT PASS" if not failures else "PREFLIGHT FAIL")
            return 0 if not failures else 1
        document = ablation.run_candidate(
            db=Database(config.database_dsn), config=config, registry=registry,
            identity_loader=loader, plan=plan, model_key=args.model, cases=cases,
            out_dir=plan_dir, clock=lambda: datetime.now(UTC),
        )
    except (ApolloError, OSError, ValueError) as exc:
        # Scalars only: no DSN, provider body or response text.
        print(f"notice-ablation {args.ablation_command}: STOP — {type(exc).__name__}: "
              f"{exc if isinstance(exc, ablation.AblationError) else ''}", file=sys.stderr)
        return 1
    reconciliation = document["reconciliation"]
    completed = sum(1 for p in document["pairs"] if p["candidate_notice"]["status"] == "completed")
    print(f"ABLATION RUN RECORDED ({args.model}): {document['provider_generations']} provider "
          f"generations, {completed} completed; reconciliation consistent="
          f"{reconciliation['consistent']}")
    print(f"record: {plan_dir / (args.model + '-candidate-run.json')}")
    print("Semantic review is pending. Nothing here is accepted behaviour.")
    return 0


def _run_gate1(config, alias: str, *, eval_surface: bool) -> int:  # type: ignore[no-untyped-def]
    """Run static checks, then one recorded synthetic generation probe."""
    from apollo.brains.registry import BrainRegistry
    from apollo.config import SURFACE_EVAL, SURFACE_INTERACTIVE
    from apollo.core.identity import IdentityLoader
    from apollo.evals.gate1 import evaluate_gate1

    surface = SURFACE_EVAL if eval_surface else SURFACE_INTERACTIVE
    try:
        report = evaluate_gate1(
            Database(config.database_dsn),
            config,
            BrainRegistry(config, surface=surface),
            IdentityLoader(config.identity_dir),
            brain_alias=alias,
            clock=lambda: datetime.now(UTC),
        )
    except Exception as exc:  # no exception messages: they may contain DSNs or provider data
        print(f"Gate 1: FAIL — {type(exc).__name__}", file=sys.stderr)
        return 1
    print("\n".join(report.as_lines()))
    return 0 if report.passed else 1


def _emit(result) -> int:  # type: ignore[no-untyped-def]
    if result.status == "completed":
        print(f"apollo> {result.response}")
        return 0
    print(f"apollo> [turn failed: {result.error_kind}]", file=sys.stderr)
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
