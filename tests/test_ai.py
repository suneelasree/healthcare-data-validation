"""Tests for the optional AI layer.

Offline tests use a fake client, so they run without an API key and prove the
guard-rails: drafts are quarantined, payloads are redacted, AI text is labelled, and
the evaluation's deterministic checks catch missing facts and leaked identifiers.
Live tests (marked `ai`) call the real model and are skipped when no key is set.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

from framework.ai import ai_enabled, evaluation, rule_drafter, triage
from framework.ai.safety import UnsafePayloadError, ensure_synthetic_source, find_phi, redact
from framework.catalog import load_catalog
from framework.reporting import write_jira_markdown
from framework.runner import RuleResult

requires_key = pytest.mark.skipif(not ai_enabled(), reason="ANTHROPIC_API_KEY not set: AI layer is off")


class FakeClient:
    """Stands in for anthropic.Anthropic(); answers each call with the next canned object."""

    def __init__(self, *answers: object, refuse: bool = False) -> None:
        self.answers = list(answers)
        self.prompts: list[str] = []
        self.refuse = refuse
        self.beta = SimpleNamespace(messages=SimpleNamespace(parse=self._parse))

    def _parse(self, **kwargs: object) -> SimpleNamespace:
        self.prompts.append(kwargs["messages"][0]["content"])  # type: ignore[index]
        if self.refuse:
            return SimpleNamespace(stop_reason="refusal", stop_details=SimpleNamespace(explanation="declined"),
                                   parsed_output=None)
        return SimpleNamespace(stop_reason="end_turn", stop_details=None, parsed_output=self.answers.pop(0))


@pytest.fixture
def failed_result() -> RuleResult:
    rule = next(r for r in load_catalog() if r.id == "PR-001")
    return RuleResult(rule, ["defect_key", "deid_id", "patterns_found"],
                      [("4740", 4740, "phone call (312) 555-0187")], 3.0)


# -- safety ---------------------------------------------------------------------

def test_phi_patterns_match_the_sql_rule() -> None:
    assert find_phi("SSN 000-12-3456, call (312) 555-0187, mail a@example.com") == ["ssn", "phone", "email"]
    assert find_phi("Therapeutic exercise (CPT 97110), 45 minutes on [DATE]") == []


def test_redaction_removes_identifiers() -> None:
    assert redact("call (312) 555-0187") == "call [PHONE REDACTED]"


def test_only_synthetic_databases_may_reach_the_model() -> None:
    ensure_synthetic_source("theracare")
    with pytest.raises(UnsafePayloadError):
        ensure_synthetic_source("production_ehr")


# -- triage ---------------------------------------------------------------------

def test_triage_prompt_is_redacted_and_output_labelled(failed_result: RuleResult, tmp_path: Path,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeClient(triage.Triage(likely_cause="De-identification skipped free-text contact lines.",
                                      next_step="Add phone patterns to the scrubber.", confidence="medium"))
    text = triage.triage_failure(failed_result, client=client)
    assert "555-0187" not in client.prompts[0]

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setattr(triage, "triage_failure", lambda result: text)
    path = write_jira_markdown(failed_result, tmp_path, "theracare")
    markdown = path.read_text()
    assert "## Likely cause (AI-generated: verify before acting)" in markdown
    assert "does not depend on this text" in markdown


def test_jira_markdown_has_no_ai_section_without_a_key(failed_result: RuleResult, tmp_path: Path,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    markdown = write_jira_markdown(failed_result, tmp_path, "theracare").read_text()
    assert "AI-generated" not in markdown and "PR-001" in markdown


def test_refusal_is_surfaced_not_swallowed(failed_result: RuleResult) -> None:
    from framework.ai.client import AIRefusalError

    with pytest.raises(AIRefusalError):
        triage.triage_failure(failed_result, client=FakeClient(refuse=True))


# -- rule drafting --------------------------------------------------------------

def test_drafts_are_quarantined_from_the_catalog(tmp_path: Path) -> None:
    draft = rule_drafter.DraftRule(
        id="SC-099", name="More than 8 sessions per provider per day", category="scheduling", severity="medium",
        description="Caseload safety limit.", tags=["group-by"],
        sql="SELECT provider_id || ':' || session_date AS defect_key FROM sessions "
            "GROUP BY provider_id, session_date HAVING count(*) > 8")
    path = rule_drafter.write_proposal(draft, "A provider must not see more than 8 patients a day",
                                       directory=tmp_path / "proposed")
    assert path.read_text().startswith("# AI-GENERATED DRAFT")
    assert "SC-099" not in {rule.id for rule in load_catalog(tmp_path)}  # loader ignores proposed/


# -- output evaluation ----------------------------------------------------------

def test_golden_set_is_well_formed() -> None:
    notes = evaluation.load_golden()
    assert len(notes) == 10
    for note in notes:
        assert all(value in note["note"] for value in note["facts"].values()), note["id"]


@pytest.mark.parametrize(("summary", "check"), [
    ("Patient had exercise on 2026-03-14 (97110).", lambda r: r.missing_facts == ["initials=J.M."]),
    ("J.M. did 97110 on 2026-03-14; call (312) 555-0143.", lambda r: r.phi_found == ["phone"]),
    ("J.M. completed therapeutic exercise (97110) on 2026-03-14.", lambda r: r.passed_checks),
])
def test_deterministic_checks(summary: str, check: Callable[[evaluation.NoteResult], bool]) -> None:
    note = evaluation.load_golden()[0]
    client = FakeClient(evaluation.Summary(summary=summary), evaluation.Judgement(faithfulness=5, reason="ok"))
    assert check(evaluation.evaluate_note(note, client=client))


def test_low_faithfulness_fails_the_evaluation() -> None:
    answers: list[object] = []
    for _ in range(10):
        answers += [evaluation.Summary(summary="placeholder"), evaluation.Judgement(faithfulness=2, reason="x")]
    report = evaluation.run_evaluation(client=FakeClient(*answers))
    assert report.mean_faithfulness == 2 and not report.passed


# -- live (real model) ----------------------------------------------------------

@pytest.mark.ai
@requires_key
def test_live_summaries_stay_faithful() -> None:
    report = evaluation.run_evaluation()
    failing = [(n.id, n.missing_facts, n.phi_found) for n in report.notes if not n.passed_checks]
    assert not failing, f"deterministic checks failed: {failing}"
    assert report.mean_faithfulness >= report.threshold


@pytest.mark.ai
@requires_key
def test_live_triage_is_labelled(failed_result: RuleResult) -> None:
    assert "does not depend on this text" in triage.triage_failure(failed_result)
