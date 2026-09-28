# Payer Policy Monitoring Prototype — Submission Working Notes

> Working draft for the Ember take-home submission. Update this document as Milestones 2–4 are completed.

## 1. Scope and assumptions

This prototype evaluates public UnitedHealthcare Commercial policy material for a **synthetic pediatric hospital in Seattle, Washington**. It does not use Seattle Children’s claims, contracts, patient data, or other nonpublic information.

The prototype intentionally prioritizes a small number of policies and an evidence-backed reviewer workflow over broad payer coverage.

Current source families:
- UnitedHealthcare Commercial Medical & Drug Policies
- UnitedHealthcare Commercial Reimbursement Policies
- UnitedHealthcare Commercial Reimbursement Policy Update Bulletins

Organization context used for applicability resolution:
- Geography: Washington
- Organization type: pediatric hospital
- Billing settings of interest: hospital outpatient department (HOPD) and professional billing
- Population: pediatric / young-adult services as defined in the synthetic profile

## 2. Selected policies and source links

### A. MRI and CT Scan — Site of Service
Type: UnitedHealthcare Commercial and Individual Exchange Medical Policy

Source:
https://www.uhcprovider.com/content/dam/provider/docs/public/policies/comm-medical-drug/mri-ct-scan-site-of-service.pdf

Why selected:
- Direct HOPD / site-of-service relevance.
- Contains pediatric language independent of CPT matching.
- Current version contains formal Policy History/Revision Information.

### B. Spinraza (Nusinersen)
Type: UnitedHealthcare Commercial Medical Benefit Drug Policy

Source:
https://www.uhcprovider.com/content/dam/provider/docs/public/policies/comm-medical-drug/spinraza-nusinersen.pdf

Why selected:
- Strong pediatric clinical relevance.
- Contains coverage criteria, documentation requirements, and a formal revision history.
- Demonstrates that relevance cannot be determined from procedure codes alone.

### C. Allergen Testing Policy, Professional and Facility
Type: UnitedHealthcare Commercial and Individual Exchange Reimbursement Policy

Source:
https://www.uhcprovider.com/content/dam/provider/docs/public/policies/comm-reimbursement/COMM-Allergen-Testing-Policy.pdf

Why selected:
- Explicit Professional and Facility scope.
- Creates a useful ambiguous pediatric case because the rule applies to individuals age 20 or older.
- Has distinct policy publication, website Last Published, and implementation/effective-date signals.

### Supplemental validation: Sleep Studies
Source:
https://www.uhcprovider.com/content/dam/provider/docs/public/policies/comm-medical-drug/sleep-studies.pdf

Why included:
- Formal revision history contains explicit before/after wording.
- Useful for validating substantive-change detection and pediatric relevance.

### Update bulletin used for change evidence
UnitedHealthcare Commercial Reimbursement Policy Update Bulletin — August 2026

Source:
https://www.uhcprovider.com/content/dam/provider/docs/public/policies/comm-reimbursement/rpub/UHC-COMM-RPUB-August-2026.pdf

## 3. Source handling and provenance

Milestone 1 uses deterministic collection:
1. Retrieve the official UHC index page and capture `Last Published`.
2. Download the configured policy PDF from the payer’s official URL.
3. Preserve the exact downloaded PDF bytes.
4. Compute SHA-256 over those bytes.
5. Extract text page-by-page with PyMuPDF.
6. Extract only explicitly supported metadata.
7. Write a structured JSON representation with source URL, retrieval timestamp, hash, page text, and metadata.

Important date concepts remain separate:
- `publication_date`: explicit policy-history publication event when available
- `last_published_date`: value observed on the UHC index
- `revision_date`: most recent dated entry in formal Policy History/Revision Information
- `effective_date`: explicit policy effective date, or a geography-resolved effective date from an official linked bulletin when the policy PDF does not provide one

Missing values remain null rather than being guessed.

When an effective date is resolved from a bulletin, the output keeps lightweight provenance identifying the bulletin and configured geography.

## 4. Change detection

### Planned Milestone 2 approach

Change detection will separate three questions:

1. **Did the artifact change?**
   - Compare SHA-256 across retrieved snapshots.
   - If hashes are identical, there is no byte-level document change.

2. **Did the normalized text change?**
   - Normalize whitespace and other presentation-only differences.
   - Compare page/section text to avoid treating line wrapping or formatting as a substantive policy change.

3. **Did the rule change?**
   - Extract the changed passages and classify the change using the payer’s own policy-history or bulletin language where available.
   - The reviewer output should describe the actual rule change rather than summarize the entire document.

### Simulated prior version

Where a true historical PDF is not available, the prototype will use a **clearly labeled simulated prior version** only for demonstration.

