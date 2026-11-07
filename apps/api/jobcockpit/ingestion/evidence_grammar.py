from __future__ import annotations

import re
from dataclasses import dataclass

from jobcockpit.ingestion.posting_document import document_for_text
from jobcockpit.ingestion.timing_clauses import _timing_clause_for_span


@dataclass(frozen=True)
class EvidenceMention:
    """A fact mention together with the grammatical clause that owns it."""

    clause: str
    start: int
    end: int

    @classmethod
    def from_scoped_posting(cls, posting_text: str, start: int, end: int) -> EvidenceMention:
        """Bind an embedded fact to its parenthesis before considering its matrix."""
        owner = document_for_text(posting_text).enclosing_parenthetical(start, end)
        if owner is not None:
            return cls(owner.text, start - owner.start, end - owner.start)
        return cls.from_posting(posting_text, start, end)

    @classmethod
    def from_posting(cls, posting_text: str, start: int, end: int) -> EvidenceMention:
        resolved = _timing_clause_for_span(posting_text, start, end)
        return cls(
            clause=resolved.text,
            start=start - resolved.start,
            end=end - resolved.start,
        )

    @property
    def prefix(self) -> str:
        return self.clause[: self.start]

    @property
    def suffix(self) -> str:
        return self.clause[self.end :]

    def is_negated_or_nonmandatory(self) -> bool:
        prefix = self.prefix[-100:]
        suffix = self.suffix[:100]
        if re.search(r"\bnot\s+only\s*$", prefix, flags=re.IGNORECASE):
            return False
        return bool(
            re.search(
                r"\b(?:do(?:es)?\s+not\s+need\s+to\s+be|need\s+not\s+be|"
                r"not\s+required\s+to\s+be|without\s+being|not)\s*$",
                prefix,
                flags=re.IGNORECASE,
            )
            or self._suffix_has_attached_nonmandatory_predicate(suffix)
        )

    @staticmethod
    def _suffix_has_attached_nonmandatory_predicate(suffix: str) -> bool:
        # Punctuation, a nested parenthetical, or a new coordinated predicate
        # ends the phrase governed by this mention. This prevents a preference
        # on a degree level or later skill from changing enrollment polarity.
        attached_phrase = re.split(
            r"[,;.!?\r\n(]|\b(?:and|while|whereas)\b",
            suffix,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0]
        return bool(
            re.search(
                r"\b(?:is|are)?\s*(?:optional|preferred|not\s+required|"
                r"not\s+needed|not\s+necessary|not\s+eligible)\b",
                attached_phrase,
                flags=re.IGNORECASE,
            )
        )

    def has_coordinated_alternative(
        self,
        alternative_pattern: str,
        *,
        direction: str = "either",
    ) -> bool:
        if direction not in {"either", "before", "after"}:
            raise ValueError(f"Unsupported coordination direction: {direction}")
        for alternative in re.finditer(
            rf"\b(?:{alternative_pattern})\b",
            self.clause,
            flags=re.IGNORECASE,
        ):
            if alternative.end() <= self.start:
                if direction == "after":
                    continue
                between = self.clause[alternative.end() : self.start]
            elif self.end <= alternative.start():
                if direction == "before":
                    continue
                between = self.clause[self.end : alternative.start()]
            else:
                continue
            if not re.search(r"\bor\b", between, flags=re.IGNORECASE):
                continue
            normalized_between = re.sub(
                r",\s*(?=or\b)|(?<=\bor),\s*",
                " ",
                between,
                flags=re.IGNORECASE,
            )
            # An alternative owns this mention only inside one coordinated
            # phrase. A later "and must be ..." starts a separate requirement;
            # punctuation and line boundaries likewise close the alternative.
            if re.search(
                r"\band\b|[,;:.]|\r|\n",
                normalized_between,
                flags=re.IGNORECASE,
            ):
                continue
            if re.search(
                r"\b(?:must|required|needs?|shall|should)\b",
                normalized_between,
                flags=re.IGNORECASE,
            ):
                continue
            return True
        return False
