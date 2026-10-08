"""Guards on what may be sent to (and accepted from) the model.

The project only ever holds synthetic data, but the guard is enforced in code rather
than trusted: payloads may only come from an allow-listed synthetic database, and any
identifier-shaped text is redacted before it leaves the process.
"""

from __future__ import annotations

import os
import re

# The same patterns the PR-001 SQL rule uses, so "no PHI" means one thing everywhere.
PHI_PATTERNS = {
    "ssn": re.compile(r"\d{3}-\d{2}-\d{4}"),
    "phone": re.compile(r"\(?\d{3}\)?[-. ]?\d{3}[-.]\d{4}"),
    "email": re.compile(r"[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}", re.IGNORECASE),
}
SYNTHETIC_DATABASES = {"theracare", "theracare_clean"}


class UnsafePayloadError(RuntimeError):
    """Refused to send data from a source that is not known to be synthetic."""


def find_phi(text: str) -> list[str]:
    """Names of the identifier patterns present in text."""
    return [name for name, pattern in PHI_PATTERNS.items() if pattern.search(text)]


def redact(text: str) -> str:
    for name, pattern in PHI_PATTERNS.items():
        text = pattern.sub(f"[{name.upper()} REDACTED]", text)
    return text


def ensure_synthetic_source(database: str | None = None) -> None:
    """Allow model calls only for the synthetic TheraCare databases (override: AI_ALLOWED_DATABASES)."""
    database = database or os.environ.get("PGDATABASE", "")
    allowed = set(filter(None, os.environ.get("AI_ALLOWED_DATABASES", "").split(","))) or SYNTHETIC_DATABASES
    if database not in allowed:
        raise UnsafePayloadError(f"database {database!r} is not an allow-listed synthetic source {sorted(allowed)}")
