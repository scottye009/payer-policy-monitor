# Running Guide

Step-by-step reproduction of the full pipeline, in order. Run everything from the
`payer-policy-monitor/` directory.

## 0. Setup

```
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Copy `.env.example` and fill in `HF_TOKEN` if you want to export it once instead of
prefixing individual commands (steps 3 and 4 below both need it):

```
cp .env.example .env   # then edit .env, or just export HF_TOKEN yourself
```

- `HF_TOKEN` — a Hugging Face Inference Providers token. Required for step 3 (Qwen
  adjudication runs live, always — there is no offline/mocked mode in production).
  Never commit this token or paste it into a tracked file.
- `CHANGE_LLM_MODEL` / `CHANGE_LLM_PROVIDER` — optional overrides; default to
  `Qwen/Qwen3-235B-A22B-Instruct-2507` via `novita`.
- `CHANGE_ALIGN_MODEL` — optional override for the local MPNet alignment model; default
  `sentence-transformers/all-mpnet-base-v2`. Downloads on first use, no token needed.

## 1. Collect the real UHC policies

```
python scripts/collect.py
```

Downloads the 5 configured documents (`config/sources.yaml`) from their live
`uhcprovider.com` URLs, saves raw PDFs to `data/raw/`, and writes structured JSON with
metadata + provenance to `data/processed/`. Takes a few seconds; needs network access,
no token.

## 2. Process the simulated prior versions

```
python scripts/process_simulated_priors.py
```

Runs the *same* extraction pipeline as step 1 against the three local, clearly-labeled
simulated-prior PDFs in `data/simulated_prior_raw/` (see
`docs/SIMULATED_PRIORS_README.md`). No network access needed. Writes to
`data/simulated_prior_processed/`.

## 3. Detect changes (needs `HF_TOKEN`)

```
HF_TOKEN='hf_...' python scripts/detect_changes.py
```

Runs the full Milestone 2 pipeline (candidate generation → semantic alignment → Policy
History corroboration → Qwen adjudication) for all three prior/current pairs. This is a
**live LLM call per candidate** (~50 calls total) — expect it to take a few minutes and
to cost real (small) money against your HF account.

Writes:

- `data/review/{document_id}_changes.json` — one per pair
- `data/review/review_queue.json` — combined, substantive/uncertain sorted first

`data/change_results/` is a frozen historical QC snapshot, committed to git — this
script no longer writes there.

## 4. (Optional) Human-readable QC workbooks

```
python scripts/build_qc_workbook.py        # from data/change_results/ (historical)
python scripts/build_review_workbook.py    # from data/review/review_queue.json (current)
python scripts/build_ground_truth_excel.py # flattens the ground-truth fixture, dev-only
```

These just reformat existing JSON into `.xlsx` (openpyxl).
`build_ground_truth_excel.py`'s output and its input
(`tests/fixtures/SIMULATED_PRIORS_GROUND_TRUTH.json`) are for internal QC only; neither
is read by any production script.

## 5. Relevance + synthetic claim-volume impact

```
python scripts/review_processing.py
```

Deterministic post-processing of `data/review/review_queue.json` against
`config/synthetic_hospital_profile.yaml` and `data/synthetic_claim_volume.csv`. Adds
`relevance`, `relevance_reason`, `potential_annual_claim_lines`, `impact_note` to every
substantive/uncertain record (non_substantive records get these fields as `null`).

Writes:

- `data/review/final_review_queue.json`
- `data/review/final_review_queue.xlsx` — **the primary reviewer-facing artifact**:
  color-coded by `relevance`, wrapped text, frozen header, auto-filter. Open this one
  first.

Prints relevance counts on completion, e.g. `{'relevant': 10, 'irrelevant': 1}`.

## 6. Run the tests

```
pytest tests/
```

42 tests, no `HF_TOKEN` needed (the LLM call is always mocked/injected in tests — see
`tests/test_change.py`'s `_fake_adjudicate` pattern). Should take under 10 seconds; the
one-time MPNet model load happens once per test session.

## 7. Launch the reviewer app

```
streamlit run app.py
```

Opens in your browser (default `http://localhost:8501`). Reads only
`data/review/final_review_queue.json` plus PDFs from `data/raw/`/
`data/simulated_prior_raw/`. Record a decision on the required items (automated `relevance` of
`relevant` or `needs_investigation`), then "Submit completed review" writes
`data/review/completed_review.json` and `.../completed_review_summary.csv`.

## Full pipeline, one shot

```
source .venv/bin/activate
python scripts/collect.py
python scripts/process_simulated_priors.py
HF_TOKEN='hf_...' python scripts/detect_changes.py
python scripts/review_processing.py
pytest tests/
streamlit run app.py
```
