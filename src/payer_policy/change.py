import difflib
import re
from dataclasses import dataclass

from payer_policy.adjudicate import adjudicate
from payer_policy.align import cosine_similarity, embed
from payer_policy.extract import find_revision_section_span
from payer_policy.models import ChangeRecord, PageText, PolicyDocument

# Landmarks of the shared UHC medical/drug policy template, used both to
# label diff context (ChangeRecord.section) and to split a Policy History
# entry into sub-entries -- never to decide whether a paragraph is
# diffable content.
_KNOWN_SECTION_HEADINGS = {
    "Application",
    "Coverage Rationale",
    "Definitions",
    "Applicable Codes",
    "Description of Services",
    "Clinical Evidence",
    "Documentation Requirements for Reviews",
    "Medical Records Documentation Used for Reviews",
    "Supporting Information",
    "U.S. Food and Drug Administration",
    "U.S. Food and Drug Administration (FDA)",
    "References",
    "Instructions for Use",
}

# A candidate is only considered "matched" to Policy History if a history
# entry clears BOTH bars -- semantic similarity alone is not enough. Long
# candidate passages (e.g. a whole multi-bullet criteria list) can drift
# above a semantic-only threshold against any policy-domain text purely
# from shared topical/domain vocabulary, with no real lexical overlap; a
# minimum lexical floor rules that out without weakening genuine matches
# (which show up with clear signal on both dimensions). Below either bar,
# revision_history_match stays False but the candidate is never dropped.
_HISTORY_SIM_THRESHOLD = 0.35
_HISTORY_LEXICAL_THRESHOLD = 0.15

# A code-table row: leading whitespace, a 5-digit CPT/HCPCS code, then its
# description. Matching >=2 such lines in one paragraph means it's a code
# table, not prose -- see _split_list_items.
_CODE_LINE_RE = re.compile(r"^\s*\d{5}\s+\S")

# A criteria-list line whose bullet glyph didn't survive extraction as text
# (common in these templates) but is still recognizable as its own item by
# the "o  " sub-bullet marker used one level down.
_SUB_BULLET_LINE_RE = re.compile(r"^\s*o\s{2,}\S")

# A paragraph ending in a dangling "and"/"or" is a list that continues past
# an incidental blank line, not a real paragraph break -- see
# _segment_page_paragraphs.
_DANGLING_CONJUNCTION_RE = re.compile(r"(?:;|,)?\s*(?:and|or)\s*$", re.IGNORECASE)


@dataclass
class Paragraph:
    raw_text: str
    normalized_text: str
    page_number: int
    section: str | None


@dataclass
class Candidate:
    change_type: str  # "added" | "removed" | "modified"
    before: Paragraph | None
    after: Paragraph | None
    semantic_similarity: float | None


def normalize_text(text: str) -> str:
    """Collapse whitespace and line-wrapping only. Never touch numbers,
    ages, dates, codes, negation, geography, dosage, or coverage/billing
    language -- those must stay intact for candidate generation to see."""
    return re.sub(r"\s+", " ", text).strip()


def _segment_page_paragraphs(text: str) -> list[str]:
    """Split a page's text into paragraphs on blank-line boundaries,
    returning each paragraph's exact original substring (including its
    internal line wrapping) so it can be used as source evidence.

    A blank line right after a dangling "and"/"or" doesn't end a criteria
    list -- it's merged back into the same paragraph, so a list item that
    happens to sit after an incidental blank line isn't diffed as if it
    were a separate, disconnected paragraph from its own list.
    """
    raw_paragraphs: list[str] = []
    current: list[str] = []
    for line in text.splitlines(keepends=True):
        if line.strip():
            current.append(line)
        elif current:
            raw_paragraphs.append("".join(current))
            current = []
    if current:
        raw_paragraphs.append("".join(current))

    merged: list[str] = []
    for paragraph in raw_paragraphs:
        if merged and _DANGLING_CONJUNCTION_RE.search(merged[-1].rstrip()):
            merged[-1] += paragraph
        else:
            merged.append(paragraph)
    return merged


def _split_list_items(raw: str) -> list[str] | None:
    """If `raw` looks like a CPT/HCPCS code table or a bulleted sub-criteria
    list (multiple lines each starting with a recognizable item marker),
    split it into one paragraph per item.

    Without this, an unrelated unchanged code/criterion gets dragged into
    the same diff candidate as a genuinely added/removed/modified one, and
    a removed item and its replacement can't line up as a single clean
    'modified' pair. Returns None if `raw` doesn't look like a marked list.
    """
    lines = raw.splitlines(keepends=True)
    for pattern in (_CODE_LINE_RE, _SUB_BULLET_LINE_RE):
        item_starts = [i for i, line in enumerate(lines) if pattern.match(line)]
        if len(item_starts) < 2:
            continue
        items = []
        for idx, start in enumerate(item_starts):
            end = item_starts[idx + 1] if idx + 1 < len(item_starts) else len(lines)
            items.append("".join(lines[start:end]))
        if item_starts[0] > 0:
            items[0] = "".join(lines[: item_starts[0]]) + items[0]
        return items
    return None


