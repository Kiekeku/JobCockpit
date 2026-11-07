from __future__ import annotations


ACADEMIC_TERM_MONTHS = {
    "winter": (1, 4),
    "spring": (5, 4),
    "summer": (5, 4),
    "fall": (9, 4),
    "autumn": (9, 4),
}
MONTH_YEAR_ACADEMIC_TERM_LABELS = {
    1: "Winter",
    5: "Summer",
    9: "Fall",
}
COMBINED_ACADEMIC_TERMS = {
    "winter/summer": ("winter", 8),
    "fall/winter": ("fall", 8),
    "autumn/winter": ("autumn", 8),
    "spring/summer": ("spring", 4),
}
ACADEMIC_TERM_PATTERN = (
    r"winter\s*[/\-\u2013]\s*summer|"
    r"fall\s*[/\-\u2013]\s*winter|"
    r"autumn\s*[/\-\u2013]\s*winter|"
    r"spring\s*[/\-\u2013]\s*summer|"
    r"winter|spring|summer|fall|autumn"
)
MONTH_NAMES = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}
DURATION_NUMBER_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
}


NON_EMPLOYMENT_TIMING_DATE_LABELS = {
    "application deadline",
    "application end date",
    "application start date",
    "apply by",
    "close date",
    "closing date",
    "date posted",
    "job post end date",
    "open date",
    "posted",
    "posted date",
    "posted on",
    "posting close date",
    "posting closing date",
    "posting end date",
    "posting opening date",
    "posting start date",
    "requisition date",
}
