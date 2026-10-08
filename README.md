# Healthcare SQL Data Validation Framework

A working framework that validates a healthcare practice-management database with SQL.
Every business rule is a query that returns **zero rows when the data is healthy**; any
row it returns is a defect, and the row itself is the evidence.

The domain is **TheraCare**, a fictional multi-practice therapy platform (PT, OT, speech
and behavioral health) covering scheduling, sessions, clinical notes, insurance
authorizations, claims (EDI 837), remittances (EDI 835) and telehealth.

> **All data is synthetic.** Names come from fixed lists, phone numbers use the fictional
> 555-01xx range, emails use `example.com`. There is no real patient data or PHI anywhere.

## Quick start

Requirements: Docker. Nothing else needs to be installed locally.

```bash
make test        # start Postgres, seed, lint, self-test, rule suite, performance, AI checks, demo
```

`make test` takes under a minute. It runs each step in a container:

| Step | What it proves |
|---|---|
| `lint` | ruff passes with zero warnings |
| `seed` | builds two databases: `theracare` (24 planted defects) and `theracare_clean` (same data, no defects) |
| `selftest` | every rule catches **exactly** its planted rows, and finds nothing in clean data |
| `rules` | the full rule suite is green on clean data (the CI gate) |
| `perf` | the five heaviest rules stay inside a time budget; required indexes exist |
| `ai` | AI guard-rails hold (offline); live AI tests run only when an API key is set |
| `demo` | the rule suite against the planted database: 23 failures, as intended, with reports |

Other entry points:

```bash
make validate ARGS="--severity critical"          # CLI: filter by --category, --tag, --severity
make validate ARGS="--category billing --tag window-function"
docker compose exec db psql -U theracare -d theracare   # explore the data (port 5433 from the host)
```

On Windows without `make`, run the `docker compose ...` lines from the Makefile directly.

## Architecture

```
            rules/*.yaml  (26 rules: SQL + metadata, no code)
                  |
                  v
 framework/catalog.py  -- load + validate rules, filter by category/tag/severity
 framework/runner.py   -- run each rule read-only, collect rows as evidence, CLI
 framework/reporting.py-- JSON summary + one Jira-ready markdown per failure
                  |
      +-----------+-------------+------------------+
      v           v             v                  v
 test_rules   test_selftest  test_performance   test_ai
 (1 test per  (planted vs    (EXPLAIN ANALYZE,  (optional AI layer:
  rule)        clean seeds)   index checks)      assists, never decides)
```

**Python runs the rules; SQL does the checking.** The core framework (catalog, runner, reporting) is under 400 lines of
Python. All domain logic lives in SQL inside YAML, where a reviewer can read it.

```
healthcare-data-validation/
  docker-compose.yml       Postgres 16 + a Python 3.11 runner container
  Makefile                 make test runs everything
  db/schema.sql            17 tables, every one carrying practice_id (tenant key)
  db/seed.py               synthetic data generator + defect planter + manifest
  rules/*.yaml             the rule catalog, one file per category
  rules/proposed/          AI-drafted rules waiting for human review (never run)
  framework/               catalog, runner, reporting, ai/
  tests/                   rules, self-test, performance, AI
  .github/workflows/ci.yml Postgres service, seed, suite, self-test, artifacts
```

### Key design decisions

- **A reporting replica, not the OLTP database.** The schema enforces primary keys but not
  foreign keys or business-key uniqueness, as is typical for replicated and warehouse copies.
  That gap (orphans, duplicates, cross-tenant rows) is exactly what the rules cover.
- **Rules are read-only.** The runner sets `default_transaction_read_only`, so a rule can
  never modify the data it checks.
- **A stable `defect_key`.** Every pass/fail rule returns a `defect_key` first column (for
  example a claim id, or `appointment_a|appointment_b`). This is what lets the self-test
  compare a rule's output to the manifest row by row.
- **Deterministic seed.** One seeded RNG and unconditional random draws mean the clean and
  planted databases are identical except for the planted rows. `--seed N` produces a
  different dataset; the self-test passes for any seed.

## The rule catalog

26 rules in 12 categories. Two are report-only (they produce a summary table instead of
pass/fail), and one uses a numeric threshold instead of zero rows.

