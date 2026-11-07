from bs4 import BeautifulSoup
from jobcockpit.ingestion.ats_handlers.apply import _description_text
from jobcockpit.ingestion.parser import parse_job_text

# Minimized from observed HTML list/strong structure; private archive fingerprint:
# fe585658f9a303afc15cc832e33d4eeaa9a46f6347ee38505227291d0bac46ca
ENROLLMENT_HTML = (
    "<ul><li>Currently enrolled in a Computer Science or equivalent degree program</li>"
    "<li><strong>Currently enrolled in a post-secondary program and returning to studies "
    "after the internship</strong></li></ul>"
)


def test_apply_description_preserves_list_boundaries_and_inline_emphasis():
    soup = BeautifulSoup("<main>" + ENROLLMENT_HTML + "</main>", "html.parser")
    text = _description_text(soup, {})
    assert text.splitlines() == [
        "Currently enrolled in a Computer Science or equivalent degree program",
        "Currently enrolled in a post-secondary program and returning to studies after the internship",
    ]
    parsed = parse_job_text("Title: Software Intern\nQualifications:\n" + text)
    assert parsed.eligibility.current_enrollment_required is True


def test_structured_and_visible_duplicate_descriptions_share_layout():
    soup = BeautifulSoup("<main>" + ENROLLMENT_HTML + "</main>", "html.parser")
    text = _description_text(soup, {"description": ENROLLMENT_HTML})
    assert len(text.splitlines()) == 2