def revision_history_text(pages: list[PageText]) -> str | None:
    """Return the current policy's Policy History/Revision Information
    section text, or None if it isn't present."""
    full_text = "\n".join(page.text for page in pages)
    span = find_revision_section_span(full_text)
    if span is None:
        return None
    start, end = span
    return full_text[start:end]


def _strip_revision_history(pages: list[PageText]) -> list[PageText]:
    """Remove the Policy History/Revision Information section from a
    document's pages before diffing. That section is used separately as
    supporting evidence (see revision_history_text), not as diffable
    content, on both the prior and current side."""
    full_text = "\n".join(page.text for page in pages)
    span = find_revision_section_span(full_text)
    if span is None:
        return pages

    start, end = span
    result = []
    offset = 0
    for page in pages:
        page_start = offset
        page_end = offset + len(page.text)
        offset = page_end + 1  # +1 for the "\n" separator used above

        excl_start = max(start, page_start)
        excl_end = min(end, page_end)
        if excl_start >= excl_end:
            result.append(page)
            continue

        local_start = excl_start - page_start
        local_end = excl_end - page_start
        kept_text = page.text[:local_start] + page.text[local_end:]
        result.append(PageText(page_number=page.page_number, text=kept_text))
    return result


def _build_paragraphs(pages: list[PageText]) -> list[Paragraph]:
    current_section: str | None = None
    paragraphs = []
    for page in pages:
        for raw in _segment_page_paragraphs(page.text):
            for item in _split_list_items(raw) or [raw]:
                normalized = normalize_text(item)
                if not normalized:
                    continue
                heading = normalized.rstrip(":")
                if len(heading) <= 80 and heading in _KNOWN_SECTION_HEADINGS:
                    current_section = heading
                    continue  # headings are structure, not diffable content
                paragraphs.append(Paragraph(item, normalized, page.page_number, current_section))
    return paragraphs


# Splits a Policy History entry on the same section-heading vocabulary used
# to label body paragraphs (e.g. "Applicable Codes", "Supporting
# Information"). History sub-entries are labeled with those same headings,
# but -- unlike the body -- aren't reliably blank-line-separated once
# extracted in spatial reading order, so relying on blank lines alone
# would blur unrelated entries together and weaken corroboration matching.
#
# A heading only counts as a split point if it occupies the rest of its
# line (optionally preceded by other content, like a date sharing its
# row) -- otherwise a heading name that happens to appear mid-sentence
# (e.g. "...Clinical Evidence and References sections...") would be
# mistaken for a real heading and fragment an unrelated entry.
_HISTORY_HEADING_SPLIT_RE = re.compile(
    "(" + "|".join(re.escape(h) for h in sorted(_KNOWN_SECTION_HEADINGS, key=len, reverse=True)) + r")[ \t]*(?=\n|$)"
)


def _history_paragraphs(history_text: str | None) -> list[tuple[str, str]]:
    """Return (raw_text, normalized_text) pairs for each sub-entry in the
    current policy's Policy History/Revision Information section."""
    if not history_text:
        return []

    tokens = _HISTORY_HEADING_SPLIT_RE.split(history_text)
    raw_entries = [tokens[0]] if tokens[0].strip() else []
    for i in range(1, len(tokens), 2):
        heading = tokens[i]
        following = tokens[i + 1] if i + 1 < len(tokens) else ""
        raw_entries.append(heading + following)

    pairs = []
    for raw in raw_entries:
        normalized = normalize_text(raw)
        if normalized:
            pairs.append((raw.strip("\n"), normalized))
    return pairs


def _align_replace_block(prior_block: list[Paragraph], current_block: list[Paragraph]) -> list[Candidate]:
    """Deterministic diff already found this block as changed; this only
    decides which prior paragraph corresponds to which current paragraph
    when a block holds more than one on either side. It never decides
    whether a matched pair is substantive."""
    if not prior_block:
        return [Candidate("added", None, c, None) for c in current_block]
    if not current_block:
        return [Candidate("removed", p, None, None) for p in prior_block]

    prior_vecs = embed([p.normalized_text for p in prior_block])
    current_vecs = embed([c.normalized_text for c in current_block])

    remaining_current = list(range(len(current_block)))
    unmatched_prior: list[int] = []
    candidates: list[Candidate] = []

    for i, prior_para in enumerate(prior_block):
        if not remaining_current:
            unmatched_prior.append(i)
            continue

        best_j, best_score, best_sim = None, -1.0, 0.0
        for j in remaining_current:
            sim = cosine_similarity(prior_vecs[i], current_vecs[j])
            lexical = difflib.SequenceMatcher(
                None, prior_para.normalized_text, current_block[j].normalized_text
            ).ratio()
            position = 1 - abs((i / len(prior_block)) - (j / len(current_block)))
            score = 0.5 * sim + 0.3 * lexical + 0.2 * position
            if score > best_score:
                best_score, best_j, best_sim = score, j, sim

        remaining_current.remove(best_j)
        candidates.append(Candidate("modified", prior_para, current_block[best_j], best_sim))

    candidates.extend(Candidate("removed", prior_block[i], None, None) for i in unmatched_prior)
    candidates.extend(Candidate("added", None, current_block[j], None) for j in remaining_current)
    return candidates


