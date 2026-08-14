"""Turning one posting into one validated application draft.

Generation is retried rather than trusted. Each attempt is checked by
`validate`, and a failed attempt is re-sent with its own violations appended to
the prompt -- a small model corrects a stated, concrete fault far more reliably
than it avoids one described in advance. If every attempt fails, that is
reported as a failure; a draft that could not pass its own checks is never
quietly downgraded into the review queue.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from jobbot.llm.base import Draft, GenerationStats, LlmClient, LlmError
from jobbot.llm.prompts import DRAFT_SCHEMA, build_prompt, system_prompt
from jobbot.llm.validate import Validation, validate_draft
from jobbot.normalize import detect_language
from jobbot.profile import Profile

logger = logging.getLogger("jobbot.llm.compose")

MAX_ATTEMPTS = 3


class CompositionFailedError(LlmError):
    """No attempt produced a draft that passed validation."""


@dataclass(frozen=True, slots=True)
class Composed:
    draft: Draft
    cv_variant: str
    cv_file: str
    stats: GenerationStats
    attempts: int


def _retry_note(validation: Validation, language: str) -> str:
    faults = "\n".join(f"- {problem}" for problem in validation.violations)
    if language == "tr":
        return (
            "\n\nÖNCEKİ DENEMEN REDDEDİLDİ. Sebepleri:\n"
            f"{faults}\n"
            "Bu hataları düzelterek metni yeniden yaz. cited_detail alanını ilan "
            "metninden birebir kopyala."
        )
    return (
        "\n\nYOUR PREVIOUS ATTEMPT WAS REJECTED. Reasons:\n"
        f"{faults}\n"
        "Rewrite it fixing exactly these faults. Copy cited_detail verbatim from "
        "the posting text above."
    )


def compose(
    client: LlmClient,
    *,
    profile: Profile,
    company_name: str,
    title: str,
    location: str,
    description: str,
    language: str | None = None,
    max_attempts: int = MAX_ATTEMPTS,
) -> Composed:
    """Generate and validate one application, retrying on its own failures."""
    language = language or detect_language(description or title)
    variant = profile.cv_for(language)
    if variant is None:
        raise CompositionFailedError("no CV variant configured; see cv/profile.json")
    cv_variant, cv = variant

    base_prompt = build_prompt(
        profile=profile,
        company_name=company_name,
        title=title,
        location=location,
        description=description,
        language=language,
    )
    system = system_prompt(language)

    prompt = base_prompt
    last: Validation | None = None
    for attempt in range(1, max_attempts + 1):
        payload, stats = client.generate_json(
            system=system, prompt=prompt, schema=DRAFT_SCHEMA
        )
        draft = Draft(
            subject=str(payload.get("subject", "")).strip(),
            body=str(payload.get("body", "")).strip(),
            cited_detail=str(payload.get("cited_detail", "")).strip(),
            language=language,
        )
        validation = validate_draft(
            draft, posting=description, profile=profile, expected_language=language
        )
        if validation.ok:
            logger.info("composed %s draft for %s on attempt %d", language, company_name, attempt)
            return Composed(
                draft=draft,
                cv_variant=cv_variant,
                cv_file=cv.file,
                stats=stats,
                attempts=attempt,
            )

        last = validation
        logger.info(
            "attempt %d/%d rejected for %s: %s",
            attempt,
            max_attempts,
            company_name,
            "; ".join(validation.violations),
        )
        prompt = base_prompt + _retry_note(validation, language)

    faults = "; ".join(last.violations) if last else "unknown"
    raise CompositionFailedError(
        f"no valid draft for {company_name} after {max_attempts} attempts: {faults}"
    )
