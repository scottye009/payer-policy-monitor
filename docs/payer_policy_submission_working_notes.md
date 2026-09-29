# Submission Notes

## Policies selected

I focused on UnitedHealthcare Commercial policies. I collected five public documents: MRI and CT Scan – Site of Service, Spinraza, Sleep Studies, Allergen Testing Policy, and the August 2026 Commercial Reimbursement Policy Update Bulletin.

For the prior/current comparison, I used MRI/CT, Spinraza, and Sleep Studies. All three referenced prior policy versions in their revision history, but I could not reliably find those older PDFs on the live UHC site. Because the assignment allowed a simulated prior when historical versions are unavailable, I created clearly labeled simulated prior versions for those three policies.

For the simulated priors, I mainly reverted changes explicitly described in UHC's revision history. I also intentionally added a substantive change that was not listed in the revision history, plus a few wording-only changes. The goal was to make sure the change detector was actually comparing the policy language rather than simply repeating the payer's own change summary.

The five source documents were:

- MRI and CT Scan – Site of Service:
  https://www.uhcprovider.com/content/dam/provider/docs/public/policies/comm-medical-drug/mri-ct-scan-site-of-service.pdf
- Spinraza (Nusinersen):
  https://www.uhcprovider.com/content/dam/provider/docs/public/policies/comm-medical-drug/spinraza-nusinersen.pdf
- Sleep Studies:
  https://www.uhcprovider.com/content/dam/provider/docs/public/policies/comm-medical-drug/sleep-studies.pdf
- Allergen Testing Policy, Professional and Facility:
  https://www.uhcprovider.com/content/dam/provider/docs/public/policies/comm-reimbursement/COMM-Allergen-Testing-Policy.pdf
- August 2026 Commercial Reimbursement Policy Update Bulletin:
  https://www.uhcprovider.com/content/dam/provider/docs/public/policies/comm-reimbursement/rpub/UHC-COMM-RPUB-August-2026.pdf

## How I check for updates and detect changes

I thought of this as two separate problems: first, how do I know a payer published something new, and second, what actually changed in the policy?

For the first part, the collection process records the source URL, retrieval time, policy number, PDF hash, and dates available from both the PDF and the UHC policy index. In a daily version of this system, I would regularly poll those payer index pages. If the site's publication/version information changes, I would download the document and compare its hash/version with the one already stored. If it is new, that would trigger the comparison pipeline.

For the second part, I first use a deterministic text diff so I do not miss differences just because they look semantically similar. I then use MPNet to help align corresponding passages between versions. Qwen is used after that to decide whether the difference is actually substantive, non-substantive, or uncertain, and to extract a few structured fields that I need later for relevance.

I use the payer's Policy History as supporting evidence, but not as the source of truth. This mattered in the MRI example because the simulated prior contains an age change from under 16 to under 18 that I intentionally left out of the revision history. The detector still found it.

I also keep the exact before/after passage, page, section, source URL, and dates with every result. The model is interpreting the source text, not generating the evidence itself.

## Dates

Dates were a little more complicated than I expected because UHC exposes several different concepts. There can be an effective date inside the PDF, a revision or publication date, a `Last Published` date on the payer's website, and the time when I retrieved the document.

I decided not to collapse those into a single date because they mean different things. If a date is missing, I leave it missing rather than guessing it from another field. If a date comes from another official source, such as the reimbursement bulletin, I keep that provenance with it.

One example was the Allergen Testing policy, where the policy PDF itself did not
provide the effective date I needed, but the official UHC reimbursement bulletin
did. I kept that date with its bulletin provenance rather than treating it as if
it came from the policy PDF.

In a production monitor, I would probably also flag unusual disagreements between these date fields and automatically re-check the source.

## How I determine relevance

For this part, I created a small synthetic pediatric hospital profile with Washington geography, pediatric ages, hospital outpatient, facility and professional billing, UHC plan scope, and several relevant service areas.

