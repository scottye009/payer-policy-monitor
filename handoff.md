# Payer Policy Monitor — Work Trial Handoff

## Goal

Extend the existing payer-policy-monitor into a repeatable reviewer workflow that can:

1. check payer policy sources for updates,
2. compare the exact previous and current versions,
3. surface meaningful evidence-backed changes,
4. persist reviewer decisions across restarts, and
5. rerun safely without duplicate findings.

---

## What changed and why

### 1. Persistent source/version state

Added persistent source monitoring using SQLite plus immutable PDF snapshots.

- `policy_versions` stores one row per unique `(policy_id, content_sha256)`.
- `source_checks` records every check as `initialized`, `updated`, `unchanged`, or `failed`.
- Live snapshots are saved under `data/monitor/snapshots/<policy_id>/<sha256>.pdf`.
- Failed fetches never replace the last known-good version.
- A source that changes A → B → A is handled correctly because current state comes from check chronology, not version insertion order.

Why: the original take-home compared fixed files but did not maintain source history or workflow state across runs.

### 2. Streamlit update checking

Added an explicit **Check for updates** action to the app.

- Network access happens only on the button click.
- Normal Streamlit reruns read status from SQLite with `get_status()`.
- The UI shows status, timestamps, hashes, version origin, and errors.
- Failed retrieval is visibly different from unchanged.

Why: Streamlit reruns frequently, so source checks must not happen implicitly during page rendering.

### 3. Updated version → existing analysis pipeline

Added `scripts/update_pipeline.py` as a small adapter between persisted versions and the original pipeline.

Flow:

`previous_hash/current_hash → exact snapshots → PolicyDocument → existing detect_changes() → existing review processing`

Only an `updated` policy is eligible for analysis, and analysis runs only after the reviewer explicitly clicks **Analyze changes**.

Why: source checking is cheap and deterministic; MPNet + Qwen analysis is slower and potentially paid. Keeping them separate avoids unnecessary model calls.

### 4. Persistent findings and human review

Added a `findings` table to the existing `data/monitor/state.db`.

Each finding has a deterministic `finding_id` based on:

- policy id,
- previous/current version hashes,
- change type,
- before/after source text,
- section,
- prior/current page.

LLM-generated fields such as classification, summary, relevance, and confidence are intentionally excluded from identity.

Human review fields are persisted separately:

- `pending / reviewed / dismissed`,
- review note,
- `reviewed_at`.

Why: rerunning the exact same A → B comparison should not create duplicates or reset review state, while a new B → C revision must create a new pending finding.

### 5. Reviewer UI cleanup

The app now separates:

1. **Source evidence** — exact prior/current snapshots, before/after text, section/page references, policy metadata.
2. **Automated interpretation** — classification, relevance, confidence, summary, extracted scope, and impact.
3. **Human review** — status and note.

Other UI changes:

- Live Policy Monitoring and Historical Review Queue are separated.
- Non-substantive findings are hidden by default but remain inspectable.
- Exact captured snapshots are available per finding.
- Findings are shown as collapsible rows.
- Completed reviews can be exported as CSV or JSON.
- **Reset live demo** clears only live monitoring state and snapshots.


### 6. Additional real-prior policies

The monitor was extended beyond the two simulated-baseline examples to also support real locally provided prior UHC PDFs:

- Surgery of the Elbow
- Home Health, Skilled, and Custodial Care Services

---

## Environment / running the app

This Intel Mac requires a Python 3.12 environment for the existing MPNet dependency stack.

The working environment is `.venv312` with compatible versions including PyTorch 2.2.2 and `sentence-transformers`.

Run:

```bash
source .venv312/bin/activate
HF_TOKEN=hf_... CHANGE_LLM_PROVIDER=nscale streamlit run app.py
```

`scaleway` can also be used and was faster in spot testing:

```bash
HF_TOKEN=hf_... CHANGE_LLM_PROVIDER=scaleway streamlit run app.py
```

The production detection code was not changed to accommodate the Intel-Mac environment.

---

## How to run the evaluation

The baseline evaluation is intentionally lightweight and does **not** rerun the LLM.

It reads the already-generated take-home output and compares it against the eight known substantive ground-truth cases for MRI/CT and Sleep Studies.

Run:

```bash
.venv312/bin/python scripts/eval_baseline.py \
  --out data/eval/results/<run_name>.json \
  --csv
```

The original pre-tuning baseline is stored at:

```text
data/eval/results/baseline_before_tuning.json
data/eval/results/baseline_before_tuning.csv
```

