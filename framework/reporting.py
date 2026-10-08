"""Write run reports: a JSON summary for dashboards and one Jira-ready markdown file per failure.

The HTML report comes from pytest-html (see the Makefile). When the optional AI layer is
switched on, each Jira file gains a clearly labelled AI triage section; the pass/fail
verdict itself is always the deterministic SQL result.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from framework.runner import SAMPLE_SIZE, RuleResult

JIRA_PRIORITY = {"critical": "Highest", "high": "High", "medium": "Medium", "low": "Low"}


def write_reports(results: Sequence[RuleResult], directory: Path, database: str) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    jira_dir = directory / "jira"
    for stale in jira_dir.glob("*.md"):
        stale.unlink()
    written = [write_json_summary(results, directory / "summary.json", database)]
    for result in results:
        if result.status == "fail":
            written.append(write_jira_markdown(result, jira_dir, database))
    return written


def write_json_summary(results: Sequence[RuleResult], path: Path, database: str) -> Path:
    summary = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "database": database,
        "totals": {status: sum(r.status == status for r in results) for status in ("pass", "fail", "report")},
        "rules": [
            {
                "id": r.rule.id,
                "name": r.rule.name,
                "category": r.rule.category,
                "severity": r.rule.severity,
                "status": r.status,
                "expectation": str(r.rule.expectation),
                "row_count": r.row_count,
                "duration_ms": round(r.duration_ms, 1),
                "columns": r.columns,
                "sample": r.sample(),
            }
            for r in results
        ],
    }
    path.write_text(json.dumps(summary, indent=2, default=str) + "\n", encoding="utf-8")
    return path


def write_jira_markdown(result: RuleResult, directory: Path, database: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    rule = result.rule
    lines = [
        f"# [Data Quality][{rule.severity.upper()}] {rule.id}: {rule.name}",
        "",
        f"**Summary:** `{rule.id}` returned {result.row_count} violating rows in `{database}` "
        f"(expected {rule.expectation}).",
        "",
        "| Field | Value |",
        "|---|---|",
        f"| Rule | {rule.id}: {rule.name} |",
        f"| Severity | {rule.severity.capitalize()} (Jira priority: {JIRA_PRIORITY[rule.severity]}) |",
        f"| Category | {rule.category} |",
        f"| Database | {database} |",
        f"| Detected | {datetime.now(UTC).isoformat(timespec='seconds')} |",
        f"| Rule file | rules/{rule.source} |",
        "",
        "## Description",
        "",
        rule.description,
        "",
        f"## Sample rows ({min(result.row_count, SAMPLE_SIZE)} of {result.row_count})",
        "",
        markdown_table(result.columns, result.rows[:SAMPLE_SIZE]),
        "",
        "## Steps to reproduce",
        "",
        f"Run this query against `{database}`; every returned row is a defect.",
        "",
        "```sql",
        rule.sql,
        "```",
    ]
    triage = _ai_triage(result)
    if triage:
        lines += ["", "## Likely cause (AI-generated: verify before acting)", "", *[f"> {t}" for t in triage]]
    path = directory / f"{rule.id}.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def markdown_table(columns: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    def cell(value: Any) -> str:
        return "NULL" if value is None else str(value).replace("|", "\\|")

    header = "| " + " | ".join(columns) + " |"
    divider = "|" + "---|" * len(columns)
    body = ["| " + " | ".join(cell(v) for v in row) + " |" for row in rows]
    return "\n".join([header, divider, *body])


def _ai_triage(result: RuleResult) -> list[str]:
    """Return AI triage lines when the AI layer is enabled, otherwise nothing."""
    from framework.ai import ai_enabled

    if not ai_enabled():
        return []
    from framework.ai.triage import triage_failure

    try:
        return triage_failure(result).splitlines()
    except Exception as error:  # triage is advisory; never let it break reporting
        return [f"AI triage unavailable: {error}"]
