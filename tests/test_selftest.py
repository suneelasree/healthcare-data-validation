"""Self-test: prove the checks themselves work.

* Planted database: every pass/fail rule returns exactly the rows the seed manifest
  says it planted for that rule. Missing a row means the rule is blind to a defect;
  an extra row means it raises false alarms.
* Clean database (same data, no defects): every pass/fail rule returns zero rows.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

import psycopg
import pytest

from framework.catalog import Rule, load_catalog
from framework.runner import format_table, run_rule

MANIFEST_PATH = Path(__file__).resolve().parent.parent / "db" / "planted_defects.json"
CATALOG = load_catalog()
CHECK_RULES = [rule for rule in CATALOG if not rule.expectation.report_only]


@pytest.fixture(scope="module")
def expected() -> dict[str, set[str]]:
    """Rule id -> the defect keys the manifest says that rule must return."""
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    keys: dict[str, set[str]] = defaultdict(set)
    for defect in manifest["defects"]:
        for rule_id, rule_keys in defect["expected"].items():
            keys[rule_id].update(rule_keys)
    return keys


def test_manifest_only_references_known_rules(expected: dict[str, set[str]]) -> None:
    unknown = set(expected) - {rule.id for rule in CATALOG}
    assert not unknown, f"manifest references rules that are not in the catalog: {sorted(unknown)}"


def test_every_check_rule_has_a_planted_defect(expected: dict[str, set[str]]) -> None:
    untested = [rule.id for rule in CHECK_RULES if not expected.get(rule.id)]
    assert not untested, f"rules with no planted defect, so the self-test cannot prove them: {untested}"


@pytest.mark.parametrize("check", CHECK_RULES, ids=[rule.id for rule in CHECK_RULES])
def test_rule_catches_exactly_its_planted_rows(check: Rule, planted_db: psycopg.Connection,
                                               expected: dict[str, set[str]]) -> None:
    result = run_rule(planted_db, check)
    found, wanted = result.defect_keys(), expected.get(check.id, set())
    missed, unexpected = sorted(wanted - found), sorted(found - wanted)
    assert not missed and not unexpected, (
        f"{check.id} {check.name}\n  missed planted rows: {missed}\n  unexpected rows:     {unexpected}\n\n"
        + format_table(result.columns, result.rows)
    )


@pytest.mark.parametrize("check", CHECK_RULES, ids=[rule.id for rule in CHECK_RULES])
def test_rule_passes_on_clean_data(check: Rule, clean_db: psycopg.Connection) -> None:
    result = run_rule(clean_db, check)
    assert result.row_count == 0, (
        f"{check.id} {check.name} flagged {result.row_count} rows in clean data (false positives)\n\n"
        + format_table(result.columns, result.rows)
    )