def _diff_paragraphs(prior_paragraphs: list[Paragraph], current_paragraphs: list[Paragraph]) -> list[Candidate]:
    """Step 1: deterministic normalization + text diff. This is what
    DISCOVERS changes; formatting-only differences that become identical
    after normalize_text() collapse into "equal" opcodes and never become
    candidates. Everything else does -- candidate generation favors
    recall, so nothing here is dropped for being similar."""
    prior_norm = [p.normalized_text for p in prior_paragraphs]
    current_norm = [p.normalized_text for p in current_paragraphs]
    matcher = difflib.SequenceMatcher(None, prior_norm, current_norm, autojunk=False)

    candidates: list[Candidate] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        prior_block = prior_paragraphs[i1:i2]
        current_block = current_paragraphs[j1:j2]
        if tag == "delete":
            candidates.extend(Candidate("removed", p, None, None) for p in prior_block)
        elif tag == "insert":
            candidates.extend(Candidate("added", None, c, None) for c in current_block)
        elif tag == "replace":
            candidates.extend(_align_replace_block(prior_block, current_block))
    return candidates


def _match_history_evidence(
    candidate: Candidate,
    history_pairs: list[tuple[str, str]],
    history_vecs: list[list[float]],
) -> tuple[bool, str | None]:
    """Step 3: does the current Policy History section appear to describe
    this change? Never used to discard a candidate -- only to attach
    supporting evidence when found."""
    if not history_pairs:
        return False, None

    query_para = candidate.after or candidate.before
    query_vec = embed([query_para.normalized_text])[0]

    best_score, best_raw = -1.0, None
    for (raw, normalized), vec in zip(history_pairs, history_vecs):
        sim = cosine_similarity(query_vec, vec)
        lexical = difflib.SequenceMatcher(None, query_para.normalized_text.lower(), normalized.lower()).ratio()
        if sim < _HISTORY_SIM_THRESHOLD or lexical < _HISTORY_LEXICAL_THRESHOLD:
            continue
        if sim > best_score:
            best_score, best_raw = sim, raw

    if best_raw is not None:
        return True, best_raw
    return False, None


def detect_changes(prior: PolicyDocument, current: PolicyDocument, *, adjudicate_fn=adjudicate) -> list[ChangeRecord]:
    """Run the full Milestone 2 pipeline for one prior/current policy pair,
    using only their already-processed PolicyDocument representations
    (never reopening a PDF)."""
    history_text = revision_history_text(current.pages)
    history_pairs = _history_paragraphs(history_text)
    history_vecs = embed([normalized for _, normalized in history_pairs]) if history_pairs else []

    prior_paragraphs = _build_paragraphs(_strip_revision_history(prior.pages))
    current_paragraphs = _build_paragraphs(_strip_revision_history(current.pages))

    candidates = _diff_paragraphs(prior_paragraphs, current_paragraphs)

    records: list[ChangeRecord] = []
    for index, candidate in enumerate(candidates):
        matched, evidence = _match_history_evidence(candidate, history_pairs, history_vecs)
        section = (candidate.after or candidate.before).section

        adjudication = adjudicate_fn(
            before_text=candidate.before.raw_text if candidate.before else None,
            after_text=candidate.after.raw_text if candidate.after else None,
            section=section,
            revision_history_evidence=evidence,
            document_title=current.title,
            document_type=current.document_type,
        )

        records.append(
            ChangeRecord(
                change_id=f"{current.document_id}-{index:04d}",
                document_id=current.document_id,
                prior_policy_number=prior.policy_number,
                current_policy_number=current.policy_number,
                prior_is_simulated=prior.is_simulated,
                effective_date=current.effective_date,
                source_url=current.source_url,
                change_type=candidate.change_type,
                before_text=candidate.before.raw_text if candidate.before else None,
                after_text=candidate.after.raw_text if candidate.after else None,
                section=section,
                prior_page=candidate.before.page_number if candidate.before else None,
                current_page=candidate.after.page_number if candidate.after else None,
                semantic_similarity=candidate.semantic_similarity,
                revision_history_match=matched,
                revision_history_evidence=evidence,
                classification=adjudication.classification,
                changed_dimensions=adjudication.changed_dimensions,
                summary=adjudication.summary,
                reason=adjudication.reason,
                confidence=adjudication.confidence,
                why_it_may_matter=adjudication.why_it_may_matter,
                billing_setting=adjudication.billing_setting,
                service_area=adjudication.service_area,
                age_min=adjudication.age_min,
                age_max=adjudication.age_max,
                codes=adjudication.codes,
                states=adjudication.states,
                plan_scope=adjudication.plan_scope,
                additional_data_needed=adjudication.additional_data_needed,
                review_status="pending",
            )
        )

    return records
