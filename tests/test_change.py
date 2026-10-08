from payer_policy.adjudicate import parse_llm_response
from payer_policy.change import detect_changes
from payer_policy.models import LLMAdjudication, PageText, PolicyDocument


def _doc(document_id: str, text: str) -> PolicyDocument:
    return PolicyDocument(
        document_id=document_id,
        title="Test Policy",
        payer="UnitedHealthcare",
        document_type="medical_policy",
        content_sha256="0" * 64,
        local_path=f"data/processed/{document_id}.json",
        pages=[PageText(page_number=1, text=text)],
    )


def _fake_adjudicate(before_text, after_text, section, revision_history_evidence, **kwargs):
    return LLMAdjudication(
        classification="uncertain", summary="test", reason="test", changed_dimensions=[], confidence=0.5
    )


def _fail_adjudicate(**kwargs):
    raise AssertionError("adjudicate should not be called when there are no candidates")


def test_whitespace_and_line_wrap_changes_are_ignored():
    prior = _doc("prior", "Coverage Rationale\n\nCoverage is limited to patients under\n16 years of  age.\n")
    current = _doc("current", "Coverage Rationale\n\nCoverage is limited to patients under 16\nyears of age.\n")

    records = detect_changes(prior, current, adjudicate_fn=_fail_adjudicate)

    assert records == []


def test_numeric_age_change_remains_a_candidate():
    prior = _doc("prior", "Coverage Rationale\n\nCoverage is limited to patients under 16 years of age.\n")
    current = _doc("current", "Coverage Rationale\n\nCoverage is limited to patients under 18 years of age.\n")

    records = detect_changes(prior, current, adjudicate_fn=_fake_adjudicate)

    assert len(records) == 1
    assert records[0].change_type == "modified"
    assert "16" in records[0].before_text
    assert "18" in records[0].after_text


def test_additions_and_removals_are_detected():
    prior = _doc(
        "prior",
        "Coverage Rationale\n\n"
        "Paragraph Alpha describes eligibility criteria for members.\n\n"
        "Paragraph Beta describes an authorization step that will be deleted.\n\n"
        "Paragraph Gamma describes documentation requirements for reviewers.\n",
    )
    current = _doc(
        "current",
        "Coverage Rationale\n\n"
        "Paragraph Alpha describes eligibility criteria for members.\n\n"
        "Paragraph Gamma describes documentation requirements for reviewers.\n\n"
        "Paragraph Delta is a brand new addition describing a site of service rule.\n",
    )

    records = detect_changes(prior, current, adjudicate_fn=_fake_adjudicate)

    change_types = {r.change_type for r in records}
    assert change_types == {"removed", "added"}
    removed = next(r for r in records if r.change_type == "removed")
    added = next(r for r in records if r.change_type == "added")
    assert "Beta" in removed.before_text
    assert added.before_text is None
    assert "Delta" in added.after_text
    assert removed.after_text is None


def test_rewritten_and_reordered_passages_are_still_aligned():
    prior = _doc(
        "prior",
        "Coverage Rationale\n\n"
        "Coverage requires prior authorization for adult patients.\n\n"
        "Imaging is not covered for cosmetic purposes.\n",
    )
    current = _doc(
        "current",
        "Coverage Rationale\n\n"
        "Cosmetic imaging remains excluded from coverage.\n\n"
        "Adult patients must obtain prior authorization before imaging.\n",
    )

    records = detect_changes(prior, current, adjudicate_fn=_fake_adjudicate)

    assert len(records) == 2
    assert all(r.change_type == "modified" for r in records)
    by_before = {r.before_text.strip(): r.after_text.strip() for r in records}
    assert by_before["Coverage requires prior authorization for adult patients."] == (
        "Adult patients must obtain prior authorization before imaging."
    )
    assert by_before["Imaging is not covered for cosmetic purposes."] == (
        "Cosmetic imaging remains excluded from coverage."
    )


def test_high_semantic_similarity_does_not_suppress_exact_token_change():
    prior = _doc("prior", "Coverage Rationale\n\nCoverage is limited to patients under 16 years of age.\n")
    current = _doc("current", "Coverage Rationale\n\nCoverage is limited to patients under 18 years of age.\n")

    records = detect_changes(prior, current, adjudicate_fn=_fake_adjudicate)

    assert len(records) == 1
    # These two sentences differ only in one number, so semantic similarity
    # is high -- but the candidate must still surface rather than being
    # dropped for looking "the same".
    assert records[0].semantic_similarity > 0.85
    assert records[0].before_text != records[0].after_text


def test_unmentioned_change_still_surfaces_with_no_history_match():
    prior = _doc(
        "prior",
        "Coverage Rationale\n\n"
        "Imaging requires prior authorization.\n\n"
        "Policy History/Revision Information\n\n"
        "Date\nSummary of Changes\n01/01/2025\nOld unrelated note\n\n"
        "Instructions for Use\n\nBoilerplate.\n",
    )
    current = _doc(
        "current",
        "Coverage Rationale\n\n"
        "Imaging no longer requires prior authorization.\n\n"
        "Policy History/Revision Information\n\n"
        "Date\nSummary of Changes\n09/01/2026\n"
        "Updated clinical evidence references only, unrelated to authorization.\n\n"
        "Instructions for Use\n\nBoilerplate.\n",
    )

    records = detect_changes(prior, current, adjudicate_fn=_fake_adjudicate)

    assert len(records) == 1
    assert records[0].revision_history_match is False
    assert records[0].revision_history_evidence is None


