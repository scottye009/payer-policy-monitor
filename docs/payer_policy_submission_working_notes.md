# Submission Notes

For the full implementation narrative, see`README.md`; for exact reproduction commands, see `docs/RUNNING_GUIDE.md`.

## 1. Policies selected and source links

I focused on UnitedHealthcare Commercial policies and selected examples that represent
different types of operational change:

- **MRI and CT Scan – Site of Service**
  https://www.uhcprovider.com/content/dam/provider/docs/public/policies/comm-medical-drug/mri-ct-scan-site-of-service.pdf
- **Spinraza (Nusinersen)**
  https://www.uhcprovider.com/content/dam/provider/docs/public/policies/comm-medical-drug/spinraza-nusinersen.pdf
- **Sleep Studies**
  https://www.uhcprovider.com/content/dam/provider/docs/public/policies/comm-medical-drug/sleep-studies.pdf
- **Allergen Testing Policy, Professional and Facility**
  https://www.uhcprovider.com/content/dam/provider/docs/public/policies/comm-reimbursement/COMM-Allergen-Testing-Policy.pdf
- **August 2026 Commercial Reimbursement Policy Update Bulletin**
  https://www.uhcprovider.com/content/dam/provider/docs/public/policies/comm-reimbursement/rpub/UHC-COMM-RPUB-August-2026.pdf

MRI/CT, Spinraza, and Sleep Studies were used for detailed prior/current comparison.
Their revision histories referenced earlier versions, but those historical PDFs were
not reliably available online, so I created clearly labeled simulated prior versions
for evaluation.

## 2. How changes are detected, including missing or conflicting dates

The workflow separates document monitoring from semantic change detection.

For monitoring, the payer index page is polled for signals such as `Last Published`,
while the downloaded PDF is stored with its hash, retrieval time, policy number,
effective date, and other extracted metadata. A changed publication signal or new
artifact triggers comparison with the stored prior version.

For actual policy changes, I use:

```text
text/structural comparison
    →
MPNet semantic passage alignment
    →
Qwen semantic adjudication
    →
substantive / non-substantive / uncertain
```

This is important because a highly similar sentence can still contain a material
change such as an age threshold, code, exclusion, dosage, or site-of-service rule.

Dates are preserved separately rather than forced into one field. For example, a
payer's website `Last Published` date may differ from the publication or effective
date shown in the PDF. In the prototype, those values retain their source/provenance;
missing dates remain `null`, and conflicting dates are surfaced rather than silently
reconciled. A bulletin can also provide an effective date that is not available
directly from the policy PDF.

## 3. How pediatric / hospital relevance is determined

After substantive changes are identified, they are compared with a clearly labeled
synthetic pediatric-hospital profile representing Washington geography, pediatric
services, HOPD/facility/professional billing, and relevant clinical service lines.

Relevance uses the most specific evidence available:

- codes, when the changed language contains CPT/HCPCS codes;
- age, when the policy changes an age threshold;
- state / plan applicability, when geography or product scope is explicit;
- billing or site-of-service language, such as hospital outpatient vs. inpatient;
- service area and policy language, when no usable billing code is present.

Many meaningful changes, such as Spinraza authorization criteria or Sleep Studies medical-necessity language, cannot be identified from a CPT code alone.

The output is deliberately conservative:

- `relevant` when there is a clear match,
- `irrelevant` when there is a clear mismatch,
- `needs_investigation` when applicability cannot be established confidently.

For example, the MRI policy's change from under 16 to under 18 is relevant to a
pediatric HOPD setting. By contrast, an Individual Exchange change specifically
excluding Illinois is not relevant to a Washington hospital.

The prototype profile is synthetic and does not establish that a policy actually
applies to Seattle Children's contracts or benefit products.

## 4. Performance, false positive, and an ambiguous case

Change detection was evaluated against a hand-built ground truth for the three
simulated-prior pairs (`tests/fixtures/SIMULATED_PRIORS_GROUND_TRUTH.json`, read only
by QC tooling, never by production code). Of the **9** changes the payer's own Policy
History/Revision Information section documents (2 in MRI/CT, 2 in Spinraza, 5 in Sleep
Studies), **all 9** were correctly found and classified `substantive` — **recall
9/9**. A 10th ground-truth item — the MRI/CT age threshold (Under 16 → Under 18) — was
*deliberately not* listed in Policy History, specifically to test whether detection
depends on the history section; it was also correctly found and classified
`substantive`, `revision_history_match: false`. It's reported separately from the 9/9
figure rather than folded into it, since there's no documented entry for it to have
"recalled" against — it's evidence that detection works independently of Policy
History, not a recall data point.

**One false positive remains**, found by cross-checking `data/review/
final_review_queue.json` against the ground truth: candidate `spinraza-0002`
("Changed 'On' to 'One of the following:'...") is classified `substantive` but has no
corresponding entry in the ground truth at all, positive or negative. Its underlying
prior-version text contains a bare fragment — `a; and` — that isn't valid English on
its own, strongly suggesting a truncation artifact from constructing the
simulated-prior fixture rather than a deliberate test case. It was not tuned around
and remains visible in the review queue for a human to see.

A second, now-fixed, false-positive source came from the simulated-prior PDF
generation process itself: the generated PDFs did not always preserve text objects in
natural reading order, so extraction initially combined unrelated content and footer
text, creating misleading change candidates. I fixed this upstream by switching to
position-sorted PDF extraction and reran QC.

