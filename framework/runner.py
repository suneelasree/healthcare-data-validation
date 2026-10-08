"""Run validation rules against PostgreSQL.

Python runs the rules; SQL does the checking. A rule passes when its query returns
no rows (or no more than its threshold). Every returned row is a defect, and the row
itself is the evidence.

    python -m framework.runner                         # every rule
    python -m framework.runner --severity critical     # filter by severity
    python -m framework.runner --category billing --tag window-function
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg

from framework.catalog import SEVERITIES, Rule, load_catalog, select

SAMPLE_SIZE = 10
KEY_COLUMN = "defect_key"


class RuleContractError(RuntimeError):
    """A pass/fail rule did not return a defect_key as its first column."""


@dataclass
class RuleResult:
    rule: Rule
    columns: list[str]
    rows: list[tuple[Any, ...]]
    duration_ms: float

    @property
    def row_count(self) -> int:
        return len(self.rows)

    @property
    def status(self) -> str:
        if self.rule.expectation.report_only:
            return "report"
        return "pass" if self.rule.expectation.passes(self.row_count) else "fail"

    @property
    def passed(self) -> bool:
        return self.status != "fail"

    def sample(self, limit: int = SAMPLE_SIZE) -> list[dict[str, Any]]:
        return [dict(zip(self.columns, row, strict=True)) for row in self.rows[:limit]]

    def defect_keys(self) -> set[str]:
        return {str(row[0]) for row in self.rows}


def connect(dbname: str | None = None) -> psycopg.Connection:
    """Connect using the standard PG* environment variables, in read-only mode."""
    conn = psycopg.connect("", dbname=dbname, autocommit=True)
    conn.execute("SET default_transaction_read_only = on")  # rules can never modify data
    return conn


def run_rule(conn: psycopg.Connection, rule: Rule) -> RuleResult:
    started = time.perf_counter()
    cursor = conn.execute(rule.sql.encode())  # bytes: rule SQL is trusted catalog text, not a template
    rows = cursor.fetchall()
    duration_ms = (time.perf_counter() - started) * 1000
    columns = [column.name for column in cursor.description or ()]
    if not rule.expectation.report_only and (not columns or columns[0] != KEY_COLUMN):
        raise RuleContractError(f"{rule.id}: first column must be '{KEY_COLUMN}', got {columns[:1]}")
    return RuleResult(rule, columns, rows, duration_ms)


def run_rules(conn: psycopg.Connection, rules: Iterable[Rule]) -> list[RuleResult]:
    return [run_rule(conn, rule) for rule in rules]


def format_table(columns: Sequence[str], rows: Sequence[Sequence[Any]], limit: int = SAMPLE_SIZE) -> str:
    """Render rows as a fixed-width text table, truncating long values."""
    shown = [[_cell(value) for value in row] for row in rows[:limit]]
    widths = [max(len(c), *(len(r[i]) for r in shown)) if shown else len(c) for i, c in enumerate(columns)]
    lines = [" | ".join(c.ljust(w) for c, w in zip(columns, widths, strict=True)),
             "-+-".join("-" * w for w in widths)]
    lines += [" | ".join(v.ljust(w) for v, w in zip(row, widths, strict=True)) for row in shown]
    if len(rows) > limit:
        lines.append(f"... {len(rows) - limit} more rows")
    return "\n".join(lines)


def _cell(value: Any, width: int = 40) -> str:
    text = "NULL" if value is None else str(value)
    return text if len(text) <= width else text[: width - 3] + "..."


def describe_failure(result: RuleResult) -> str:
    rule = result.rule
    return (f"[{rule.severity.upper()}] {rule.id} {rule.name}: {result.row_count} violating rows "
            f"(expected {rule.expectation})\n{rule.description}\n\n"
            f"Sample (up to {SAMPLE_SIZE} rows):\n{format_table(result.columns, result.rows)}")


def print_summary(results: Sequence[RuleResult]) -> None:
    print(f"{'RULE':<8} {'SEVERITY':<9} {'STATUS':<7} {'ROWS':>6} {'MS':>8}  NAME")
    for r in results:
        print(f"{r.rule.id:<8} {r.rule.severity:<9} {r.status.upper():<7} {r.row_count:>6} "
              f"{r.duration_ms:>8.1f}  {r.rule.name}")
    counts = {s: sum(r.status == s for r in results) for s in ("pass", "fail", "report")}
    print(f"\n{counts['pass']} passed, {counts['fail']} failed, {counts['report']} report-only")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the healthcare data validation rules.")
    parser.add_argument("--category", action="append", default=[], help="only this category (repeatable)")
    parser.add_argument("--tag", action="append", default=[], help="only rules with this tag (repeatable)")
    parser.add_argument("--severity", action="append", default=[], choices=SEVERITIES, help="repeatable")
    parser.add_argument("--dbname", help="database to validate (default: PGDATABASE)")
    parser.add_argument("--report-dir", type=Path, default=Path(os.environ.get("REPORT_DIR", "reports")))
    parser.add_argument("--no-reports", action="store_true", help="print results only")
    parser.add_argument("--list", action="store_true", help="list the selected rules and exit")
    args = parser.parse_args(argv)

    rules = select(load_catalog(), args.category, args.tag, args.severity)
    if args.list:
        for rule in rules:
            print(f"{rule.id:<8} {rule.severity:<9} {rule.category:<20} {rule.name}")
        return 0

    with connect(args.dbname) as conn:
        database = conn.info.dbname
        results = run_rules(conn, rules)

    print(f"Database: {database}\n")
    print_summary(results)
    for result in results:
        if result.status == "fail":
            print("\n" + describe_failure(result))
    if not args.no_reports:
        from framework.reporting import write_reports

        written = write_reports(results, args.report_dir / database, database)
        print(f"\nReports: {', '.join(str(p) for p in written)}")
    return 0 if all(r.passed for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
