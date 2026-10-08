"""Rule drafting: turn a plain-English user story into a candidate YAML rule.

The draft is written to rules/proposed/, which the catalog loader never reads. It runs
only after a human reviews it and moves it into rules/.

    python -m framework.ai.rule_drafter "A provider must not see more than 8 patients a day"
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

from framework.ai import ai_enabled
from framework.ai.client import ask, model_name
from framework.catalog import RULES_DIR, Rule, load_catalog
from framework.runner import connect

PROPOSED_DIR = RULES_DIR / "proposed"
SCHEMA_PATH = RULES_DIR.parent / "db" / "schema.sql"

SYSTEM = (
    "You write PostgreSQL data-validation rules for a therapy practice-management database. "
    "A rule is a SELECT that returns one row per violation and zero rows when the data is healthy. "
    "The first column must be named defect_key: a stable text identifier of the offending entity "
    "(for example session_id::text, or 'table:' || id). Use only tables and columns in the schema "
    "you are given. Write read-only SQL with no trailing semicolon. Prefer anti-joins (NOT EXISTS), "
    "window functions and CTEs where they make the check clearer."
)


class DraftRule(BaseModel):
    id: str = Field(description="Category prefix and number, e.g. SC-003.")
    name: str
    category: str = Field(description="snake_case category, e.g. scheduling, billing, clinical_compliance.")
    severity: Literal["critical", "high", "medium", "low"]
    description: str = Field(description="Why the rule matters, in two or three sentences.")
    tags: list[str]
    sql: str


def draft_rule(story: str, client: Any = None) -> DraftRule:
    existing = ", ".join(f"{r.id} {r.name}" for r in load_catalog())
    prompt = (f"User story:\n{story}\n\nSchema:\n{SCHEMA_PATH.read_text(encoding='utf-8')}\n\n"
              f"Existing rules (do not duplicate; pick an unused id): {existing}")
    return ask(SYSTEM, prompt, DraftRule, effort="high", client=client)


def write_proposal(draft: DraftRule, story: str, directory: Path = PROPOSED_DIR) -> Path:
    """Validate the draft against the catalog contract and write it for human review."""
    entry = {**draft.model_dump(exclude={"sql"}), "expectation": "zero_rows", "sql": draft.sql.strip() + "\n"}
    Rule.from_dict(entry, directory / "draft.yaml")  # same validation as the real catalog
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{re.sub(r'[^A-Za-z0-9-]', '_', draft.id)}.yaml"
    header = (f"# AI-GENERATED DRAFT ({model_name()}). Not run until a human reviews it and moves it into rules/.\n"
              f"# Story: {' '.join(story.split())}\n")
    path.write_text(header + yaml.safe_dump([entry], sort_keys=False, width=100), encoding="utf-8")
    return path


def check_sql_plans(draft: DraftRule) -> str | None:
    """EXPLAIN (without running) the draft SQL; returns an error message if it does not plan."""
    try:
        with connect() as conn:
            conn.execute(b"EXPLAIN " + draft.sql.encode())
    except Exception as error:
        return str(error).strip()
    return None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Draft a validation rule from a user story (AI-assisted).")
    parser.add_argument("story", help="plain-English user story or business rule")
    args = parser.parse_args(argv)
    if not ai_enabled():
        print("ANTHROPIC_API_KEY is not set; the AI layer is off.", file=sys.stderr)
        return 2
    draft = draft_rule(args.story)
    path = write_proposal(draft, args.story)
    problem = check_sql_plans(draft)
    print(f"Draft written to {path}")
    print(f"SQL check: {'plans OK (not executed)' if problem is None else 'DOES NOT PLAN: ' + problem}")
    print("Review it, then move it into rules/ to activate it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
