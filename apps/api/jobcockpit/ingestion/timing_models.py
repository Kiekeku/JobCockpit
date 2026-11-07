from __future__ import annotations

from dataclasses import dataclass

from jobcockpit.ingestion.parser_models import ParsedTiming
from jobcockpit.ingestion.timing_dates import _month_span_duration


@dataclass(frozen=True)
class ScheduleEvidenceResolution:
    positive_values: frozenset[str] = frozenset()
    negative_values: frozenset[str] = frozenset()

    @property
    def has_evidence(self) -> bool:
        return bool(self.positive_values or self.negative_values)

    @property
    def is_ambiguous(self) -> bool:
        return len(self.positive_values) > 1 or bool(self.positive_values & self.negative_values)

    @property
    def value(self) -> str | None:
        if self.is_ambiguous or len(self.positive_values) != 1:
            return None
        return next(iter(self.positive_values))

    def merged(
        self,
        other: "ScheduleEvidenceResolution",
    ) -> "ScheduleEvidenceResolution":
        return ScheduleEvidenceResolution(
            positive_values=self.positive_values | other.positive_values,
            negative_values=self.negative_values | other.negative_values,
        )


@dataclass(frozen=True)
class ParsedTimingCandidate:
    timing: ParsedTiming
    schedule_resolution: ScheduleEvidenceResolution = ScheduleEvidenceResolution()
    default_schedule_type: str | None = None
    alternative_duration: bool = False
    prefer_academic_term_label: bool = False
    prefer_start_date_term_label: bool = False
    duration_options: frozenset[int] = frozenset()
    ambiguous_fields: tuple[str, ...] = ()
    authoritative_date_range: bool = False
    authoritative_duration: bool = False


@dataclass(frozen=True)
class EmploymentTimingResolution:
    timing: ParsedTiming
    ambiguous_fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class PostingContentScope:
    text: str
    lines: tuple[str, ...]


@dataclass(frozen=True)
class EmploymentTimingContentScope:
    timing_text: str
    posted_date_text: str


@dataclass(frozen=True)
class TimingClause:
    text: str
    start: int
    end: int
    context_type: str = "unknown"


@dataclass(frozen=True)
class PostingFieldSpan:
    label: str
    value: str
    start: int
    end: int
    value_start: int
    value_end: int


@dataclass(frozen=True)
class EmploymentDateEvidence:
    kind: str
    value: str
    label: str
    offset: int
    raw_label: str | None = None


@dataclass(frozen=True)
class EmploymentDurationEvidence:
    months: int
    raw_label: str
    label: str
    offset: int


@dataclass(frozen=True)
class AcademicTermEvidence:
    term_key: str
    term_label: str
    year: int
    claim_group: int
    line: str
    start: int
    end: int
    is_title_authority: bool = False


@dataclass(frozen=True)
class AcademicTimingSignature:
    start_date: str
    end_date: str
    duration_months: int


@dataclass(frozen=True)
class ProviderDateClaim:
    start: int
    end: int
    text: str
    value: str
    role: str


@dataclass(frozen=True)
class ProviderTimingClaimResolution:
    timing: ParsedTiming
    date_claims: tuple[ProviderDateClaim, ...] = ()
    ambiguous_fields: tuple[str, ...] = ()

    def values_for_role(self, role: str) -> frozenset[str]:
        return frozenset(claim.value for claim in self.date_claims if claim.role == role)


@dataclass(frozen=True)
class EmploymentTimingEvidence:
    start_dates: tuple[EmploymentDateEvidence, ...]
    end_dates: tuple[EmploymentDateEvidence, ...]
    duration_terms: tuple[EmploymentDurationEvidence, ...]
    academic_terms: tuple[AcademicTermEvidence, ...]
    schedule_fields: ScheduleEvidenceResolution
    schedule_prose: ScheduleEvidenceResolution
    schedule_defaults: ScheduleEvidenceResolution
    semantic_ambiguous_fields: tuple[str, ...] = ()

    @property
    def start_date(self) -> str | None:
        return _single_employment_date(self.start_dates)

    @property
    def end_date(self) -> str | None:
        return _single_employment_date(self.end_dates)

    @property
    def has_start_dates(self) -> bool:
        return bool(self.start_dates)

    @property
    def has_end_dates(self) -> bool:
        return bool(self.end_dates)

    @property
    def duration_months(self) -> int | None:
        values = {item.months for item in self.duration_terms}
        return next(iter(values)) if len(values) == 1 else None

    @property
    def duration_raw_label(self) -> str | None:
        duration_months = self.duration_months
        if duration_months is None:
            return None
        return next(
            item.raw_label for item in self.duration_terms if item.months == duration_months
        )

    @property
    def has_duration_terms(self) -> bool:
        return bool(self.duration_terms)

    @property
    def has_timing_fields(self) -> bool:
        return bool(self.start_dates or self.end_dates or self.duration_terms)

    @property
    def ambiguous_fields(self) -> tuple[str, ...]:
        fields: list[str] = []
        if _employment_dates_conflict(self.start_dates):
            fields.append("employment_timing.start_date")
        if _employment_dates_conflict(self.end_dates):
            fields.append("employment_timing.end_date")

        duration_values = {item.months for item in self.duration_terms}
        if len(duration_values) > 1:
            fields.append("employment_timing.duration_months")
        elif self.start_date and self.end_date and self.duration_months is not None:
            if not _date_range_supports_duration(
                self.start_date,
                self.end_date,
                self.duration_months,
            ):
                fields.append("employment_timing.duration_months")
        for field in self.semantic_ambiguous_fields:
            if field not in fields:
                fields.append(field)
        return tuple(fields)


@dataclass(frozen=True)
class MonthRangeTimingCandidate:
    segment: str
    start: int
    end: int
    raw_label: str
    start_year: int
    start_month: int
    end_year: int
    end_month: int
    kind: str
    order: int
    start_day: int | None = None
    end_day: int | None = None
    supports_end_boundary: bool = False


@dataclass(frozen=True)
class MonthRangeTimingAssessment:
    candidate: MonthRangeTimingCandidate
    context_type: str
    confidence: int
    reason: str


@dataclass(frozen=True)
class MonthRangeContextParts:
    context: str
    prefix: str
    suffix: str
    label: str
    range_start_in_context: int


def _single_employment_date(
    evidence: tuple[EmploymentDateEvidence, ...],
) -> str | None:
    values = {item.value for item in evidence}
    return next(iter(values)) if len(values) == 1 else None


def _employment_dates_conflict(
    evidence: tuple[EmploymentDateEvidence, ...],
) -> bool:
    return len({item.value for item in evidence}) > 1


def _duration_months_from_date_range(start_date: str, end_date: str | None) -> int | None:
    if not end_date:
        return None
    start_year, start_month, _ = (int(part) for part in start_date.split("-"))
    end_year, end_month, _ = (int(part) for part in end_date.split("-"))
    duration = _month_span_duration(start_year, start_month, end_year, end_month)
    return duration if 1 <= duration <= 24 else None


def _date_range_supports_duration(
    start_date: str,
    end_date: str,
    duration_months: int,
) -> bool:
    if _duration_months_from_date_range(start_date, end_date) == duration_months:
        return True
    start_year, start_month, start_day = (int(part) for part in start_date.split("-"))
    end_year, end_month, end_day = (int(part) for part in end_date.split("-"))
    return (
        start_day == end_day
        and _month_span_duration(
            start_year,
            start_month,
            end_year,
            end_month,
            end_is_boundary=True,
        )
        == duration_months
    )
