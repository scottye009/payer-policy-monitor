from review_processing import assess_relevance, estimate_claim_impact, process_review_queue

PROFILE = {
    "geography": {"state": "WA"},
    "payer_scope": {"payer": "UnitedHealthcare", "plans": ["commercial", "individual_exchange"]},
    "population": {"age_min": 0, "age_max": 21},
    "billing_settings": ["hospital_outpatient", "inpatient", "facility", "professional", "pharmacy"],
    "service_areas": ["imaging", "sleep", "neurology", "pharmacy_drug", "laboratory"],
}

CLAIM_ROWS = [
    {"payer": "UnitedHealthcare", "plan_scope": "commercial", "service_area": "imaging",
     "billing_setting": "hospital_outpatient", "age_min": 0, "age_max": 15, "code": None,
     "service_description": "MRI/CT under 16", "annual_claim_lines": 6200},
    {"payer": "UnitedHealthcare", "plan_scope": "commercial", "service_area": "imaging",
     "billing_setting": "hospital_outpatient", "age_min": 16, "age_max": 17, "code": None,
     "service_description": "MRI/CT 16-17", "annual_claim_lines": 850},
    {"payer": "UnitedHealthcare", "plan_scope": "commercial", "service_area": "imaging",
     "billing_setting": "hospital_outpatient", "age_min": 18, "age_max": 21, "code": None,
     "service_description": "MRI/CT 18-21", "annual_claim_lines": 520},
    {"payer": "UnitedHealthcare", "plan_scope": "commercial", "service_area": "neurology",
     "billing_setting": "hospital_outpatient", "age_min": 0, "age_max": 17, "code": "J2326",
     "service_description": "Spinraza administration", "annual_claim_lines": 48},
]


def _record(**overrides):
    base = {
        "classification": "substantive",
        "service_area": "imaging",
        "billing_setting": "hospital_outpatient",
        "age_min": None,
        "age_max": None,
        "codes": [],
        "states": [],
        "plan_scope": [],
        "before_text": None,
        "after_text": None,
    }
    base.update(overrides)
    return base


def test_clear_match_is_relevant():
    relevance, reason = assess_relevance(_record(), PROFILE)
    assert relevance == "relevant"
    assert reason


def test_service_area_mismatch_is_irrelevant():
    relevance, _ = assess_relevance(_record(service_area="surgery"), PROFILE)
    assert relevance == "irrelevant"


def test_named_states_without_explicit_polarity_trigger_needs_investigation():
    relevance, reason = assess_relevance(_record(states=["IL"]), PROFILE)
    assert relevance == "needs_investigation"
    assert "IL" in reason
    # why_it_may_matter is never consulted -- only structured fields feed the decision.
    assert "why_it_may_matter" not in reason


def test_explicit_exclusion_not_naming_hospital_state_is_irrelevant():
    after_text = (
        "This Medical Policy applies to Individual Exchange benefit plans in all states, "
        "except for Illinois, Maryland, Massachusetts, Texas, and Wisconsin."
    )
    relevance, reason = assess_relevance(
        _record(states=["IL"], after_text=after_text, plan_scope=["individual_exchange"]), PROFILE
    )
    assert relevance == "irrelevant"
    assert "WA" in reason


def test_explicit_exclusion_naming_hospital_state_is_relevant():
    after_text = "This Medical Policy applies to all states, except for Washington and Oregon."
    relevance, reason = assess_relevance(_record(states=["WA", "OR"], after_text=after_text), PROFILE)
    assert relevance == "relevant"
    assert "WA" in reason


def test_age_range_outside_pediatric_population_is_irrelevant():
    relevance, _ = assess_relevance(_record(age_min=22, age_max=40), PROFILE)
    assert relevance == "irrelevant"


def test_age_threshold_delta_matches_only_newly_affected_band():
    record = _record(
        age_max=17,
        before_text="Coverage is limited to patients under 16 years of age.",
        after_text="Coverage is limited to patients under 18 years of age.",
    )
    lines, note = estimate_claim_impact(record, CLAIM_ROWS)
    # Only the 16-17 band (850), not the full 0-17 resulting range (6200+850).
    assert lines == 850
    assert "16-17" in note
    assert "newly added/removed" in note


def test_explicit_code_without_matching_row_does_not_fall_back_to_area_volume():
    record = _record(codes=["70471"])
    lines, note = estimate_claim_impact(record, CLAIM_ROWS)
    assert lines is None
    assert "code-specific volume is unavailable" in note


def test_explicit_plan_scope_without_matching_row_does_not_fall_back_to_commercial():
    record = _record(plan_scope=["individual_exchange"])
    lines, note = estimate_claim_impact(record, CLAIM_ROWS)
    assert lines is None
    assert "not used as a substitute" in note


def test_service_area_only_proxy_is_explicitly_labeled_upper_bound():
    record = _record(service_area="neurology")
    lines, note = estimate_claim_impact(record, CLAIM_ROWS)
    assert lines == 48
    assert "Upper-bound proxy" in note


def test_no_claim_rows_for_service_area_reports_no_match():
    lines, note = estimate_claim_impact(_record(service_area="laboratory"), CLAIM_ROWS)
    assert lines is None
    assert "No synthetic claim-volume rows matched" in note


def test_non_substantive_records_get_null_new_fields_and_are_not_processed():
    records = [
        {"change_id": "a", "classification": "non_substantive", "service_area": "imaging",
         "billing_setting": "hospital_outpatient", "age_min": None, "age_max": None, "codes": [],
         "states": [], "plan_scope": [], "before_text": None, "after_text": None},
    ]
    processed = process_review_queue(records, PROFILE, CLAIM_ROWS)
    assert processed[0]["relevance"] is None
    assert processed[0]["relevance_reason"] is None
    assert processed[0]["potential_annual_claim_lines"] is None
    assert processed[0]["impact_note"] is None


def test_irrelevant_records_are_not_given_a_claim_impact_estimate():
    records = [_record(service_area="surgery") | {"change_id": "b"}]
    processed = process_review_queue(records, PROFILE, CLAIM_ROWS)
    assert processed[0]["relevance"] == "irrelevant"
    assert processed[0]["potential_annual_claim_lines"] is None
