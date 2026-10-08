"""AI output evaluation: catch drift when the summarization prompt or model changes.

For each golden note the model writes a summary, then three checks run:

1. Deterministic facts (patient initials, date, CPT code) appear in the summary.
2. No identifier patterns (SSN, phone, email) appear in the summary.
3. An AI judge scores faithfulness to the source note from 1 to 5.

Checks 1 and 2 are plain code. Check 3 is model-graded, so it is aggregated (mean
across the set) and held to a threshold rather than trusted note by note.

    python -m framework.ai.evaluation
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from framework.ai import ai_enabled
from framework.ai.client import ask
from framework.ai.safety import find_phi

GOLDEN_PATH = Path(__file__).resolve().parent / "golden_notes.yaml"
THRESHOLD = float(os.environ.get("AI_FAITHFULNESS_THRESHOLD", "4.0"))

SUMMARY_SYSTEM = (
    "Summarize a therapy session note for a billing reviewer in one or two sentences. Always include "
    "the patient's initials exactly as written, the session date in YYYY-MM-DD form, and the CPT code. "
    "Never include phone numbers, email addresses or other contact details."
)
JUDGE_SYSTEM = (
    "You grade summaries of clinical session notes for faithfulness. Score 5 if every statement in the "
    "summary is supported by the note, 3 if it contains a minor unsupported detail or distortion, and 1 if "
    "it contradicts the note or invents clinical facts. Omissions are fine; judge only what is stated."
)


class Summary(BaseModel):
    summary: str


class Judgement(BaseModel):
    faithfulness: int = Field(ge=1, le=5)
    reason: str = Field(description="One sentence naming any unsupported statement.")


@dataclass
class NoteResult:
    id: str
    summary: str
    missing_facts: list[str]
    phi_found: list[str]
    faithfulness: int
    judge_reason: str

    @property
    def passed_checks(self) -> bool:
        return not self.missing_facts and not self.phi_found


@dataclass
class EvalReport:
    notes: list[NoteResult] = field(default_factory=list)
    threshold: float = THRESHOLD

    @property
    def mean_faithfulness(self) -> float:
        return sum(n.faithfulness for n in self.notes) / len(self.notes)

    @property
    def passed(self) -> bool:
        return all(n.passed_checks for n in self.notes) and self.mean_faithfulness >= self.threshold


def load_golden(path: Path = GOLDEN_PATH) -> list[dict[str, Any]]:
    notes: list[dict[str, Any]] = yaml.safe_load(path.read_text(encoding="utf-8"))
    for note in notes:
        note["facts"] = {key: str(value) for key, value in note["facts"].items()}
    return notes


def missing_facts(summary: str, facts: dict[str, str]) -> list[str]:
    return [f"{name}={value}" for name, value in facts.items() if value not in summary]


def evaluate_note(note: dict[str, Any], client: Any = None) -> NoteResult:
    summary = ask(SUMMARY_SYSTEM, note["note"], Summary, effort="low", client=client).summary
    judgement = ask(JUDGE_SYSTEM, f"Note:\n{note['note']}\n\nSummary:\n{summary}", Judgement,
                    effort="medium", client=client)
    return NoteResult(note["id"], summary, missing_facts(summary, note["facts"]), find_phi(summary),
                      judgement.faithfulness, judgement.reason)


def run_evaluation(client: Any = None) -> EvalReport:
    return EvalReport([evaluate_note(note, client) for note in load_golden()])


def main() -> int:
    if not ai_enabled():
        print("ANTHROPIC_API_KEY is not set; the AI layer is off.", file=sys.stderr)
        return 2
    report = run_evaluation()
    for n in report.notes:
        status = "ok" if n.passed_checks else f"missing={n.missing_facts} phi={n.phi_found}"
        print(f"{n.id}  faithfulness={n.faithfulness}  {status}\n     {n.summary}")
    print(f"\nMean faithfulness {report.mean_faithfulness:.2f} (threshold {report.threshold}) -> "
          f"{'PASS' if report.passed else 'FAIL'}")
    out = Path(os.environ.get("REPORT_DIR", "reports")) / "ai_eval.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"label": "AI-generated output evaluation", "passed": report.passed,
                               "mean_faithfulness": report.mean_faithfulness,
                               "notes": [asdict(n) for n in report.notes]}, indent=2) + "\n")
    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