For each substantive change, I use the most specific information available. If there is a CPT or HCPCS code, I use that. If there is an age threshold, state, plan, or billing-setting change, I compare that directly with the hospital profile. When there is no useful code, I use the policy language and the service area instead. This is important for things like Spinraza authorization criteria or Sleep Studies medical-necessity changes, which can matter even when there is no new CPT code.

The output is `relevant`, `irrelevant`, or `needs_investigation`. I kept the third option because there are cases where the policy alone is not enough to know whether a rule applies to a particular contract or benefit plan.

One example is the MRI Individual Exchange change that added Illinois to an exclusion list. The change itself is substantive, but it is irrelevant to the synthetic Washington hospital. Earlier in development I actually sent this to `needs_investigation` because I had captured the state but not the fact that it was an exclusion. I corrected the relevance logic after reviewing the exact passage.

## Impact

I used synthetic annual claim volumes for quantifying impact. The goal was to estimate the population that could potentially be affected without pretending the number was more precise than the available data.

For example, when the MRI age threshold changes from under 16 to under 18, I count only the synthetic volume for ages 16–17 because that is the newly affected group. For the addition of CPT 70471, I do not have code-specific synthetic volume, so I return no estimate rather than using all imaging claims as a substitute.

For broader clinical changes, such as Sleep Studies criteria, the claim data cannot tell me which patients actually meet the new clinical requirement. In those cases I use the matching service volume only as an upper-bound proxy and say what additional information would be needed for a better estimate.

## QC and issues I encountered

I created a small hand-built ground truth from the simulated priors to check the detector. Across the three policies, all 9 changes explicitly described in the payer's revision history were found and classified as substantive. The additional MRI age change that was intentionally not listed in the revision history was also found.

I also noticed that classification recall (9/9) and history corroboration are not the same thing: only 4 of those 9 changes actually came back with `revision_history_match: true`. Sleep Studies is the main culprit — it groups several separate changes under one large history entry, so the text-overlap check gets diluted even when the semantic match is strong. Classification still works fine without that corroboration, but it means the "Policy History confirms this" citation is missing on cases where it should be there. I haven't fixed the matching logic yet, just noted it.

One other issue I ran into was PDF extraction. The simulated PDFs did not always store text objects in natural reading order, so some extracted text was being combined incorrectly and producing misleading differences. PDF processing was new to me, so I spent some time tracing where the problem was actually coming from. I eventually fixed it upstream by using position-sorted text extraction rather than trying to compensate for it with the LLM.

There is still one known false positive in the Spinraza example that appears to come from malformed text in the simulated prior. I left it visible instead of tuning the detector specifically around that fixture, since a human reviewer can see the evidence and decide it is not meaningful.

## AI use and confidentiality

I did not use Ember AI.

I designed the data model, schemas, relevance and impact logic, evaluation setup, and prompts. Claude Code was used as a coding assistant for implementation, debugging, testing, code review, and repository cleanup. GPT helped generate the simulated prior PDFs based on the changes I specified. Qwen3-235B-A22B-Instruct is the model used by the actual change-detection pipeline.

Everything used in the prototype is either public or synthetic: public UHC policies, simulated prior policies, a synthetic hospital profile, and synthetic claim volumes. I also do not treat model output as the final answer. The app keeps the exact source passages available for review, and the final relevance decision is still made by a human reviewer.

## What I would build next

The next step would be to make the collection process continuous and support more payers. I would create lightweight source adapters for UHC, Premera, Regence, and other payer sites, monitor them on a schedule, and store each real policy version as it appears.

A new or updated policy would automatically trigger the same comparison pipeline. The synthetic hospital profile would eventually be replaced with the organization's real payer products, service lines, sites of care, and billing settings, and synthetic claim volumes could be replaced by actual utilization data.

I would keep one shared review queue across payers rather than separate workflows for each payer. New changes could then be prioritized based on relevance, effective date, potential exposure, and uncertainty. I would also add notifications for newly prioritized items so the
reviewer does not need to keep checking the dashboard, while keeping the app as
the place where evidence and final decisions are recorded.

Before production use, I would build a much larger evaluation set using real historical policy pairs across multiple payers and use reviewer decisions to continuously measure and improve the change-detection and relevance logic.