def test_policy_history_section_excluded_from_content_diff():
    prior = _doc(
        "prior",
        "Coverage Rationale\n\nSame body text unchanged.\n\n"
        "Policy History/Revision Information\n\n"
        "Date\nSummary of Changes\n01/01/2025\nOld note\n\n"
        "Instructions for Use\n\nBoilerplate.\n",
    )
    current = _doc(
        "current",
        "Coverage Rationale\n\nSame body text unchanged.\n\n"
        "Policy History/Revision Information\n\n"
        "Date\nSummary of Changes\n09/01/2026\nNew note about something\n\n"
        "Instructions for Use\n\nBoilerplate.\n",
    )

    # Only the history section differs between prior and current; if it
    # weren't excluded this would surface as a spurious candidate.
    records = detect_changes(prior, current, adjudicate_fn=_fail_adjudicate)

    assert records == []


def test_exact_before_after_evidence_is_preserved():
    prior = _doc("prior", "Coverage Rationale\n\nCoverage is limited to\npatients under 16\nyears of age.\n")
    current = _doc("current", "Coverage Rationale\n\nCoverage is limited to\npatients under 18\nyears of age.\n")

    records = detect_changes(prior, current, adjudicate_fn=_fake_adjudicate)

    assert len(records) == 1
    # Exact substrings from the page text, including original line wraps --
    # not the whitespace-normalized version used internally for diffing.
    assert records[0].before_text == "Coverage is limited to\npatients under 16\nyears of age.\n"
    assert records[0].after_text == "Coverage is limited to\npatients under 18\nyears of age.\n"


def test_enrichment_fields_propagate_from_adjudication_to_change_record():
    def _fake_adjudicate_with_enrichment(before_text, after_text, section, revision_history_evidence, **kwargs):
        return LLMAdjudication(
            classification="substantive",
            summary="test",
            reason="test",
            changed_dimensions=["age/numeric thresholds"],
            confidence=0.9,
            why_it_may_matter="Broadens pediatric eligibility.",
            billing_setting="hospital_outpatient",
            service_area="imaging",
            age_min=16,
            age_max=18,
            codes=["70471"],
            states=["IL"],
            plan_scope=["commercial"],
            additional_data_needed=["patient_age"],
        )

    prior = _doc("prior", "Coverage Rationale\n\nCoverage is limited to patients under 16 years of age.\n")
    current = _doc("current", "Coverage Rationale\n\nCoverage is limited to patients under 18 years of age.\n")

    records = detect_changes(prior, current, adjudicate_fn=_fake_adjudicate_with_enrichment)

    assert len(records) == 1
    record = records[0]
    assert record.why_it_may_matter == "Broadens pediatric eligibility."
    assert record.billing_setting == "hospital_outpatient"
    assert record.service_area == "imaging"
    assert record.age_min == 16
    assert record.age_max == 18
    assert record.codes == ["70471"]
    assert record.states == ["IL"]
    assert record.plan_scope == ["commercial"]
    assert record.additional_data_needed == ["patient_age"]


def test_malformed_llm_output_is_handled_explicitly():
    result = parse_llm_response("this is not json at all")

    assert isinstance(result, LLMAdjudication)
    assert result.classification == "uncertain"
    assert result.confidence == 0.0

    missing_fields = parse_llm_response('{"classification": "substantive"}')
    assert missing_fields.classification == "uncertain"
    assert missing_fields.confidence == 0.0


def test_out_of_vocabulary_enrichment_value_keeps_the_classification():
    # Real Qwen reply for a home-health punctuation change: valid JSON and a
    # correct classification, but billing_setting outside the allowed values.
    raw = (
        '{"classification": "non_substantive", "summary": "Period changed to a semicolon.", '
        '"reason": "Punctuation only.", "changed_dimensions": ["wording", "punctuation"], '
        '"confidence": 0.98, "why_it_may_matter": "", "billing_setting": "home_health", '
        '"service_area": "home_care", "age_min": null, "age_max": null, "codes": [], '
        '"states": [], "plan_scope": [], "additional_data_needed": []}'
    )
    result = parse_llm_response(raw)

    assert result.classification == "non_substantive"
    assert result.confidence == 0.98
    assert result.billing_setting == "other"
    assert result.service_area == "other"


def test_in_vocabulary_enrichment_values_are_untouched():
    raw = (
        '{"classification": "substantive", "summary": "s", "reason": "r", "confidence": 0.9, '
        '"billing_setting": "hospital_outpatient", "service_area": "imaging"}'
    )
    result = parse_llm_response(raw)
    assert (result.billing_setting, result.service_area) == ("hospital_outpatient", "imaging")


def test_invalid_required_field_still_falls_back():
    # Coercion is limited to enrichment fields; a bad classification is still rejected.
    raw = '{"classification": "maybe", "summary": "s", "reason": "r", "confidence": 0.9, "billing_setting": "x"}'
    assert parse_llm_response(raw).classification == "uncertain"
    assert parse_llm_response('["not", "an", "object"]').classification == "uncertain"