Two of the ground truth's non-substantive wording-only test items (Spinraza's
"limited to"→"for"; Sleep Studies' age phrasing) sit in the same unmarked bullet-list
paragraph as a genuinely substantive edit, so their containing candidate
(`spinraza-0007`, `sleep_studies-0002`) is correctly classified substantive as a
whole. That's a known paragraph-granularity limitation, not a separate false positive.

I also intentionally preserve ambiguous relevance cases rather than forcing a
decision. During development, a state-specific applicability change was initially
sent to `needs_investigation` because the structured representation captured the
state but not the exclusion polarity. After reviewing the source language, the rule
was refined so an explicit Illinois-only exclusion is correctly `irrelevant` to the
Washington synthetic hospital.

The reviewer application still provides `Needs investigation` as a first-class human
decision for cases where policy or contract applicability truly remains unclear.

## 5. AI use, confidentiality, and output validation

I didn't use Ember AI in this project. AI tools were used selectively during development, but the system design and
evaluation decisions were made manually.

I designed the overall data model and pipeline structure, including the document
schema, change-record schema, relevance logic, synthetic hospital profile,
claim-impact approach, and the prompts used for policy-change adjudication.

**Claude Code** was used primarily as a coding assistant for implementation,
debugging, code review, testing, and repository polish.

**GPT** was used to help generate the clearly labeled simulated prior policy documents. The simulated priors were
manually specified and reviewed: documented revision-history changes were
intentionally reverted, and selected additional substantive/non-substantive changes
were introduced to support controlled evaluation.

At runtime, **Qwen3-235B-A22B-Instruct-2507** is used for semantic change adjudication
and structured extraction.

For confidentiality, the prototype uses only:

- public payer policy documents,
- clearly labeled simulated prior policies,
- a synthetic hospital profile, and
- synthetic claim volumes.

No patient data, Seattle Children's claims, contracts, or other confidential hospital
information were provided to these models.

Model output is not treated as source truth. Validation includes deterministic
preservation of exact before/after policy text, page/source metadata, controlled QC
against the simulated ground truth, manual review of detected changes, and final
human reviewer approval in the application.

The model interprets evidence; it does not create the evidence supporting the alert.

## 6. How I would extend this for daily multi-payer monitoring

The next step would be to turn the current batch prototype into a scheduled,
event-driven monitoring service.

```text
UHC / Premera / Regence policy sources
        ↓
daily source monitoring
        ↓
new or updated document detected
        ↓
store immutable policy version
        ↓
compare with true historical version
        ↓
semantic change detection
        ↓
match against organization profile
        ↓
estimate operational exposure
        ↓
prioritized human review queue / alert
```

Each payer would have a lightweight source adapter for its index pages, update
bulletins, and document structure, while the downstream change/relevance workflow
remains shared.

In production, the synthetic profile would be replaced by an organization-maintained
profile describing: payer products and contracts, geography, facilities and sites of
care, professional/facility billing, service lines, and relevant codes and workflows.
Synthetic claim volumes would similarly be replaced with real utilization data, and
authorization/clinical data could be incorporated when claims alone cannot determine
impact.

The queue should be update-driven rather than payer-by-payer: every newly detected
policy change enters the same review pipeline and can be prioritized across payers
based on factors such as: likely organizational relevance, effective-date proximity,
site-of-care or authorization impact, potential claim exposure, and model uncertainty.

The result would be a continuous organization-level policy monitoring workflow rather
than a set of separate manual payer reviews.

### Trying other models, and possibly fine-tuning

The prototype uses one general-purpose instruct model
(`Qwen/Qwen3-235B-A22B-Instruct-2507`) for every adjudication call. Before relying on
this for daily monitoring, I'd want to benchmark alternatives on a much larger,
held-out set of real policy changes rather than assume the current model is the best
fit:

- Other open-weight instruct models of similar or larger scale, to see whether
  accuracy on this specific task (classifying substantive vs. non-substantive payer
  policy language) actually differs meaningfully from Qwen's, and at what cost/latency
  tradeoff.
- A smaller model **fine-tuned** on a curated set of before/after passages labeled
  substantive/non_substantive/uncertain (built up from QC over time, per the point
  below). A narrow, fine-tuned model is often cheaper, faster, and more consistent on
  one well-defined classification task than a large general-purpose model prompted
  for it — worth trying once there's enough labeled data to fine-tune on responsibly.
- **Enterprise-grade proprietary model APIs** (e.g. Azure OpenAI, AWS Bedrock, or a
  vendor offering under a signed data-processing/BAA-style agreement) if their
  accuracy on this task is meaningfully better and the organization needs to feed the
  model more sensitive context later (e.g. real claims or eligibility data at the
  impact-estimation stage, which this prototype never does). The choice would be
  driven by measured accuracy and the actual data-sensitivity requirements of that
  stage, not by which model is newest.

### A larger evaluation set before deployment

Current QC is manual and small: a hand-built ground truth over three documents and 11
substantive/uncertain candidates (§4). Before deployment I would build a much larger, more
representative evaluation set (many more documents, multiple payers, and eventually
real rather than simulated prior/current pairs), track precision/recall/false-positive
rate on it over time, and re-run it automatically whenever the prompt, alignment, or
matching logic changes, rather than re-verifying a handful of examples by hand after
every change, which is what this prototype still does today.
