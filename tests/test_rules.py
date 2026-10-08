"""One test per catalog rule, run against PGDATABASE.

A failing test shows the rule, its severity and up to 10 violating rows.
Report-only rules always pass and attach their summary table to the HTML report.
"""

from __future__ import annotations

from collections.abc import Callable

import psycopg
import pytest

from framework.catalog import Rule
from framework.reporting import markdown_table
from framework.runner import RuleResult, describe_failure, run_rule


def test_rule(rule: Rule, db: psycopg.Connection, record_result: Callable[[RuleResult], None],
              extras: list) -> None:
    result = run_rule(db, rule)
    record_result(result)

    if rule.expectation.report_only:
        import pytest_html

        extras.append(pytest_html.extras.text(markdown_table(result.columns, result.rows), name=rule.name))
        assert result.row_count > 0, f"{rule.id} is report-only but returned no rows"
        return

    if not result.passed:
        pytest.fail(describe_failure(result), pytrace=False)
