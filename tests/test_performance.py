"""Performance: EXPLAIN ANALYZE the five heaviest rules against a time budget,
and assert the indexes those rules rely on exist.

The heaviest rules are measured, not hard-coded: every rule is profiled once and the
five slowest are held to PERF_BUDGET_MS (default 2000 ms).
"""

from __future__ import annotations

import os
from typing import Any

import psycopg
import pytest

from framework.catalog import Rule, load_catalog

BUDGET_MS = float(os.environ.get("PERF_BUDGET_MS", "2000"))
HEAVIEST = 5
EXPECTED_INDEXES = {
    "idx_appointments_provider_start",  # SC-001 overlap self-join
    "idx_sessions_appointment",          # DI-002, MT-001
    "idx_sessions_patient_date",         # AU-001, PE-001
    "idx_clinical_notes_session",        # CC-001, CC-002
    "idx_claims_session",                # MT-001
    "idx_claims_number_version",         # BR-003
    "idx_remittances_claim",             # BR-001, BR-002
    "idx_telehealth_logs_session",       # TH-001
    "idx_ml_features_claim",             # ML-001
    "idx_guardians_patient",             # CC-003
    "idx_patients_practice_mrn",         # DI-001
}


def explain_analyze(conn: psycopg.Connection, rule: Rule) -> dict[str, Any]:
    cursor = conn.execute(b"EXPLAIN (ANALYZE, FORMAT JSON) " + rule.sql.encode())
    plan: dict[str, Any] = cursor.fetchone()[0][0]
    return plan


@pytest.fixture(scope="module")
def heaviest(db: psycopg.Connection) -> list[tuple[Rule, float]]:
    timings = [(rule, explain_analyze(db, rule)["Execution Time"]) for rule in load_catalog()]
    timings.sort(key=lambda item: item[1], reverse=True)
    print("\nRule execution times (EXPLAIN ANALYZE):")
    for rule, ms in timings:
        print(f"  {rule.id:<8} {ms:>8.1f} ms  {rule.name}")
    return timings[:HEAVIEST]


@pytest.mark.parametrize("rank", range(HEAVIEST), ids=[f"heaviest-{n + 1}" for n in range(HEAVIEST)])
def test_heaviest_rules_within_budget(rank: int, heaviest: list[tuple[Rule, float]]) -> None:
    rule, ms = heaviest[rank]
    assert ms <= BUDGET_MS, f"{rule.id} {rule.name} took {ms:.1f} ms, budget is {BUDGET_MS:.0f} ms"


def test_expected_indexes_exist(db: psycopg.Connection) -> None:
    rows = db.execute("SELECT indexname FROM pg_indexes WHERE schemaname = 'public'").fetchall()
    missing = EXPECTED_INDEXES - {name for (name,) in rows}
    assert not missing, f"missing indexes: {sorted(missing)}"
