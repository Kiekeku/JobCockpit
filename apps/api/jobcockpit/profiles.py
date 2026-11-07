from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
import re

from docx import Document
from pypdf import PdfReader


MAX_RESUME_UPLOAD_BYTES = 5 * 1024 * 1024
SUPPORTED_RESUME_EXTENSIONS = {".md", ".txt", ".docx", ".pdf"}
CLAIM_START_VERBS = {
    "achieved",
    "administered",
    "analyzed",
    "architected",
    "automated",
    "built",
    "building",
    "collaborated",
    "configured",
    "containerized",
    "coordinated",
    "created",
    "delivered",
    "deployed",
    "designed",
    "developed",
    "documented",
    "drove",
    "engineered",
    "enabled",
    "established",
    "experimented",
    "facilitated",
    "implemented",
    "improved",
    "integrated",
    "launched",
    "led",
    "managed",
    "maintained",
    "migrated",
    "modernized",
    "optimized",
    "operated",
    "owned",
    "partnered",
    "provided",
    "refactored",
    "reduced",
    "resolved",
    "shipped",
    "streamlined",
    "supported",
    "tested",
    "trained",
    "translated",
    "troubleshot",
    "used",
}
NOISE_SECTION_TITLES = {
    "education",
    "experience",
    "projects",
    "extracurriculars",
    "technical skills",
    "skills",
}
SKILL_LINE_PREFIXES = (
    "programming:",
    "databases & tools:",
    "frameworks & libraries:",
    "productivity:",
)


@dataclass(frozen=True)
class ParsedResume:
    parse_status: str
    text: str
    warnings: list[str]


def parse_resume_bytes(filename: str, content: bytes) -> ParsedResume:
    extension = Path(filename).suffix.lower()
    try:
        if extension in {".md", ".txt"}:
            text = content.decode("utf-8")
        elif extension == ".docx":
            text = _parse_docx(content)
        elif extension == ".pdf":
            text = _parse_pdf(content)
        else:
            return ParsedResume("unsupported", "", ["unsupported_file_type"])
    except Exception:
        return ParsedResume("stored_unparsed", "", ["parse_failed"])

    normalized = "\n".join(line.strip() for line in text.splitlines() if line.strip())
    if not normalized:
        return ParsedResume("stored_unparsed", "", ["no_text_extracted"])
    return ParsedResume("parsed", normalized, [])


def extract_resume_evidence(text: str, profile_skills: list[str]) -> list[dict]:
    claims: list[dict] = []
    seen: set[str] = set()
    current_claim_parts: list[str] = []

    def flush_claim() -> None:
        if not current_claim_parts:
            return
        claim = _clean_claim(" ".join(current_claim_parts))
        current_claim_parts.clear()
        if not claim:
            return
        key = claim.lower()
        if key in seen:
            return
        seen.add(key)
        claims.append({"claim": claim, "skills": _matched_skills(claim, profile_skills)})

    for raw_line in text.splitlines():
        line = _clean_resume_line(raw_line)
        if not line:
            continue
        if _is_resume_noise(line):
            flush_claim()
            continue
        if _starts_evidence_claim(line):
            flush_claim()
            current_claim_parts.append(line)
            continue
        if current_claim_parts:
            if _claim_parts_complete(current_claim_parts):
                flush_claim()
                continue
            current_claim_parts.append(line)
    flush_claim()
    return claims


def _parse_docx(content: bytes) -> str:
    document = Document(BytesIO(content))
    return "\n".join(paragraph.text for paragraph in document.paragraphs)


def _parse_pdf(content: bytes) -> str:
    reader = PdfReader(BytesIO(content))
    if reader.is_encrypted:
        try:
            decrypt_result = reader.decrypt("")
        except Exception:
            decrypt_result = 0
        if decrypt_result == 0:
            return ""
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def _clean_claim(line: str) -> str | None:
    claim = re.sub(r"^[\s\-*•]+", "", line).strip()
    claim = re.sub(r"\s+", " ", claim)
    if len(claim) < 6:
        return None
    return claim


def _clean_resume_line(line: str) -> str:
    line = re.sub(r"^[\s\-*•]+", "", line).strip()
    line = line.replace("$\\sim$", "~")
    line = re.sub(r"`?<!--\s*-->\s*`?", "", line)
    line = re.sub(r"\{=[^}]+\}", "", line)
    line = re.sub(r"\{\\?\\.?(?:smallcaps|underline)\}", "", line)
    line = line.replace("**", "").replace("*", "").replace("\\", "")
    return re.sub(r"\s+", " ", line).strip()


def _is_resume_noise(line: str) -> bool:
    normalized = line.strip("# ").strip().lower()
    if not normalized:
        return True
    if line.startswith(":::"):
        return True
    if normalized in NOISE_SECTION_TITLES:
        return True
    if re.fullmatch(r"[-\s]{3,}", line):
        return True
    if any(marker in normalized for marker in ("mailto:", "linkedin.com", "github.com")):
        return True
    if re.fullmatch(r"\+?\d[\d\s().|$-]{7,}", line):
        return True
    return normalized.startswith(SKILL_LINE_PREFIXES)


def _starts_evidence_claim(line: str) -> bool:
    first_word = re.match(r"[A-Za-z]+", line)
    if not first_word:
        return False
    return first_word.group(0).lower() in CLAIM_START_VERBS


def _claim_parts_complete(parts: list[str]) -> bool:
    return " ".join(parts).rstrip().endswith((".", "!", "?"))


def _matched_skills(claim: str, profile_skills: list[str]) -> list[str]:
    haystack = claim.lower()
    return [skill for skill in profile_skills if skill.lower() in haystack]
