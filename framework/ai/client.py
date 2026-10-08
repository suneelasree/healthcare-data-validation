"""Thin wrapper over the Anthropic API: one structured-output call, validated by Pydantic."""

from __future__ import annotations

import os
from typing import Any, TypeVar

from pydantic import BaseModel

DEFAULT_MODEL = "claude-opus-5-5"
ModelT = TypeVar("ModelT", bound=BaseModel)


class AIRefusalError(RuntimeError):
    """The model declined the request."""


def model_name() -> str:
    return os.environ.get("ANTHROPIC_MODEL", DEFAULT_MODEL)


def ask(system: str, prompt: str, schema: type[ModelT], *, effort: str = "medium", client: Any = None) -> ModelT:
    """Send one request and return the response parsed into `schema`.

    Structured output (output_format) guarantees schema-valid JSON, so callers get a
    typed object rather than free text to scrape. The server-side fallback re-routes the
    request if the primary model declines it on policy grounds.
    """
    if client is None:
        import anthropic

        client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
    response = client.beta.messages.parse(
        model=model_name(),
        max_tokens=16000,
        system=system,
        messages=[{"role": "user", "content": prompt}],
        output_format=schema,
        output_config={"effort": effort},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    if response.stop_reason == "refusal":
        raise AIRefusalError(getattr(response.stop_details, "explanation", None) or "model declined the request")
    parsed: ModelT = response.parsed_output
    return parsed