The simulated prior will be grounded in an explicit UHC revision-history statement rather than invented from scratch. The preferred example is Sleep Studies because the current policy states both the previous and current wording for a coverage criterion.

The simulated artifact will be stored separately from real payer documents and clearly marked:
`SIMULATED PRIOR VERSION — generated only to demonstrate comparison behavior.`

A separate formatting-only fixture will be used to confirm that changes such as whitespace, line wrapping, or presentation do not create a substantive-change alert.

### Missing or conflicting dates

Date precedence is source-specific rather than based on whichever date is newest:
- explicit policy Effective Date → policy effective date
- formal policy-history date → revision date
- explicit `Policy published` history event → publication date
- UHC index `Last Published` → last-published observation
- official bulletin event → event-level effective date, resolved for configured geography when needed

If official sources conflict rather than describing different concepts, the system should surface the conflict for reviewer investigation instead of silently choosing one.

## 5. Pediatric, HOPD, and professional-billing relevance

Relevance will be evaluated using both structured codes and policy language.

### Pediatric relevance
Signals may include:
- explicit age thresholds
- child / adolescent / infant language
- pediatric procedures or clinical criteria
- pediatric disease or treatment context

A lack of pediatric-specific CPT codes does not make a policy irrelevant.

### HOPD relevance
Signals may include:
- hospital outpatient department
- hospital-based facility
- site-of-service restrictions
- outpatient facility claims
- UB-04 / institutional billing
- facility reimbursement requirements

### Professional-billing relevance
Signals may include:
- Professional policy designation
- CMS-1500 / professional claim language
- professional component / physician billing language
- explicit professional reimbursement rules

The system will distinguish **relevance** from **confidence** and allow `needs investigation` when applicability is plausible but uncertain.

## 6. Ambiguous / false-positive case

Current candidate: **Allergen Testing Policy, Professional and Facility**.

The policy is operationally relevant because it applies to professional and facility reimbursement, but its primary age threshold begins at 20 years. For a synthetic pediatric hospital profile that may include patients through age 21, applicability is narrow rather than clearly irrelevant or broadly pediatric.

Expected reviewer disposition:
`Needs investigation`

This is a useful example of why simple keyword or CPT matching is insufficient.

## 7. Reviewer experience

The final review queue should show, for each candidate change:
- policy title and payer
- plan / geography context
- whether the event is new or revised
- concise description of the actual rule change
- why it may matter to the synthetic pediatric hospital
- affected billing setting
- applicable effective date
- exact supporting passage and page
- source link
- uncertainty / applicability notes
- reviewer action: Relevant / Irrelevant / Needs investigation

The queue is intended to help a small policy-review team decide what deserves investigation, not to make autonomous coverage decisions.

## 8. Ember AI, confidentiality, and validation

Ember AI was not used in the current prototype.

The prototype uses only:
- public UnitedHealthcare policy material
- synthetic organizational context
- synthetic or clearly labeled simulated prior versions where needed

No patient data, Seattle Children’s claims, contracts, credentials, or other nonpublic information are included.

Any model-assisted interpretation added later will be constrained to public policy text, will return source-grounded structured output, and will require reviewer verification before an item is treated as actionable.

## 9. What I would build next for daily multi-payer monitoring

1. Scheduled discovery across payer index pages rather than a manually curated manifest.
2. Snapshot/version storage for each observed policy.
3. Robust section-aware diffing across historical versions.
4. Payer-specific adapters for index-page and bulletin formats.
5. Relevance scoring using a maintained synthetic/approved organizational profile.
6. Reviewer feedback capture to improve triage rules.
7. Monitoring for failed retrievals, parser drift, and source-layout changes.
8. Audit trail linking every queue item to the exact source version and evidence passage.

## 10. Current implementation status

- [x] Milestone 1 — deterministic collection and provenance
- [ ] Milestone 2 — change detection and simulated prior comparison
- [ ] Milestone 3 — pediatric / HOPD / professional relevance assessment
- [ ] Milestone 4 — evidence-backed reviewer queue
- [ ] Milestone 5 — impact framing and final submission polish

## 11. Known gaps / engineering tradeoffs

- The prototype monitors a small curated UHC source set rather than attempting comprehensive payer coverage.
- Historical PDFs are not guaranteed to remain publicly downloadable; when unavailable, simulated priors are clearly labeled and grounded in payer-authored revision history.
- UHC templates vary across medical, drug, reimbursement, and bulletin documents, so extraction is conservative and leaves unsupported fields null.
- Applicability is evaluated against a synthetic Seattle pediatric-hospital profile, not Seattle Children’s actual contracts, claims, or plan mix.
- The prototype prioritizes traceability and reviewer usefulness over production-scale crawling infrastructure.
