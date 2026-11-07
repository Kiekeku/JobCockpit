from jobcockpit.db.models import EvidenceItem, JobPosting
from jobcockpit.documents.generator import build_resume_markdown, select_evidence_for_job


def _job(role: dict) -> JobPosting:
    return JobPosting(
        id="job_capabilities",
        source_type="paste",
        posting_snapshot_hash="hash_capabilities",
        raw_content_artifact_id="artifact_raw",
        company_name="Northwind Capital",
        normalized_title="financial_analyst_intern",
        role_type="internship",
        source={},
        employer={"company_name": "Northwind Capital"},
        role={"job_title": "Financial Analyst Intern", **role},
        employment_timing={},
        location={},
        eligibility={},
        compensation={},
        application={},
        fit_analysis={},
        data_quality={},
    )


def _evidence(evidence_id: str, claim: str, skills: list[str]) -> EvidenceItem:
    return EvidenceItem(
        evidence_id=evidence_id,
        candidate_id="candidate_local",
        source_type="resume_upload",
        claim=claim,
        skills=skills,
        allowed_for_resume=True,
        allowed_for_cover_letter=True,
    )


def test_document_evidence_ranking_prefers_role_capabilities_before_tech_stack():
    job = _job({"capabilities": ["Financial Modeling"], "tech_stack": ["Python"]})
    python_evidence = _evidence("evidence_python", "Built Python tools", ["Python"])
    finance_evidence = _evidence("evidence_finance", "Built financial models", ["Financial Modeling"])

    ranked = select_evidence_for_job(job, [python_evidence, finance_evidence], limit=2)

    assert [item.evidence_id for item in ranked] == ["evidence_finance", "evidence_python"]


def test_document_evidence_ranking_falls_back_to_tech_stack_for_existing_jobs():
    job = _job({"tech_stack": ["Python"]})
    finance_evidence = _evidence("evidence_finance", "Built financial models", ["Financial Modeling"])
    python_evidence = _evidence("evidence_python", "Built Python tools", ["Python"])

    ranked = select_evidence_for_job(job, [finance_evidence, python_evidence], limit=2)

    assert [item.evidence_id for item in ranked] == ["evidence_python", "evidence_finance"]


def test_document_evidence_ranking_prioritizes_required_then_preferred_then_responsibility():
    job = _job(
        {
            "required_capabilities": ["SQL"],
            "preferred_capabilities": ["Tableau"],
            "responsibility_capabilities": ["Python"],
            "capabilities": ["SQL", "Tableau", "Python"],
        }
    )
    responsibility = _evidence("evidence_python", "Built Python tools", ["Python"])
    preferred = _evidence("evidence_tableau", "Built Tableau dashboards", ["Tableau"])
    required = _evidence("evidence_sql", "Built SQL models", ["SQL"])

    ranked = select_evidence_for_job(
        job,
        [responsibility, preferred, required],
        limit=3,
    )

    assert [item.evidence_id for item in ranked] == [
        "evidence_sql",
        "evidence_tableau",
        "evidence_python",
    ]


def test_resume_placeholder_uses_neutral_target_requirements_wording():
    job = _job({"capabilities": ["Financial Modeling", "Budget Forecasting"]})

    markdown = build_resume_markdown(job)

    assert "target requirements" in markdown
    assert "Financial Modeling, Budget Forecasting" in markdown
    assert "tech stack" not in markdown.lower()