| Category | Rules |
|---|---|
| Data integrity | DI-001 duplicate MRN · DI-002 session without appointment · DI-003 note without session · DI-004 required field NULL · DI-005 duplicate patient (normalized name + DOB) · DI-006 past appointment still "scheduled" (threshold: up to 5) |
| Scheduling | SC-001 provider double-booked (self-join) · SC-002 appointment ends before it starts |
| Clinical compliance | CC-001 claim without a signed note · CC-002 note signed more than 72h late · CC-003 minor without a guardian |
| Licensing | LI-001 session after license expiry |
| Authorizations | AU-001 sessions beyond the approved count (CTE + HAVING) |
| Billing | BR-001 underpayment beyond 2% tolerance · BR-002 no remittance after 45 days · BR-003 paid version is not the latest (ROW_NUMBER) · BR-004 top 3 denial codes per payer (RANK, report) |
| Telehealth | TH-001 billed minutes exceed video minutes by more than 5 |
| Patient engagement | PE-001 gap of more than 30 days for active patients (LAG) · PE-002 monthly retention cohort (report) |
| Multi-tenant isolation | MT-001 child row in a different practice than its parent (11 parent/child pairs) |
| Privacy | PR-001 SSN, phone or email pattern in a de-identified note (regex) |
| ML data quality | ML-001 feature computed after claim submission (leakage) |
| Migration | MG-001 row counts · MG-002 column aggregates · MG-003 EXCEPT-based row diff |

## Self-test: testing the tests

A validation rule that never fires looks exactly like healthy data. So the seed plants
24 known defects and writes `db/planted_defects.json`, an answer key listing the exact
`defect_key` each rule must return:

```json
{ "id": "D07", "description": "Provider 37 is double-booked: appointments 16618 and 22805 overlap",
  "expected": { "SC-001": ["16618|22805"] } }
```

`tests/test_selftest.py` then asserts, for every rule:

1. **Planted database:** found keys == expected keys. A missed key means the rule is blind;
   an extra key means it raises false alarms.
2. **Clean database:** zero rows.

When one defect legitimately trips several rules, the manifest says so. For example, a row
dropped by the migration is expected in MG-001 (count), MG-002 (aggregate) and MG-003 (row diff).
Defect D24 (a date of birth shifted by a day) is deliberately caught **only** by MG-003, which
shows why the cheaper count and aggregate checks are not enough on their own.

Weakening a rule makes the self-test fail. Changing CC-002's window from 72 to 100 hours
fails with `missed planted rows: ['13738']`.

## Add a rule in under five minutes

1. Write the query. It must return one row per violation, with `defect_key` first.
2. Add an entry to the right `rules/<category>.yaml`:

   ```yaml
   - id: SC-003
     name: Provider sees more than 8 patients in a day
     category: scheduling
     severity: medium
     description: >
       Caseload safety limit: more than 8 attended sessions per provider per day.
     tags: [group-by]
     expectation: zero_rows          # or {max_rows: N}, or report_only
     sql: |
       SELECT provider_id || ':' || session_date AS defect_key,
              provider_id, session_date, count(*) AS sessions
       FROM sessions
       GROUP BY provider_id, session_date
       HAVING count(*) > 8
   ```

3. Plant a defect for it in `db/seed.py` (`Planter.plant_operational`) and record the
   expected key. The self-test refuses to pass until every pass/fail rule has a planted defect.
4. Run `make test`.

No framework code changes: the test suite, CLI filters and reports pick the rule up automatically.

## Three complex queries, explained

### 1. Provider double-booking: an index-friendly self-join (SC-001)

```sql
FROM appointments a
JOIN appointments b
  ON b.provider_id = a.provider_id
 AND b.start_time >= a.start_time               -- b starts no earlier than a ...
 AND b.start_time <  a.end_time                 -- ... and before a ends
 AND (b.start_time > a.start_time OR b.appointment_id > a.appointment_id)
WHERE a.status <> 'cancelled' AND b.status <> 'cancelled'
```

The textbook overlap test is `a.start < b.end AND b.start < a.end`. It is correct but gives the
planner only one bound on `b.start_time`, so Postgres compared every pair of a provider's
appointments: **781 ms**. Two intervals overlap exactly when the later-starting one begins
before the earlier one ends. Writing it that way bounds `b.start_time` on both sides, which
turns the join into a tight range scan on the `(provider_id, start_time)` index: **19 ms,
about 30× faster**. The last condition reports each pair once while still handling two
appointments that start at the same minute. `tests/test_performance.py` keeps it fast.

