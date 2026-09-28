# Payer Policy Review Prototype

Prototype for converting UnitedHealthcare policy updates into an
evidence-backed review queue for a synthetic pediatric hospital.

## Current scope

This prototype uses a small set of public UnitedHealthcare policy documents
to demonstrate deterministic collection, change detection, relevance
assessment, and reviewer triage.

No Seattle Children's claims, contracts, patient data, or other non-public
information are used.

## Setup

python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

## Collect documents

python scripts/collect.py

## Data provenance

Source documents are retrieved directly from official UnitedHealthcare URLs.
Each retrieval records:

- source URL
- UTC retrieval timestamp
- SHA-256 of downloaded content
- available policy/effective/revision dates
- page-level extracted text
