"""Optional AI layer. AI assists; deterministic SQL checks decide; humans approve."""

from __future__ import annotations

import os


def ai_enabled() -> bool:
    """The AI layer switches on only when an API key is present."""
    return bool(os.environ.get("ANTHROPIC_API_KEY"))