### 2. Authorization overuse: CTE + GROUP BY + HAVING (AU-001)

```sql
WITH usage AS (
    SELECT a.authorization_id, a.approved_sessions,
           count(s.session_id) AS sessions_used
    FROM authorizations a
    JOIN sessions s
      ON s.patient_id = a.patient_id
     AND s.session_date BETWEEN a.start_date AND a.end_date
    GROUP BY a.authorization_id
    HAVING count(s.session_id) > a.approved_sessions
)
SELECT authorization_id::text AS defect_key, *, sessions_used - approved_sessions AS sessions_over
FROM usage
```

A payer approves N sessions inside a date window. The join is a **range join**: each session
is counted against every authorization whose window contains it, not just by patient.
`HAVING` filters on the aggregate, which a `WHERE` clause cannot do. Grouping by the primary
key lets Postgres return the other authorization columns without listing them all in the
`GROUP BY` (functional dependency). The output gives the billing team the overage directly.

### 3. Resubmitted claims paid on the wrong version: ROW_NUMBER (BR-003)

```sql
WITH ranked AS (
    SELECT claim_id, claim_number, version,
           ROW_NUMBER() OVER (PARTITION BY claim_number ORDER BY version DESC) AS recency
    FROM claims
)
SELECT paid.claim_number AS defect_key, paid.version AS paid_version, latest.version AS latest_version
FROM ranked paid
JOIN remittances r    ON r.claim_id = paid.claim_id AND r.denial_code IS NULL
JOIN ranked latest    ON latest.claim_number = paid.claim_number AND latest.recency = 1
WHERE paid.recency > 1
```

A corrected claim keeps its claim number and increments the version, and only the latest
version should be paid. `ROW_NUMBER()` ranks versions within each claim number (1 = latest)
without collapsing rows the way `GROUP BY` would. The query then joins the ranking to itself:
one side is the version that was actually paid, the other is the latest version. Any paid row
with `recency > 1` means the payer and the practice disagree about what was billed.

## Performance

`tests/test_performance.py` runs `EXPLAIN (ANALYZE, FORMAT JSON)` on every rule, picks the
five slowest **by measurement** (not a hand-maintained list) and fails if any exceeds
`PERF_BUDGET_MS` (default 2000 ms). It also asserts the 11 indexes the rules depend on exist.
Current worst case: MG-003 at about 100 ms over 20k sessions.

## Reports

Every run writes to `reports/<database>/`:

- `report.html`: pytest-html; failures show severity, rule and up to 10 violating rows,
  and report-only rules attach their summary table.
- `summary.json`: per-rule status, row count, duration and a sample, for dashboards.
- `jira/<RULE>.md`: one Jira-ready ticket per failure (summary, severity mapped to Jira
  priority, sample rows, reproduction SQL).

## Optional AI layer

**AI assists, deterministic checks decide, humans approve.** The framework runs fully
without it. Every AI feature switches on only when `ANTHROPIC_API_KEY` is set, and none of
them can change a rule's pass/fail result.

| Feature | Command | Guard-rail |
|---|---|---|
| Rule drafting | `make draft-rule STORY="..."` | Drafts go to `rules/proposed/`, which the catalog never loads. The SQL is validated with `EXPLAIN` (not executed). A human moves it into `rules/`. |
| Failure triage | automatic in Jira markdown | Appears under *"Likely cause (AI-generated: verify before acting)"*, after the deterministic verdict. |
| Output evaluation | `make ai-eval` | 10 synthetic golden notes. Deterministic checks (initials, date, CPT present; no SSN/phone/email in the output), plus an AI-judge faithfulness score with a mean threshold of 4.0 out of 5, to catch drift when the prompt or model changes. |

Safety: payloads may only come from the allow-listed synthetic databases, identifier
patterns are redacted before anything leaves the process, and responses use structured
output validated by Pydantic. The offline tests in `tests/test_ai.py` prove these guard-rails
with a fake client; the live tests skip without a key.

## CI

`.github/workflows/ci.yml` runs on every push: a Postgres 16 service, lint, seed both
databases, self-test, clean rule suite, performance, AI tests, then the planted-data demo
(`continue-on-error`, since failures are the point). All reports and the manifest are
uploaded as the `validation-reports` artifact. Add `ANTHROPIC_API_KEY` as a repository
secret to switch on the live AI tests.
