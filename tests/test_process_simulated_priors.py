from process_simulated_priors import SIMULATED_PRIORS, process_one


def test_all_three_priors_process():
    documents = [process_one(entry) for entry in SIMULATED_PRIORS]
    assert len(documents) == 3
    assert {d.document_id for d in documents} == {"mri_ct_prior", "spinraza_prior", "sleep_studies_prior"}


def test_policy_number_and_effective_date_extracted():
    for entry in SIMULATED_PRIORS:
        document = process_one(entry)
        assert document.policy_number is not None
        assert document.effective_date is not None


def test_is_simulated_flagged():
    for entry in SIMULATED_PRIORS:
        document = process_one(entry)
        assert document.is_simulated is True
        assert document.artifact_type == "simulated_prior"


def test_page_text_preserved():
    for entry in SIMULATED_PRIORS:
        document = process_one(entry)
        assert len(document.pages) > 0
        assert document.pages[0].page_number == 1
        assert all(page.text.strip() for page in document.pages)


def test_no_fake_source_url_created():
    for entry in SIMULATED_PRIORS:
        document = process_one(entry)
        assert document.source_url is None
        assert document.retrieved_at is None
        assert document.source_path is not None
