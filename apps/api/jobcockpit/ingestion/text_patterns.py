from __future__ import annotations

# Job pages commonly normalize range punctuation inconsistently. Keep the accepted
# separators in one place so numeric field parsers do not drift apart.
NUMERIC_RANGE_SEPARATOR_PATTERN = r"(?:-|[\u2010-\u2014]|\ufffd|to)"
