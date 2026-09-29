# Simulated prior PDFs

These PDFs are **not original archived UnitedHealthcare documents**. Each is built from the corresponding current public UHC PDF and is clearly labeled on page 1 as a simulated prior.

The policy body was reverted only where the current payer-authored Policy History/Revision Information explicitly supports the prior state. The current revision-history section is intentionally retained verbatim for provenance.

Two controlled test conditions were also added:

- MRI/CT contains one **unlisted substantive pediatric/HOPD change** (age threshold 16 -> 18 in the current version) to test whether change detection can find an important change not declared in revision history.
- Spinraza and Sleep Studies each contain a **semantically neutral wording-only change** to test whether the system can avoid overcalling non-substantive wording edits.

See `ground_truth.json` for the expected changes. Do not expose the ground-truth file to any model/classifier being evaluated if you want a blind test.
