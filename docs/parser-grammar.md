# Posting grammar and field resolution

The deterministic parser combines domain grammar with lexical recognizers. Regex
is appropriate for recognizing words, dates, amounts, and heading productions;
the enclosing clause, owner, modality, and alternatives determine what those
matches mean. A keyword match alone should not establish a required field.

## Shared preprocessing

`parse_job_text` and `parse_job_text_with_settings` create a `parse_session`.
Nested calls reuse that session. `posting_document.py` provides exact text spans,
physical lines, nonempty blocks, and parenthetical ownership. Role parsing,
posting scopes, field spans, timing clauses, lexical tokens, and document-clause
boundaries reuse immutable preprocessing results within the session.

Cache keys include the function and every argument, including the exact text.
Original, title-hinted, and masked variants have distinct coordinate systems.
Only immutable results may use `session_cached`; mutable parsed jobs, role
projections, and timing candidates must not be cached. The cache is bounded and
cleared when the outer parse returns or raises. No private text goes into a
process-wide cache. The heading-prefix index contains grammar constants only,
and static inline heading productions are compiled once at module import.

For HTML descriptions, `posting_html_text` preserves block/list boundaries while
keeping inline emphasis attached to its sentence. Generic apply pages, Eluta,
and Hanzilla use this helper for descriptions. Metadata remains scalar text.
Offsets in claims refer to their normalized posting text, **not original HTML
byte offsets**. This does not yet provide a DOM tree or reversible HTML mapping.

## Scoped academic requirements

The observed enrollment production illustrates the semantic contract:

```text
Currently enrolled in a post-secondary program,
and will not graduate prior to this co-op role
(preference for students enrolled in an official co-op program)
```

It produces three applicant-owned claims: required enrollment, a required
negative graduation predicate with a temporal condition, and a preferred co-op
status. The preference belongs to the parenthesis; it does not weaken the two
matrix conditions. Negative polarity does not by itself mean optional modality.
An explicit preference governing the entire graduation condition stays preferred.

Eligibility consumes scoped mentions and overlapping role claims. It distinguishes
required, preferred, non-required, alternative, and unresolved evidence internally.
A form question alone leaves co-op status unresolved and adds a manual-review
reason. A separately stated mandatory requirement still establishes the flag.

Requirement necessity uses three-valued logic over AND/OR expressions. A complete
alternative such as returning to studies can satisfy the requirement without
co-op registration. A university-name fragment is unknown to that predicate
classifier; an OR between school names cannot waive the shared enrollment head.
Each new predicate classifier must preserve this distinction.

The public eligibility schema remains compatible. `false` plus an ambiguous-field
entry represents unresolved question-only evidence; consumers should inspect
data quality alongside booleans. General enrollment retains its existing grammar.

## Timing authority and diagnostics

Timing selection uses ordered, named `EvidenceRule` entries in
`employment_timing.py`. They retain the established precedence, lazy compatibility
checks, and candidate identity used by subsequent date refinement. Rules record
selected, incompatible, or lower-priority decisions. No candidate produces an
unknown decision. Existing ambiguity handling and canonical Winter/Summer/Fall
terms remain part of the timing contract.

Diagnostics can be inspected without changing the API response:

```python
from jobcockpit.ingestion.parser import parse_job_text
from jobcockpit.ingestion.posting_document import parse_session

with parse_session() as session:
    parsed = parse_job_text(raw_text, source_url)
    decisions = list(session.decisions)
    cache_hits = session.cache_hits
```

These decisions are internal and may include source offsets. Store any real
posting diagnostics only under `var/`. Timing may make multiple resolution
passes; the decision list includes those passes, not just the final projection.

## Extending and validating

Use the [dataset map and decision table](parser-validation.md) to choose test data
and scan scope. The golden corpus supplies approved expectations; the saved corpus
supplies source evidence. Validation reports are observations, not expected answers.

Before adding grammar, inspect all matching real source contexts and separate
actual errors from legitimate matches. Add minimized `observed_posting` golden
cases before modifying behavior, including positive contrast cases and observed
layout variants. Keep lexical recognition separate from interpretation and field
authority. Prefer extending a shared production to adding an independent search
in a projection.

Run the golden corpus, focused contracts, the full parallel suite, Ruff, privacy
scan, and build as described in `AGENTS.md`. Compare complete old/new outputs on
the same hash-verified saved inputs and inspect every difference. Measure latency
without overlapping test/build workloads. Regression cases and a before/after
scan do not establish precision or recall on unseen employers; that requires a
separately labelled evaluation set grouped by employer/template.

This is an incremental shared grammar layer. Compensation, general enrollment,
and several timing/document recognizers still have their own domain grammars;
they have not all been replaced by a universal English syntax model.
