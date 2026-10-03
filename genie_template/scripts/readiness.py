"""Production readiness review of the project: is it fit to go live, and what is still the template's default?

    python scripts/readiness.py            print the review (exit 0)
    python scripts/readiness.py --strict   exit 1 if a MUST check fails (genie-ci runs this)

MUST checks block a release (the space would not work or could not be gated); SHOULD checks are what a
production service needs (owners, alerts, enough benchmarks...). The notebook (part J) and every pull request
(job summary) show the review.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import genie_tools as T                                        # noqa: E402

TEMPLATE_DEFAULTS = ("acme",)                                  # names that show a value was not edited yet


def checks() -> list[tuple[str, str, bool, str]]:
    """(level, check, passed, advice)."""
    out = []

    def add(level, name, ok, advice):
        out.append((level, name, bool(ok), advice))

    space = T.load_space()
    errors, _ = T.validate(space)
    prod, qa = T.settings("prod"), T.settings("qa")
    benchmarks = space.get("benchmarks", {}).get("questions", [])
    examples = space.get("instructions", {}).get("example_question_sqls", [])
    samples = space.get("config", {}).get("sample_questions", [])
    questions = {" ".join(b.get("question", [])).strip().lower() for b in benchmarks}
    asked = questions | {" ".join(e.get("question", [])).strip().lower() for e in examples}
    places = {(T.settings(e)["catalog"], T.settings(e)["schema"]) for e in T.ENVIRONMENTS}

    add("MUST", "the space definition is valid", not errors, "; ".join(errors[:3]) or "python scripts/validate_space.py")
    add("MUST", "enough benchmarks for the gate", len(benchmarks) >= int(qa["gate_min_graded"]),
        f"{len(benchmarks)} benchmark(s); gate_min_graded is {qa['gate_min_graded']}")
    add("MUST", "dev, qa and prod use different data", len(places) == 3, f"{sorted(places)}")
    add("MUST", "a smoke-test question is set", (prod.get("smoke_question") or "").strip(), "smoke_question")
    add("MUST", "benchmark questions are unique", len(questions) == len(benchmarks), "two benchmarks ask the same")

    def default(v):
        return any(d in str(v or "").lower() for d in TEMPLATE_DEFAULTS)
    add("SHOULD", "the project is named (not the template's)", not default(T.project_name()), "bundle.name")
    add("SHOULD", "owners and support are named",
        not any(default(prod.get(k)) for k in ("business_owner", "technical_owner", "support_contact")),
        "business_owner, technical_owner, support_contact")
    add("SHOULD", "groups are the project's", not any(default(prod.get(k)) for k in
                                                       ("users_group", "developers_group", "deployers_group")),
        "users_group, developers_group, deployers_group")
    add("SHOULD", "catalogs are the project's", not any(default(c) for c, _ in places), "catalog per environment")
    add("SHOULD", "prod failure e-mails go to someone", prod.get("alert_emails"), "targets.prod.variables.alert_emails")
    add("SHOULD", "prod SQL alerts notify someone", prod.get("alert_subscribers"),
        "targets.prod.variables.alert_subscribers")
    add("SHOULD", "prod alerts are running", prod.get("alerts_pause_status") == "UNPAUSED", "alerts_pause_status")
    add("SHOULD", "data freshness is checked", float(prod.get("max_data_age_hours") or 0) > 0,
        "max_data_age_hours: the load interval plus a margin, e.g. 26 for a daily load")
    add("SHOULD", "at least 10 benchmarks", len(benchmarks) >= 10, f"{len(benchmarks)} today: cover every kind of "
        "question users ask; failed and thumbs-down questions (usage dashboard) are the best source")
    add("SHOULD", "at least 3 sample questions", len(samples) >= 3, f"{len(samples)} today")
    add("SHOULD", "the space has text instructions", space.get("instructions", {}).get("text_instructions"),
        "business terms, time conventions, when to ask a clarifying question")
    add("SHOULD", "the smoke question has a trusted answer", (prod.get("smoke_question") or "").strip().lower() in asked,
        "make smoke_question one of the benchmarks or example SQL questions, so the smoke test is meaningful")
    add("SHOULD", "the gate is at least 60%", float(qa["gate_min_accuracy"]) >= 0.6, "gate_min_accuracy")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--strict", action="store_true")
    args = ap.parse_args(argv)
    results = checks()
    for level, name, ok, advice in results:
        print(f"{'pass' if ok else 'FAIL' if level == 'MUST' else 'todo'}  {level:<6} {name}" + ("" if ok else f"  -> {advice}"))
    must = [r for r in results if r[0] == "MUST" and not r[2]]
    todo = [r for r in results if r[0] == "SHOULD" and not r[2]]
    print(f"\n{'NOT READY' if must else 'ready' if not todo else 'ready, with open items'}: "
          f"{len(must)} MUST failed, {len(todo)} SHOULD open")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as fh:
            fh.write("### Production readiness\n\n| | Level | Check | To do |\n|---|---|---|---|\n" + "\n".join(
                f"| {'✅' if ok else '❌' if level == 'MUST' else '⚠️'} | {level} | {name} | {'' if ok else advice} |"
                for level, name, ok, advice in results) + "\n")
    return 1 if (must and args.strict) else 0


if __name__ == "__main__":
    sys.exit(main())
