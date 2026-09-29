# Payer Policy Review Prototype

Prototype for converting UnitedHealthcare policy updates into an
evidence-backed review queue for a synthetic pediatric hospital.

No Seattle Children's claims, contracts, patient data, or other non-public
information are used.

See `docs/payer_policy_submission_working_notes.md` for the full submission
narrative: selected policies and why, source handling details, the planned
Milestone 3-5 approach, and known gaps/tradeoffs.

## Current scope

- **Milestone 1 — complete**: deterministic collection.
- **Milestone 2 — complete**: hybrid policy change detection (deterministic
  diff + semantic alignment + Policy History corroboration + LLM
  adjudication), evaluated against a controlled simulated-prior fixture set.
- Relevance assessment and reviewer triage (Milestones 3-4) are not yet
  implemented. `config/synthetic_hospital_profile.yaml` and
  `data/synthetic_claim_volume.csv` are fixtures staged for that work but
  aren't read by any code yet.

## Setup

python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

Milestone 2's LLM adjudication step requires `HF_TOKEN` (a Hugging Face
Inference Providers token) at run time -- see `.env.example`. The local
MPNet alignment model downloads on first use (no token needed for that).

## 1. Collect documents

python scripts/collect.py

Downloads each configured UHC policy PDF, preserves the raw bytes,
extracts page-level text, and records metadata plus provenance from three
distinct sources -- the policy PDF itself, its UHC index-page listing, and
(only when the PDF has no explicit Effective Date) a linked official UHC
bulletin resolved for the configured organization geography.

Configuration lives in `config/sources.yaml`: each document's source URL,
its UHC index-page family, an optional linked bulletin for effective-date
fallback, and the organization's geography used to resolve state-specific
exceptions.

Output: `data/processed/*.json` (raw PDFs in `data/raw/`).

**Known limitation:** `allergen_testing`'s bulletin-derived `effective_date`
is currently `null`. The reimbursement bulletin's 3-column table (Policy
Title | Effective Date | Policy Summary) is read in spatial order, which
row-bands content across all three columns instead of keeping each title
contiguous, so the title-substring match in `bulletin.py` no longer finds
it. Not yet fixed.

## 2. Process simulated prior versions

python scripts/process_simulated_priors.py

Runs the same PDF extraction/metadata pipeline as step 1 against the three
controlled simulated-prior PDFs in `data/simulated_prior_raw/` (see
`docs/SIMULATED_PRIORS_README.md`). No payer URL or retrieval timestamp is
invented for these local, non-payer artifacts; `is_simulated`,
`artifact_type`, and `source_path` mark them as such.

Output: `data/simulated_prior_processed/*.json`.

## 3. Detect changes

HF_TOKEN='hf_...' python scripts/detect_changes.py

For each of the three simulated-prior/current pairs (MRI/CT, Spinraza,
Sleep Studies), runs the Milestone 2 pipeline end to end:

1. **Candidate generation** (`change.py`) -- normalizes only presentation
   noise (whitespace, line wrapping), then diffs paragraphs with
   `difflib`. The Policy History/Revision Information section is excluded
   from this diff on both sides (it's used separately as evidence, see
   step 3). CPT/HCPCS code tables and "o  " sub-bullet lists are split into
   one paragraph per item so an unrelated unchanged code/criterion isn't
   dragged into a diff candidate.
2. **Semantic alignment** -- a local `sentence-transformers/all-mpnet-base-v2`
   model (`align.py`) is used only to pair corresponding prior/current
   passages within an ambiguous multi-item diff block (combined with
   lexical overlap and position); it never decides whether a matched pair
   is substantive, and a high similarity score never suppresses a
   candidate.
3. **Policy History corroboration** -- for each candidate, checks whether
   the current policy's history section appears to describe it (requiring
   *both* meaningful semantic similarity and lexical overlap, since a long
   candidate passage can otherwise drift toward any policy-domain text on
   topic alone). Sets `revision_history_match`/`revision_history_evidence`
   but never discards an unmatched candidate.
4. **LLM adjudication** (`adjudicate.py`) -- every candidate is sent to
   Hugging Face Inference Providers (default `Qwen/Qwen3-235B-A22B-Instruct-2507`
   via the `novita` provider; configurable via `CHANGE_LLM_MODEL` /
   `CHANGE_LLM_PROVIDER`) with the exact BEFORE/AFTER text, section, and
   any matching history passage, and classified `substantive` /
   `non_substantive` / `uncertain` with structured, Pydantic-validated
   JSON output. Administrative/simulation-artifact text (a "SIMULATED
   PRIOR VERSION" banner, a bare policy-number/effective-date change, page
   header/footer/copyright boilerplate) is treated as non-substantive
   unless bundled with real rule content. Malformed LLM output falls back
   to `uncertain` rather than crashing the run.

Output: one `data/change_results/{document_id}_changes.json` per pair,
plus a combined `data/change_results/review_queue.json` (all candidates,
substantive/uncertain sorted first).

**Evaluated against a controlled ground truth** (`tests/fixtures/SIMULATED_PRIORS_GROUND_TRUTH.json`,
never read by production code) for internal QC: 9/9 documented substantive
test changes across the three pairs are correctly found and classified.
Known residual limitations, not yet fixed:
- A few real edits sit in the same unmarked bullet-list paragraph as an
  adjacent wording-only edit (no reliable bullet marker survived PDF text
  extraction for that list level), so the bundle is classified
  substantive as a whole rather than isolating the wording-only part.
- Two Spinraza differences (a garbled "a; and" fragment; an undocumented
  "planned inpatient admission" criterion) look like unintended fixture
  artifacts rather than deliberate test cases -- flagged, not tuned around.

## 4. Build review workbooks

python scripts/build_qc_workbook.py

Writes `data/change_results/change_detection_qc.xlsx` -- a human-readable
rendering of the detector's own output (one tab per policy pair, no ground
truth), safe for a blind human review.

python scripts/build_ground_truth_excel.py

Writes `tests/fixtures/SIMULATED_PRIORS_GROUND_TRUTH.xlsx` -- a plain
flattened view of the ground-truth fixture, for internal reference only.

## Run tests

pytest tests/

## Data provenance (Milestone 1)

Each record in `data/processed/` includes:

- source URL, UTC retrieval timestamp, and SHA-256 of the exact downloaded
  bytes (the raw PDF itself is preserved under `data/raw/`)
- page-level extracted text, in spatial reading order
  (`page.get_text("text", sort=True)`) so edited/reinserted text objects in
  the simulated-prior PDFs don't extract out of their visual position
- `policy_number`, `publication_date`, `revision_date` -- parsed only from
  explicit labels or a formal revision-history section in the PDF; left
  null when no such evidence exists
- `effective_date` -- the PDF's own Effective Date when present, otherwise
  resolved from a linked UHC bulletin's default or state-exception date;
  `effective_date_provenance` records the bulletin and geography whenever
  the latter applies
- `last_published_date`, `index_url`, `index_observed_at` -- from the
  document's official UHC index-page listing, kept distinct from the
  PDF-derived dates above

Missing values remain null rather than being guessed.

## Auditable output (Milestone 2)

Each `ChangeRecord` in `data/change_results/` includes exact `before_text`/
`after_text` and `revision_history_evidence` (verbatim source substrings,
never paraphrased), `change_type`, `section`, `prior_page`/`current_page`,
`semantic_similarity`, `revision_history_match`, `classification`,
`changed_dimensions`, `summary`, `reason`, `confidence`, and
`review_status` (defaults to `"pending"`) -- understandable without
reopening the source PDFs.
