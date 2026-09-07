"""Shared Gemini helpers: model selection + retries on overloaded models."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from google.genai import errors as genai_errors

logger = logging.getLogger("automations.gemini")

DEFAULT_GEMINI_MODEL = "gemini-flash-latest"
DEFAULT_FALLBACKS = (
    "gemini-3.5-flash",
    "gemini-3.7-flash",
    "gemini-3-flash-preview",
    "gemini-3.6-flash",
)


def resolve_gemini_model() -> str:
    return (os.getenv("GEMINI_MODEL") or DEFAULT_GEMINI_MODEL).strip() or DEFAULT_GEMINI_MODEL


def resolve_fallback_models(primary: str) -> list[str]:
    raw = os.getenv("GEMINI_FALLBACK_MODELS")
    if raw is None:
        candidates = list(DEFAULT_FALLBACKS)
    else:
        candidates = [m.strip() for m in raw.split(",") if m.strip()]
    ordered = [primary]
    for model in candidates:
        if model not in ordered:
            ordered.append(model)
    return ordered


def _is_retriable(exc: BaseException) -> bool:
    if isinstance(exc, genai_errors.ServerError):
        return True
    text = str(exc).lower()
    return any(
        token in text
        for token in ("503", "unavailable", "high demand", "resource_exhausted", "429")
    )


async def generate_content_with_fallback(
    client: Any,
    *,
    contents: Any,
    config: Any,
    primary_model: str | None = None,
    attempts_per_model: int = 2,
) -> Any:
    """Call generate_content, retrying and falling back when Gemini is overloaded."""
    primary = primary_model or resolve_gemini_model()
    models = resolve_fallback_models(primary)
    last_exc: BaseException | None = None

    for model in models:
        for attempt in range(1, attempts_per_model + 1):
            try:
                response = await client.aio.models.generate_content(
                    model=model,
                    contents=contents,
                    config=config,
                )
                if model != primary or attempt > 1:
                    logger.info("Gemini OK with model=%s (attempt %s)", model, attempt)
                return response
            except Exception as exc:  # noqa: BLE001 - need to inspect API errors
                last_exc = exc
                if not _is_retriable(exc):
                    raise
                logger.warning(
                    "Gemini model=%s attempt=%s failed (%s); retrying/falling back",
                    model,
                    attempt,
                    exc,
                )
                await asyncio.sleep(0.7 * attempt)

    assert last_exc is not None
    raise last_exc
