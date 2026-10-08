"""Load and filter the YAML rule catalog.

A rule is pure data: adding one means adding a YAML entry, never framework code.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml

RULES_DIR = Path(__file__).resolve().parent.parent / "rules"
SEVERITIES = ("critical", "high", "medium", "low")
REQUIRED_FIELDS = ("id", "name", "category", "severity", "description", "sql", "expectation")


class CatalogError(ValueError):
    """A rule file is malformed."""


@dataclass(frozen=True)
class Expectation:
    kind: Literal["zero_rows", "max_rows", "report_only"]
    max_rows: int = 0

    @classmethod
    def parse(cls, raw: Any) -> Expectation:
        if raw in ("zero_rows", "report_only"):
            return cls(raw)
        if isinstance(raw, dict) and isinstance(raw.get("max_rows"), int):
            return cls("max_rows", raw["max_rows"])
        raise CatalogError(f"expectation must be zero_rows, report_only or {{max_rows: N}}, got {raw!r}")

    @property
    def report_only(self) -> bool:
        return self.kind == "report_only"

    def passes(self, row_count: int) -> bool:
        return row_count <= self.max_rows

    def __str__(self) -> str:
        return {"zero_rows": "0 rows", "report_only": "report only"}.get(self.kind, f"<= {self.max_rows} rows")


@dataclass(frozen=True)
class Rule:
    id: str
    name: str
    category: str
    severity: str
    description: str
    sql: str
    expectation: Expectation
    tags: tuple[str, ...]
    source: str

    @classmethod
    def from_dict(cls, raw: dict[str, Any], source: Path) -> Rule:
        missing = [f for f in REQUIRED_FIELDS if not raw.get(f)]
        if missing:
            raise CatalogError(f"{source.name}: rule {raw.get('id', '?')} is missing {', '.join(missing)}")
        if raw["severity"] not in SEVERITIES:
            raise CatalogError(f"{source.name}: rule {raw['id']} has unknown severity {raw['severity']!r}")
        return cls(
            id=raw["id"],
            name=raw["name"],
            category=raw["category"],
            severity=raw["severity"],
            description=" ".join(raw["description"].split()),
            sql=raw["sql"].strip().rstrip(";"),
            expectation=Expectation.parse(raw["expectation"]),
            tags=tuple(raw.get("tags", ())),
            source=source.name,
        )


def load_catalog(directory: Path = RULES_DIR) -> list[Rule]:
    """Load every rules/*.yaml file. Drafts in rules/proposed/ are deliberately ignored."""
    rules: list[Rule] = []
    for path in sorted(directory.glob("*.yaml")):
        entries = yaml.safe_load(path.read_text(encoding="utf-8")) or []
        rules.extend(Rule.from_dict(entry, path) for entry in entries)
    seen: set[str] = set()
    for rule in rules:
        if rule.id in seen:
            raise CatalogError(f"duplicate rule id {rule.id}")
        seen.add(rule.id)
    return sorted(rules, key=lambda r: r.id)


def select(
    rules: Iterable[Rule],
    categories: Iterable[str] = (),
    tags: Iterable[str] = (),
    severities: Iterable[str] = (),
) -> list[Rule]:
    """Keep rules matching every filter given; an empty filter matches everything."""
    categories, tags, severities = set(categories), set(tags), set(severities)
    return [
        rule for rule in rules
        if (not categories or rule.category in categories)
        and (not tags or tags & set(rule.tags))
        and (not severities or rule.severity in severities)
    ]