The script uses deterministic text anchors and evaluates at the **expected-case level**, so one finding may satisfy more than one expected case.

---

## Evaluation results

### Before any detection tuning

Ground-truth baseline:

- Expected substantive cases: **8**
- True positives: **8/8**
- Uncertain: **0**
- False negatives: **0**
- Potential false positives: **0**

Breakdown:

- MRI/CT: 3/3 expected substantive changes recovered.
- Sleep Studies: 5/5 expected substantive changes recovered.
- One Sleep Studies finding contains two expected changes, so seven substantive finding records still represent eight expected cases.

Policy History corroboration matched only 3 of the 7 documented cases because of the existing Sleep Studies sub-heading splitter weakness. This did **not** reduce detection recall because Policy History is corroborative rather than the sole detection mechanism.

Conclusion: no detection change was justified by the baseline.

### Regression against the original take-home output

The new live-version path reproduced the original take-home behavior:

- MRI/CT: 12/12 candidates reproduced exactly.
- Across MRI/CT + Sleep Studies: classification and relevance matched the take-home on **36/36** findings in a real Qwen run through nscale.

### Fix made after the baseline

A Home Health finding exposed one schema-parsing failure:

- the text change was punctuation-only,
- Qwen correctly returned `non_substantive`,
- but Qwen used out-of-vocabulary enrichment values such as `home_health`,
- Pydantic rejected the whole response and the pipeline fell back to `uncertain`.

The fix only coerces invalid optional enrichment fields such as `billing_setting` or `service_area` to `other`. Required fields such as classification, summary, reason, and confidence are never coerced.

After the fix:

- the real failing reply parses as `non_substantive`,
- malformed required fields still fall back safely,
- full tests passed,
- the original baseline remained **8/8 TP, 0 FP**.

This was a robustness fix to response parsing, not a change to candidate generation, MPNet alignment, the Qwen prompt/model, or the expected-case detection logic.

---

## Validation summary

At the latest recorded state:

- Full test suite: **87 passed**.
- Source monitoring, persistence, review, export, reset, update-pipeline integration, and response-parsing regression cases are covered.
- Live UHC checks were exercised in isolated copies of the repo so test resets do not delete the working app state.

---

## Known limitations

1. **Updated but never analyzed**
   - The UI currently reflects the latest source check.
   - If an `updated` pair is not analyzed and a later check returns `unchanged`, the original Analyze button is no longer surfaced.
   - Demo order is therefore: **Updated → Analyze → check again**.

2. **Local-only prototype storage**
   - SQLite and filesystem snapshots are appropriate for the work trial, but there is no multi-user concurrency, authentication, remote object storage, or deployment layer.

3. **Small diagnostic evaluation**
   - The 8-case baseline is useful for regression/debugging but is not a production performance estimate.
   - The two newer real-prior policies do not have hand-labeled ground truth.

4. **Model/provider dependency**
   - Qwen inference depends on the configured Hugging Face provider and token.
   - Provider availability and latency can vary.

5. **Spinraza failure-test configuration**
   - The current work-trial notes include a deliberately broken Spinraza URL used to demonstrate a failed fetch while preserving the prior version.
   - Restore the real URL before treating the configuration as a normal production-like monitor.

---

## Next two things I would build

### 1. Persist analysis state for version pairs

Add an explicit analysis-state record for each `updated` comparison, for example:

```text
pending_analysis → analyzed → failed_analysis
```

This would let the app continue surfacing an unanalyzed A → B comparison even after a later source check returns unchanged, eliminating the current workflow limitation without tying analysis state to the latest check status.

### 2. Add a broader labeled evaluation set and operational metrics

Create a larger hand-reviewed set across multiple payer policies and track:

- substantive-change recall,
- false-positive rate,
- abstention/uncertain rate,
- retrieval failures,
- analysis latency,
- reviewer agreement / override rate.

That would provide evidence for where model/detection tuning is actually needed before adding more payers or production infrastructure.

---

## Short architecture summary

```text
Known payer URLs / local priors
        ↓
Persistent source monitor
(SQLite checks + version hashes + immutable snapshots)
        ↓
UPDATED only
        ↓
Update pipeline adapter
        ↓
Existing take-home pipeline
(PDF extraction → diff → MPNet → Qwen → Policy History → relevance)
        ↓
Persistent findings
        ↓
Streamlit reviewer workflow
(source evidence → automated interpretation → human review → export)
```

The core design principle was to add reliable workflow state around the original detection pipeline, and only change model-related behavior when an observed failure justified it.
