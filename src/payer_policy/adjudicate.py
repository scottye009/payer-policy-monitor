import json
import os

import requests
from pydantic import ValidationError

from payer_policy.models import LLMAdjudication

ROUTER_URL = "https://router.huggingface.co/v1/chat/completions"
DEFAULT_MODEL = "Qwen/Qwen3-235B-A22B-Instruct-2507"
DEFAULT_PROVIDER = "novita"
DEFAULT_TIMEOUT = 120.0

_FALLBACK_REASON = "LLM did not return valid structured JSON matching the required adjudication schema."

_SYSTEM_PROMPT = (
    "You are adjudicating a single detected change between a prior and current version of a "
    "UnitedHealthcare payer policy PDF, for a pediatric hospital policy review team. You are given "
    "the exact BEFORE passage, the exact AFTER passage, the section they fall under, and (if found) "
    "a matching passage from the current policy's Policy History/Revision Information section.\n\n"
    "Classify the change as one of:\n"
    "- substantive: materially changes coverage/medical necessity, eligibility, age/numeric "
    "thresholds, authorization, treatment/dosage/frequency, CPT/HCPCS codes, geography/plan "
    "applicability, site of service, professional/facility billing, reimbursement, or exclusions.\n"
    "- non_substantive: wording, grammar, formatting, or stylistic changes that preserve meaning.\n"
    "- uncertain: you cannot confidently tell from the given text.\n\n"
    "Administrative and simulation-artifact text is never substantive on its own -- classify it "
    "non_substantive even though it is being added/removed/changed, unless it is bundled together "
    "with genuine coverage/rule content in the same passage. This includes: a 'SIMULATED PRIOR "
    "VERSION' or similar test-artifact banner; a bare policy number and/or effective date changing "
    "with no accompanying rule text; and page numbers, running headers/footers, or copyright/"
    "proprietary-information boilerplate.\n\n"
    "Pay close attention to logical connectives (\"and\" vs \"or\") and negation. Replacing \"and\" "
    "with \"or\" (or the reverse) between two conditions changes whether BOTH must hold or EITHER "
    "is sufficient -- this broadens or narrows who/what qualifies even when most of the wording is "
    "identical or highly similar, and must be classified substantive.\n\n"
    "Base your answer only on the BEFORE/AFTER text given -- never invent or paraphrase evidence "
    "that isn't in the passages provided. The Policy History passage is supporting context, not an "
    "exhaustive list of what changed; a change may be substantive even if the history doesn't "
    "mention it, and high textual/semantic similarity between BEFORE and AFTER never by itself "
    "makes a change non_substantive.\n\n"
    "Respond with ONLY a single JSON object matching this schema, no other text, no markdown fence:\n"
    '{"classification": "substantive|non_substantive|uncertain", "summary": "...", "reason": "...", '
    '"changed_dimensions": ["..."], "confidence": 0.0}'
)


class AdjudicationError(Exception):
    """Raised when the LLM API call itself fails (network/HTTP error)."""


def _build_user_prompt(
    before_text: str | None,
    after_text: str | None,
    section: str | None,
    revision_history_evidence: str | None,
) -> str:
    parts = [
        f"Section: {section or 'unknown'}",
        "BEFORE:\n" + (before_text if before_text is not None else "(none -- this passage was added)"),
        "AFTER:\n" + (after_text if after_text is not None else "(none -- this passage was removed)"),
    ]
    if revision_history_evidence:
        parts.append("Matching Policy History/Revision Information passage:\n" + revision_history_evidence)
    else:
        parts.append("No matching Policy History/Revision Information passage was found for this change.")
    return "\n\n".join(parts)


def _resolve_model() -> str:
    model = os.environ.get("CHANGE_LLM_MODEL", DEFAULT_MODEL)
    provider = os.environ.get("CHANGE_LLM_PROVIDER", DEFAULT_PROVIDER)
    return f"{model}:{provider}" if provider else model


def call_llm(prompt: str, *, model: str | None = None, token: str | None = None, timeout: float = DEFAULT_TIMEOUT) -> str:
    """Call the configured HF Inference Providers chat model and return the
    raw text of its reply. This is a live call -- the LLM always runs in
    production; only tests mock it."""
    model = model or _resolve_model()
    token = token or os.environ.get("HF_TOKEN")
    if not token:
        raise AdjudicationError("HF_TOKEN is not set; the LLM adjudication step requires it.")

    headers = {"Authorization": f"Bearer {token}"}
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
    }
    try:
        response = requests.post(ROUTER_URL, headers=headers, json=payload, timeout=timeout)
    except requests.RequestException as exc:
        raise AdjudicationError(f"request to {model} failed: {exc}") from exc

    if response.status_code != 200:
        raise AdjudicationError(f"non-200 response ({response.status_code}) from {model}: {response.text[:500]}")

    data = response.json()
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise AdjudicationError(f"unexpected response shape from {model}: {data!r}") from exc


def parse_llm_response(raw_text: str) -> LLMAdjudication:
    """Parse and validate the LLM's raw reply against the required schema.

    Explicitly handles malformed output (non-JSON, or JSON missing/violating
    required fields) by returning a safe 'uncertain' fallback rather than
    raising, so one bad LLM reply can't crash the whole detection run.
    """
    text = raw_text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()

    try:
        payload = json.loads(text)
        return LLMAdjudication(**payload)
    except (json.JSONDecodeError, ValidationError, TypeError):
        return LLMAdjudication(
            classification="uncertain",
            summary="LLM adjudication could not be completed.",
            reason=_FALLBACK_REASON,
            changed_dimensions=[],
            confidence=0.0,
        )


def adjudicate(
    before_text: str | None,
    after_text: str | None,
    section: str | None,
    revision_history_evidence: str | None,
    *,
    call_fn=call_llm,
) -> LLMAdjudication:
    prompt = _build_user_prompt(before_text, after_text, section, revision_history_evidence)
    raw_text = call_fn(prompt)
    return parse_llm_response(raw_text)
