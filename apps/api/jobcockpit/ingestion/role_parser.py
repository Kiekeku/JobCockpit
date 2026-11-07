from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

from jobcockpit.ingestion.capability_ontology import (
    non_technical_capability_pattern_rows,
    tech_stack_pattern_rows,
    technology_domains,
    technology_is_supported,
)
from jobcockpit.ingestion.location_parser import (
    extract_location_value as _extract_location_value,
    looks_like_comma_separated_location as _looks_like_comma_separated_location,
)
from jobcockpit.ingestion.parser_models import ParsedJob
from jobcockpit.ingestion.posting_fields import (
    _clean_field_value,
    _field_label,
    _field_value,
    _is_structured_field_label,
    _provenance_field_semantic,
    _provenance_field_value,
)

MAX_JOB_TITLE_CHARS = 255
MAX_JOB_TITLE_WORDS = 30

TECH_STACK_PATTERNS = tech_stack_pattern_rows()
NON_TECHNICAL_CAPABILITY_PATTERNS = non_technical_capability_pattern_rows()
TECHNICAL_CAPABILITY_DOMAINS = technology_domains()
DOMAIN_ORDER = (
    "software",
    "cybersecurity",
    "data",
    "hardware_engineering",
    "quality_assurance",
    "product_management",
    "project_management",
    "business_analysis",
    "finance",
    "sales",
    "marketing",
    "operations",
    "customer_support",
    "design",
    "research",
    "hr_recruiting",
    "legal_policy",
    "general_business",
)
DOMAIN_SIGNAL_PATTERNS = {
    "software": (
        r"\b(software|developer|backend|front[-\s]?end|full[-\s]?stack|web|api)\b",
        r"\b(python|typescript|javascript|fastapi|react|next\.js|docker|api|automation scripts?)\b",
    ),
    "cybersecurity": (
        r"\b(application security|appsec|cyber\s*security|cybersecurity|cyber|security engineer|security intern|security analyst|soc analyst|information security|infosec)\b",
        r"\b(siem|threat detection|incident response|vulnerabilit(?:y|ies)|iam|identity access|security monitoring|secure coding|access controls?|security policies|application controls?|authentication|authorization|splunk|wireshark)\b",
    ),
    "data": (
        r"\b(data|machine learning|ml|ai|analytics?|data scientist|data science|data engineer|data analyst|business intelligence|bi analyst)\b",
        r"\b(data analysis|analytics?|machine learning|statistical|sql|tableau|power\s*bi|etl|data pipelines?|dashboards?|spark|airflow|dbt|snowflake)\b",
    ),
    "hardware_engineering": (
        r"\b(hardware|electrical|optical|photonics|circuit|fpga|ecad|modem|pcb|embedded)\b",
        r"\b(hardware|electrical|optical|circuit|fpga|ecad|lab tests?|test equipment|high[-\s]speed digital|pcb|oscilloscopes?)\b",
    ),
    "quality_assurance": (
        r"\b(qa|quality assurance|sdet|software engineer in test|test engineer|testing)\b",
        r"\b(test plans?|test cases?|test automation|manual testing|regression tests?|selenium|playwright|quality assurance)\b",
    ),
    "product_management": (
        r"\b(product manager|product management|associate product manager|\bpm\b)\b",
        r"\b(product roadmap|roadmaps?|user stories|feature prioritization|product requirements|prd|backlog)\b",
    ),
    "project_management": (
        r"\b(project manager|project management|program manager|program management)\b",
        r"\b(project plans?|project coordination|milestones?|status reporting|risk tracking|stakeholder updates?)\b",
    ),
    "business_analysis": (
        r"\b(business analyst|business analysis|\bba\b)\b",
        r"\b(requirements gathering|business requirements|process mapping|map processes|uat|user acceptance testing)\b",
    ),
    "finance": (
        r"\b(finance|financial|accounting|investment|capital|treasury)\b",
        r"\b(financial model|financial statement|variance analysis|budget|forecast|accounting|excel)\b",
    ),
    "sales": (
        r"\b(sales|account executive|business development|revenue)\b",
        r"\b(crm|lead generation|pipeline|prospecting|customer discovery|account management)\b",
    ),
    "marketing": (
        r"\b(marketing|growth|brand|communications|content)\b",
        r"\b(seo|marketing campaigns?|email campaigns?|social media campaigns?|content marketing|market research|social media|brand)\b",
    ),
    "operations": (
        r"\b(operations|logistics|supply chain|process|program coordinator|project coordinator)\b",
        r"\b(process improvement|inventory|vendor management|logistics|supply chain|staff scheduling|shift scheduling|resource scheduling|production scheduling|project scheduling|scheduling meetings|scheduling interviews)\b",
    ),
    "customer_support": (
        r"\b(customer support|customer success|support specialist|client support|service desk)\b",
        r"\b(troubleshooting|ticketing|zendesk|customer success|customer support|client escalations?)\b",
    ),
    "design": (
        r"\b(design|designer|ux|ui|product design)\b",
        r"\b(figma|wireframing|prototyping|ux research|user research|design system)\b",
    ),
    "research": (
        r"\b(research|researcher|scientist|lab)\b",
        r"\b(literature review|experimental design|data collection|statistical analysis|lab)\b",
    ),
    "hr_recruiting": (
        r"\b(hr|human resources|recruiting|talent acquisition|people operations)\b",
        r"\b(recruiting|interview coordination|onboarding|hris|employee relations)\b",
    ),
    "legal_policy": (
        r"\b(legal|policy|compliance|privacy|contracts?)\b",
        r"\b(contract review|regulatory compliance|legal compliance|privacy compliance|compliance requirements|policy analysis|legal research|regulatory)\b",
    ),
    "general_business": (
        r"\b(business operations|strategy|chief of staff)\b",
        r"\b(stakeholder management|project coordination|reporting|presentations?|requirements gathering)\b",
    ),
}
ROLE_CAPABILITY_PATTERNS = (
    (
        "Software engineering",
        r"\bsoftware\s+projects?\b|\bsoftware\s+engineering\s+principles?\b",
        ("software",),
    ),
    (
        "Object-oriented programming",
        r"\bobject[- ]oriented\s+(?:languages?|programming)\b",
        ("software",),
    ),
    (
        "Requirements analysis",
        r"\b(?:determine|analy[sz]e|review)\s+(?:user\s+)?requirements?\b",
        ("business_analysis", "software"),
    ),
    (
        "Game development",
        r"\bgame(?:[- ]related)?\s+(?:development|projects?)\b",
        ("software",),
    ),
    (
        "Data structures and algorithms",
        r"\bdata\s+structures?\s+and\s+algorithms?\b",
        ("software",),
    ),
    (
        "Product reliability",
        r"\breliability\b(?=[^.;]{0,80}\bproducts?\b)",
        ("software", "operations"),
    ),
    ("Observability", r"\bobservability\b", ("software", "operations")),
    ("Monitoring", r"\bmonitoring\b", ("software", "operations")),
    ("Operations at scale", r"\boperations?\s+at\s+scale\b", ("operations",)),
    (
        "Technical problem solving",
        r"\b(?:solve\s+(?:complex\s+)?problems?\s+through\s+[^.;]{0,60}\bengineering|"
        r"structured\s+problem[- ]solving|technical\s+problems?)\b",
        ("software", "general_business"),
    ),
    ("Full-Stack Development", r"\bfull[-\s]?stack\b", ("software",)),
    (
        "Backend Development",
        r"\bbackend\b(?!\s+database\s+validation\b)|"
        r"\bback[-\s]?end\b(?!\s+database\s+validation\b)|"
        r"\bserver[-\s]?side\b|\brest\s+apis?\b",
        ("software",),
    ),
    (
        "Frontend Development",
        r"\bfront[-\s]?end\b|\bui components?\b|\buser interfaces?\b",
        ("software",),
    ),
    ("API Development", r"\bapi development\b|\bbuild\s+apis?\b|\bapis?\b", ("software",)),
    ("SIEM", r"\bsiem\b", ("cybersecurity",)),
    ("Security Monitoring", r"\bsecurity monitoring\b|\bmonitor\s+siem\b", ("cybersecurity",)),
    ("Incident Response", r"\bincident response\b", ("cybersecurity",)),
    (
        "Vulnerability Assessment",
        r"\bvulnerabilit(?:y|ies)\b|\bvulnerability assessment\b",
        ("cybersecurity",),
    ),
    ("Threat Detection", r"\bthreat detection\b|\bthreat hunting\b", ("cybersecurity",)),
    ("IAM", r"\biam\b|\bidentity and access management\b|\bidentity access\b", ("cybersecurity",)),
    ("Secure Coding", r"\bsecure coding\b", ("cybersecurity",)),
    ("Access Controls", r"\baccess controls?\b|\bapplication controls?\b", ("cybersecurity",)),
    ("Authentication", r"\bauthentication\b", ("cybersecurity",)),
    ("Authorization", r"\bauthorization\b", ("cybersecurity",)),
    ("Security Policies", r"\bsecurity policies\b", ("cybersecurity",)),
    ("PCB Design", r"\bpcb\b|\bprinted\s+circuit\s+board\b", ("hardware_engineering",)),
    ("FPGA", r"\bfpga\b", ("hardware_engineering",)),
    ("Oscilloscope", r"\boscilloscopes?\b", ("hardware_engineering",)),
    (
        "Embedded Systems",
        r"\bembedded\s+(?:applications?|code|development|devices?|firmware|hardware|"
        r"linux|operating\s+systems?|software|systems?)\b",
        ("hardware_engineering", "software"),
    ),
    ("Data Pipelines", r"\bdata pipelines?\b", ("data",)),
    ("ETL", r"\betl\b", ("data",)),
    ("Spark", r"\b(?:apache\s+)?spark\b", ("data",)),
    ("Airflow", r"\b(?:apache\s+)?airflow\b", ("data",)),
    ("Dashboarding", r"\bdashboards?\b", ("data", "general_business")),
    (
        "Data Visualization",
        r"\bdata visualizations?\b|\bvisualizations?\b",
        ("data", "general_business"),
    ),
    (
        "Statistical Analysis",
        r"\bstatistical analysis\b|\bstatistical modeling\b|\bstatistics\b|"
        r"\bstatistical(?:\s+and\s+analytical)?\s+skills\b",
        ("data", "research"),
    ),
    (
        "Experimentation",
        r"\bexperiments?\b|\ba\s*/\s*b testing\b|\bab testing\b",
        ("data", "research", "product_management"),
    ),
    ("Financial Modeling", r"\bfinancial models?\b|\bfinancial modeling\b", ("finance",)),
    ("Variance Analysis", r"\bvariance analysis\b", ("finance",)),
    ("Budget Forecasting", r"\bbudget forecasts?\b|\bbudgeting\b|\bforecasting\b", ("finance",)),
    ("Financial Statements", r"\bfinancial statements?\b", ("finance",)),
    (
        "Data Analysis",
        r"\bdata analysis\b|\banalytics?\b|\bsql analysis\b|"
        r"\banaly[sz](?:e|ing)\s+(?:data|datasets?)\b",
        ("data", "general_business"),
    ),
    (
        "Machine Learning",
        r"\bmachine[-\s]learning\b|\bdeep learning\b|\bml\b",
        ("data",),
    ),
    ("Tableau", r"\btableau\b", ("data", "general_business")),
    ("Power BI", r"\bpower\s*bi\b", ("data", "general_business")),
    ("Product Management", r"\bproduct manager\b|\bproduct management\b", ("product_management",)),
    ("Roadmapping", r"\broadmaps?\b|\bproduct roadmap\b", ("product_management",)),
    ("User Stories", r"\buser stories\b", ("product_management", "business_analysis")),
    (
        "Project Management",
        r"\bproject manager\b|\bproject management\b|\bprogram management\b|"
        r"\bproject administration\b",
        ("project_management",),
    ),
    ("Business Analysis", r"\bbusiness analyst\b|\bbusiness analysis\b", ("business_analysis",)),
    (
        "Requirements Gathering",
        r"\brequirements gathering\b|"
        r"\b(?:analy[sz]e|collect|define|determine|document|elicit|gather|prioritize|translat(?:e|es|ed|ing))\b"
        r"[^.;]{0,50}\b(?:business|client|feature|product|user)\s+requirements\b",
        ("business_analysis", "product_management", "general_business"),
    ),
    (
        "Data cleaning",
        r"\b(?:clean|cleaning)\b[^.;]{0,50}\b(?:data|datasets?)\b|"
        r"\b(?:data|datasets?)\b[^.;]{0,50}\b(?:clean|cleaning)\b",
        ("data",),
    ),
    ("AI modeling", r"\b(?:ai|artificial intelligence)\s+model(?:ing|ling)\b", ("data",)),
    (
        "Prompt engineering",
        r"\bprompt(?:ing|\s+engineering)\b",
        ("data", "software"),
    ),
    (
        "Problem solving",
        r"\bproblem[- ]solving\b|\bsolve\s+(?:complex\s+)?(?:business|technical)\s+problems?\b",
        ("general_business", "software"),
    ),
    (
        "Process Mapping",
        r"\bprocess mapping\b|\bmap processes\b",
        ("business_analysis", "operations"),
    ),
    ("UAT", r"\buat\b|\buser acceptance testing\b", ("business_analysis", "quality_assurance")),
    (
        "Quality Assurance",
        r"\bqa\b|\bquality assurance\b|\btesting activities\b|"
        r"\bfunctional\s*,\s*regression\s*,\s*system integration\b",
        ("quality_assurance",),
    ),
    ("Test Planning", r"\btest plans?\b|\btest cases?\b", ("quality_assurance",)),
    (
        "Regression Testing",
        r"\bregression tests?\b|\bregression testing\b|"
        r"\bregression(?=\s*,\s*(?:integration|system)\b)",
        ("quality_assurance",),
    ),
    (
        "Test Automation",
        r"\btest automation\b|\bautomated tests?\b",
        ("quality_assurance", "software"),
    ),
    ("Manual Testing", r"\bmanual testing\b", ("quality_assurance",)),
    ("Selenium", r"\bselenium\b", ("quality_assurance", "software")),
    ("Playwright", r"\bplaywright\b", ("quality_assurance", "software")),
    ("CRM", r"\bcrm\b|\bsalesforce\b|\bhubspot\b", ("sales", "marketing", "customer_support")),
    ("Lead Generation", r"\blead generation\b|\bprospecting\b", ("sales",)),
    ("Sales Pipeline", r"\bsales pipeline\b|\bpipeline management\b", ("sales",)),
    ("Account Management", r"\baccount management\b", ("sales", "customer_support")),
    ("SEO", r"\bseo\b", ("marketing",)),
    (
        "Campaign Management",
        r"\bcampaign management\b|\b(?:marketing|email|social media|paid|ad|advertising|growth|brand|content)\s+campaigns?\b|\bcampaigns?\s+(?:management|planning|performance|analytics)\b",
        ("marketing",),
    ),
    ("Market Research", r"\bmarket research\b", ("marketing", "research")),
    ("Social Media", r"\bsocial media\b", ("marketing",)),
    ("Process Improvement", r"\bprocess improvement\b", ("operations", "general_business")),
    ("Inventory Management", r"\binventory management\b|\binventory\b", ("operations",)),
    ("Vendor Management", r"\bvendor management\b", ("operations",)),
    (
        "Scheduling",
        r"\b(?:staff|shift|resource|production|project|calendar|interview|meeting|workforce)\s+scheduling\b|\bscheduling\s+(?:meetings|interviews|shifts|resources|production|deliveries)\b",
        ("operations", "hr_recruiting"),
    ),
    (
        "Troubleshooting",
        r"\btroubleshooting\b",
        ("customer_support", "software", "hardware_engineering"),
    ),
    ("Ticketing", r"\bticketing\b|\bzendesk\b|\bservice desk\b", ("customer_support",)),
    ("Customer Success", r"\bcustomer success\b", ("customer_support", "sales")),
    ("Figma", r"\bfigma\b", ("design",)),
    ("Wireframing", r"\bwireframing\b", ("design",)),
    ("Prototyping", r"\bprototyping\b", ("design",)),
    ("UX Research", r"\bux research\b|\buser research\b", ("design", "research")),
    ("Literature Review", r"\bliterature review\b", ("research",)),
    ("Experimental Design", r"\bexperimental design\b", ("research",)),
    ("Data Collection", r"\bdata collection\b", ("research", "data")),
    (
        "Recruiting",
        r"\brecruiting\b|\btalent acquisition\b|"
        r"\b(?:campus|personnel)\s+recruitment\b|"
        r"\brecruitment\s+(?:activities|coordination|operations|records?)\b|"
        r"\bhr(?:\s+functions|\s+ops)?\b[^.;]{0,60}\brecruitment\b",
        ("hr_recruiting",),
    ),
    ("Interview Coordination", r"\binterview coordination\b", ("hr_recruiting",)),
    ("Onboarding", r"\bonboarding\b", ("hr_recruiting",)),
    ("Contract Review", r"\bcontract review\b", ("legal_policy",)),
    (
        "Compliance",
        r"\b(?:regulatory|legal|privacy|security|sox|soc\s*2|pci|hipaa|governance|risk)\s+compliance\b|\bcompliance\s+(?:requirements|programs?|policies|controls|audits?|reviews?|monitoring)\b",
        ("legal_policy",),
    ),
    ("Policy Analysis", r"\bpolicy analysis\b", ("legal_policy",)),
    (
        "Stakeholder Management",
        r"\bstakeholder management\b",
        ("general_business", "product_management", "project_management"),
    ),
    (
        "Project Coordination",
        r"\bproject coordination\b",
        ("general_business", "operations", "project_management"),
    ),
    (
        "Reporting",
        r"\breporting\b|\b(?:prepare|create|build|generate|analyze|produce|maintain|deliver|write)\s+(?:\w+\s+){0,3}reports?\b|\b(?:monthly|weekly|quarterly|annual|financial|business|status|dashboard|analytics?)\s+reports?\b|\bsummarize findings\b",
        ("general_business", "finance", "data"),
    ),
    ("Presentation Skills", r"\bpresentations?\b|\bslide decks?\b", ("general_business",)),
)


@dataclass(frozen=True)
class RoleCapabilityContextRule:
    positive_context_patterns: tuple[str, ...] = ()
    negative_context_patterns: tuple[str, ...] = ()
    requires_context: bool = False


_PROJECT_MANAGER_PERSON_NOUN_PHRASE = (
    r"(?:[a-z][\w'-]*\s+){0,4}project\s+managers?\b"
)


ROLE_CAPABILITY_CONTEXT_RULES = {
    "Project Management": RoleCapabilityContextRule(
        negative_context_patterns=(
            rf"\b(?:"
            r"report(?:s|ed|ing)?"
            r"(?:\s+(?:(?:and\s+)?[a-z][\w'-]*ly|(?:dotted|solid)[-\s]+line|up)){0,4}"
            r"\s+(?:into|through|to|via)"
            r"|(?:work(?:s|ed|ing)?\s+)?under"
            rf")\s+{_PROJECT_MANAGER_PERSON_NOUN_PHRASE}",
            rf"\b(?:"
            r"collaborat(?:e|es|ed|ing)|partner(?:s|ed|ing)?|work(?:s|ed|ing)?"
            r")"
            r"(?:\s+[a-z][\w'-]*ly){0,3}"
            rf"\s+(?:alongside|for|under|with)\s+{_PROJECT_MANAGER_PERSON_NOUN_PHRASE}",
            rf"\b(?:(?:direct|dotted[-\s]+line|solid[-\s]+line)\s+)?"
            r"reporting\s+(?:line|relationship)\s+(?:into|to|with)\s+"
            rf"{_PROJECT_MANAGER_PERSON_NOUN_PHRASE}",
        ),
    ),
}


def _replace_implausible_title_with_baseline(
    parsed: ParsedJob,
    *,
    baseline: ParsedJob | None,
) -> None:
    if _is_plausible_job_title(parsed.role.job_title):
        return
    if baseline and _is_plausible_job_title(baseline.role.job_title):
        parsed.role.job_title = baseline.role.job_title
        parsed.role.normalized_title = baseline.role.normalized_title


def _fill_role_capability_defaults(
    parsed: ParsedJob,
    baseline: ParsedJob | None = None,
    *,
    posting_text: str = "",
) -> None:
    role = parsed.role
    role_text = "\n".join(
        item
        for item in (
            role.job_title or "",
            role.description_clean or "",
            role.description_raw or "",
        )
        if item
    )
    baseline_domains = list(baseline.role.capability_domains) if baseline else []
    baseline_capabilities = list(baseline.role.capabilities) if baseline else []
    baseline_tech_stack = list(baseline.role.tech_stack) if baseline else []
    for capability in baseline_tech_stack:
        _append_unique(role.tech_stack, capability)
    if role.capabilities:
        for capability in baseline_capabilities:
            _append_unique(role.capabilities, capability)
    else:
        capabilities: list[str] = []
        for capability in baseline_capabilities:
            _append_unique(capabilities, capability)
        if not role.capability_domains and baseline_domains:
            role.capability_domains = list(baseline_domains)
        for capability in role.tech_stack:
            _append_unique(capabilities, capability)
        if capabilities:
            role.capabilities = capabilities
        elif role.evidence:
            role.capabilities = []
        else:
            role.capabilities = _extract_role_capabilities(
                role_text,
                role.tech_stack,
                require_candidate_scope=True,
            )
    for capability in role.tech_stack:
        _append_unique(role.capabilities, capability)
    full_stack_role_context = bool(
        re.search(
            r"\broles?\s*,?\s+including\b[^.;]{0,160}"
            r"\b(?:frontend|backend|full[- ]?stack)\b",
            posting_text,
            re.IGNORECASE,
        )
    )
    if full_stack_role_context:
        for capability, pattern in (
            ("Full-Stack Development", r"\bfull[- ]?stack\b"),
            ("Backend Development", r"\bback[- ]?end\b"),
            ("Frontend Development", r"\bfront[- ]?end\b"),
        ):
            if re.search(pattern, "\n".join((role_text, posting_text)), re.IGNORECASE):
                _append_unique(role.environment_capabilities, capability)
                _append_unique(role.capabilities, capability)
    inferred_domains = _infer_capability_domains(
        role.job_title,
        "" if role.evidence else role_text,
        supported_capabilities=role.capabilities,
    )
    fallback_domains = _merge_domains(inferred_domains, baseline_domains)
    domain_context = _merge_domains(role.capability_domains, fallback_domains)
    derived_domains = _domains_from_capabilities(role.capabilities, domain_context)
    derived_domains = _merge_domains(
        derived_domains,
        _technical_domains_from_capabilities(role.tech_stack, domain_context),
    )
    if role.capability_domains:
        role.capability_domains = _merge_domains(role.capability_domains, derived_domains)
        return
    role.capability_domains = _merge_domains(fallback_domains, derived_domains)


def _extract_tech_stack(
    raw_text: str,
    *,
    assume_capability_context: bool = False,
    assume_title_context: bool = False,
    title_supporting_domains: list[str] | tuple[str, ...] = (),
) -> list[str]:
    technologies: list[str] = []
    for technology, pattern in TECH_STACK_PATTERNS:
        if any(
            technology_is_supported(
                technology,
                raw_text,
                match,
                assume_capability_context=assume_capability_context,
                assume_title_context=assume_title_context,
                title_supporting_domains=title_supporting_domains,
            )
            for match in re.finditer(pattern, raw_text, flags=re.IGNORECASE)
        ):
            _append_unique(technologies, technology)
    return technologies


def _infer_capability_domains(
    title: str | None,
    raw_text: str,
    *,
    supported_capabilities: list[str] | None = None,
) -> list[str]:
    title_text = title or ""
    supported = set(supported_capabilities or [])
    scores: dict[str, int] = {}
    for domain in DOMAIN_ORDER:
        title_pattern, body_pattern = DOMAIN_SIGNAL_PATTERNS[domain]
        score = 0
        if re.search(title_pattern, title_text, flags=re.IGNORECASE):
            score += 2 if domain == "software" else 3
        if any(
            _domain_body_match_is_supported(domain, raw_text, match, supported)
            for match in re.finditer(body_pattern, raw_text, flags=re.IGNORECASE)
        ):
            score += 1
        if score:
            scores[domain] = score
    return sorted(scores, key=lambda domain: (-scores[domain], DOMAIN_ORDER.index(domain)))


def _domain_body_match_is_supported(
    domain: str,
    raw_text: str,
    domain_match: re.Match[str],
    supported_capabilities: set[str],
) -> bool:
    overlapping_capabilities: set[str] = set()
    for capability, pattern in (
        *TECH_STACK_PATTERNS,
        *NON_TECHNICAL_CAPABILITY_PATTERNS,
    ):
        if domain not in TECHNICAL_CAPABILITY_DOMAINS.get(capability, ()):
            continue
        if any(
            _spans_overlap(domain_match, capability_match)
            for capability_match in re.finditer(pattern, raw_text, flags=re.IGNORECASE)
        ):
            overlapping_capabilities.add(capability)
    for capability, pattern, capability_domains in ROLE_CAPABILITY_PATTERNS:
        if domain not in capability_domains:
            continue
        if any(
            _spans_overlap(domain_match, capability_match)
            for capability_match in re.finditer(pattern, raw_text, flags=re.IGNORECASE)
        ):
            overlapping_capabilities.add(capability)
    return not overlapping_capabilities or bool(
        overlapping_capabilities & supported_capabilities
    )


def _spans_overlap(first: re.Match[str], second: re.Match[str]) -> bool:
    return first.start() < second.end() and second.start() < first.end()


def _extract_role_capabilities(
    raw_text: str,
    tech_stack: list[str],
    *,
    restrict_technologies_to_stack: bool = False,
    require_candidate_scope: bool = False,
) -> list[str]:
    capabilities: list[str] = []
    for item in tech_stack:
        _append_unique(capabilities, item)
    for capability in _extract_non_technical_capabilities(
        raw_text,
        require_candidate_scope=require_candidate_scope,
    ):
        _append_unique(capabilities, capability)
    for capability, pattern, _domains in ROLE_CAPABILITY_PATTERNS:
        if (
            restrict_technologies_to_stack
            and capability in TECHNICAL_CAPABILITY_DOMAINS
            and capability not in tech_stack
        ):
            continue
        if any(
            _role_capability_match_is_supported(capability, raw_text, match)
            for match in re.finditer(pattern, raw_text, flags=re.IGNORECASE)
        ):
            _append_unique(capabilities, capability)
    return capabilities


def _extract_non_technical_capabilities(
    raw_text: str,
    *,
    assume_title_context: bool = False,
    title_supporting_domains: list[str] | tuple[str, ...] = (),
    require_candidate_scope: bool = False,
) -> list[str]:
    capabilities: list[str] = []
    for capability, pattern in NON_TECHNICAL_CAPABILITY_PATTERNS:
        if any(
            technology_is_supported(
                capability,
                raw_text,
                match,
                assume_title_context=assume_title_context,
                title_supporting_domains=title_supporting_domains,
                require_candidate_scope=require_candidate_scope,
            )
            for match in re.finditer(pattern, raw_text, flags=re.IGNORECASE)
        ):
            _append_unique(capabilities, capability)
    return capabilities


def _extract_role_option_capabilities(
    title: str | None,
    posting_text: str,
) -> list[str]:
    """Preserve explicitly named software role families outside claim sections."""

    contexts = [
        match.group(0)
        for match in re.finditer(
            r"\broles?\s*,?\s+including\b[^.;]{0,200}",
            posting_text,
            re.IGNORECASE,
        )
    ]
    text = "\n".join(contexts)
    capabilities: list[str] = []
    for capability, pattern in (
        ("Full-Stack Development", r"\bfull[- ]?stack\b"),
        ("Backend Development", r"\bback[- ]?end\b"),
        ("Frontend Development", r"\bfront[- ]?end\b"),
    ):
        if re.search(pattern, text, re.IGNORECASE):
            _append_unique(capabilities, capability)
    return capabilities


def _role_capability_source_match(
    canonical_name: str,
    raw_text: str,
) -> tuple[str, int, int] | None:
    matches = [
        match
        for capability, pattern, _domains in ROLE_CAPABILITY_PATTERNS
        if capability.casefold() == canonical_name.casefold()
        for match in re.finditer(pattern, raw_text, flags=re.IGNORECASE)
        if _role_capability_match_is_supported(capability, raw_text, match)
    ]
    if not matches:
        return None
    match = min(matches, key=lambda item: (item.start(), -(item.end() - item.start())))
    return match.group(0), match.start(), match.end()


def _role_capability_match_is_supported(
    capability: str,
    raw_text: str,
    match: re.Match[str],
) -> bool:
    rule = ROLE_CAPABILITY_CONTEXT_RULES.get(capability)
    if rule is not None:
        if any(
            usage.start() <= match.start() and match.end() <= usage.end()
            for pattern in rule.negative_context_patterns
            for usage in re.finditer(pattern, raw_text, flags=re.IGNORECASE)
        ):
            return False
        if rule.requires_context:
            context_start = max(0, match.start() - 80)
            context_end = min(len(raw_text), match.end() + 80)
            context = raw_text[context_start:context_end]
            if not any(
                re.search(pattern, context, flags=re.IGNORECASE)
                for pattern in rule.positive_context_patterns
            ):
                return False
    if capability != "Reporting":
        return True
    if match.group(0).casefold() != "reporting":
        return True
    suffix = raw_text[match.end() : match.end() + 40]
    return re.match(r"\s+to\b", suffix, re.IGNORECASE) is None


def _domains_from_capabilities(
    capabilities: list[str], domain_context: list[str] | None = None
) -> list[str]:
    context = set(domain_context or [])
    domains = _technical_domains_from_capabilities(capabilities, domain_context)
    capability_set = set(capabilities)
    for capability, _pattern, capability_domains in ROLE_CAPABILITY_PATTERNS:
        if capability not in capability_set:
            continue
        for domain in _domain_contributions(capability_domains, context):
            _append_unique(domains, domain)
    return _valid_domains_in_order(domains)


def _domain_contributions(
    capability_domains: tuple[str, ...], context: set[str]
) -> tuple[str, ...]:
    if not context:
        return capability_domains
    overlapping_domains = tuple(domain for domain in capability_domains if domain in context)
    return overlapping_domains or capability_domains


def _technical_domains_from_capabilities(
    capabilities: list[str], domain_context: list[str] | None = None
) -> list[str]:
    context = set(domain_context or [])
    domains: list[str] = []
    for capability in capabilities:
        capability_domains = TECHNICAL_CAPABILITY_DOMAINS.get(capability)
        if not capability_domains:
            continue
        for domain in _technical_domain_contributions(capability_domains, context):
            _append_unique(domains, domain)
    return _valid_domains_in_order(domains)


def _technical_domain_contributions(
    capability_domains: tuple[str, ...], context: set[str]
) -> tuple[str, ...]:
    if context:
        overlapping_domains = tuple(domain for domain in capability_domains if domain in context)
        if overlapping_domains:
            return overlapping_domains
    return capability_domains[:1]


def _merge_domains(primary: list[str], secondary: list[str]) -> list[str]:
    merged: list[str] = []
    for domain in [*primary, *secondary]:
        _append_unique(merged, domain)
    return _valid_domains_in_order(merged)


def _valid_domains_in_order(domains: list[str]) -> list[str]:
    valid_domains = set(DOMAIN_ORDER)
    return [domain for domain in domains if domain in valid_domains]


def _append_unique(items: list[str], item: str) -> None:
    if item and item not in items:
        items.append(item)


def _title_value(lines: list[str]) -> str | None:
    page_title = _page_title(lines)
    explicit_title_lines = (
        lines
        if _is_blocked_page_text(page_title or "")
        else _lines_excluding_labels(lines, {"source evidence"})
    )
    # Prefer fields whose schema says that their value is a role.  Source-list
    # metadata is lower-quality evidence than the posting's own title fields,
    # but it is still structurally a title; this matters when the fetched page
    # itself exposes only a generic "Jobs" heading.
    title = _field_value(explicit_title_lines, "Title", "Job Title", "Role", "Position")
    if _is_plausible_job_title(title) and not _is_generic_careers_title(title or ""):
        return _clean_title_value(_dedupe_repeated_phrase(title))

    if page_title and not _is_blocked_page_text(page_title):
        title = _title_from_mixed_line(page_title)
        if title:
            return title

    title = _provenance_field_value(lines, "role")
    if title:
        # Adapter snapshots can retain the full posting heading while their
        # discovery metadata abbreviates it. Require both the Company envelope
        # and an independent repetition in the body before preserving a suffix.
        if (
            len(lines) > 1
            and _field_label(lines[1]) == "company"
            and lines[0].startswith(title + ",")
            and lines.count(lines[0]) > 1
        ):
            full_title = _title_from_mixed_line(lines[0])
            if full_title:
                return full_title
        title = _title_from_mixed_line(title)
        if title:
            return title

    source_identity = _source_evidence_identity(lines)
    if source_identity:
        return source_identity[0]

    # A capture with an explicit page-title envelope is browser/ATS content.
    # If none of the structured identity tiers above owns a title, arbitrary
    # body lines are navigation or listing content rather than job identity.
    if page_title:
        return None
    for line in lines:
        if _is_non_title_line(line):
            continue
        title = _title_from_mixed_line(line)
        if title:
            return title
    return None


def _source_evidence_identity(lines: list[str]) -> tuple[str, str] | None:
    for line in lines:
        if _field_label(line) != "source evidence":
            continue
        title_value = _field_value([line], "Source Evidence")
        company_value = _field_value([line], "Company")
        if not title_value:
            continue

        card_segments = [
            _clean_field_value(segment)
            for segment in re.split(r"\s+(?:\u00b7|\ufffd)\s+", title_value)
        ]
        if len(card_segments) >= 3 and all(card_segments[:2]):
            card_title = _clean_title_value(_dedupe_repeated_phrase(card_segments[0]))
            card_company = _clean_company_name(card_segments[1])
            embedded_company = _clean_company_name(company_value)
            companies_agree = not embedded_company or (
                card_company
                and " ".join(card_company.casefold().split())
                == " ".join(embedded_company.casefold().split())
            )
            if _is_plausible_job_title(card_title) and card_company and companies_agree:
                return card_title, card_company

        if not company_value:
            continue
        title = _title_from_source_evidence(title_value)
        company = _clean_company_name(company_value)
        if title and company:
            return title, company
    return None


def _title_from_source_evidence(value: str) -> str | None:
    if _source_evidence_is_commentary(value):
        return None
    before_company = re.split(r"\s+Company\s*:", value, maxsplit=1, flags=re.IGNORECASE)[0]
    segments = [_clean_field_value(segment) for segment in before_company.split("\u00b7")]
    segments = [segment for segment in segments if segment]
    if len(segments) >= 2:
        return _clean_title_value(_dedupe_repeated_phrase(segments[0]))
    return _title_from_mixed_line(before_company)


def _title_from_mixed_line(line: str) -> str | None:
    candidate = re.split(r"\s+Company\s*:", line, maxsplit=1, flags=re.IGNORECASE)[0]
    candidate = _clean_field_value(candidate)
    linkedin_title = _title_from_linkedin_page_title(candidate)
    if linkedin_title:
        return linkedin_title
    if re.search(r"\|\s*LinkedIn\s*$", candidate, flags=re.IGNORECASE):
        return None
    candidate = _clean_title_value(_dedupe_repeated_phrase(candidate)) if candidate else ""
    if _is_generic_careers_title(candidate):
        return None
    return candidate if _is_plausible_job_title(candidate) else None


def _source_evidence_is_commentary(value: str) -> bool:
    normalized = " ".join(value.casefold().split())
    evidence_reporting_clause = (
        r"(?:official\s+)?(?:careers?\s+page|posting\s+url)\s+(?:"
        r"appear(?:s|ed|ing)?|contain(?:s|ed|ing)?|redirect(?:s|ed|ing)?|"
        r"(?:is|was)\s+(?:available|found|indexed|listed|present|shown))"
    )
    return bool(
        re.search(
            r"\b(?:rendered verification|required review|caller review|redirects? to|"
            r"url reconstructed|exact job must be selected|indexed (?:title|content)|"
            rf"{evidence_reporting_clause})\b",
            normalized,
        )
    )


def _is_generic_careers_title(value: str) -> bool:
    normalized = " ".join(value.casefold().strip(" -|").split())
    return bool(
        re.fullmatch(
            r"(?:careers?|career opportunities|job opportunities|jobs|open positions|"
            r"current openings|job details)(?: (?:at|with) .+)?|"
            r"[\w .&'’]{1,80}\s+careers|"
            r"job board(?:\s*\|\s*.+)?|"
            r"(?:(?:.+?)\s*(?:-|\|)\s*)?careers?\s+"
            r"(?:page|homepage|cent(?:re|er))"
            r"(?:\s*\|\s*recruitment)?|"
            r".+\s*\|\s*careers?\s+at\s+.+|"
            r"view\s+open\s+positions(?:\s+.+)?|"
            r".+\s+jobs?\s*(?:&|and)\s*.+\s+jobs?\s*\|\s*"
            r"(?:your\s+)?career\s+at\s+.+",
            normalized,
        )
    )


def _is_plausible_job_title(value: str | None) -> bool:
    if not value or _is_generic_careers_title(value):
        return False
    cleaned = " ".join(value.split())
    return len(cleaned) <= MAX_JOB_TITLE_CHARS and len(cleaned.split()) <= MAX_JOB_TITLE_WORDS


def _clean_title_value(value: str) -> str:
    cleaned = value
    while True:
        stripped = re.sub(
            r"^(?:Title|Job Title|Role|Position):\s*", "", cleaned, flags=re.IGNORECASE
        )
        if stripped == cleaned:
            return cleaned
        cleaned = stripped


def _company_value(
    lines: list[str], title: str | None, source_url: str | None = None
) -> str | None:
    page_title = _page_title(lines)
    non_evidence_lines = _lines_excluding_labels(lines, {"source evidence"})
    company = _field_value(non_evidence_lines, "Company")
    company = _clean_company_name(company)
    if company:
        # Discovery can split a prose paragraph at "at" into Title/Company.
        # Replace that artifact only when the body proves the split and the
        # page title independently agrees with consecutive role/employer lines.
        explicit_title = _field_value(non_evidence_lines, "Title")
        if explicit_title and not _is_plausible_job_title(explicit_title):
            reconstructed = f"{explicit_title} at {company}"
            if reconstructed in non_evidence_lines:
                corroborated = _company_from_corroborated_page_title(lines, page_title)
                if corroborated:
                    return corroborated
        return company
    company = _company_from_linkedin_page_title(page_title or "")
    if company:
        return company
    source_identity = _source_evidence_identity(lines)
    if source_identity and title:
        evidence_title, evidence_company = source_identity
        if " ".join(evidence_title.casefold().split()) == " ".join(title.casefold().split()):
            return evidence_company
    provenance_company = _clean_company_name(_provenance_field_value(lines, "company"))
    tenant = _workday_tenant(source_url or "")
    if not tenant:
        apply_url = _field_value(lines, "Apply URL") or _provenance_field_value(
            lines, "apply url"
        )
        tenant = _workday_tenant(apply_url or "")
    tenant_company = _company_corroborated_by_workday_tenant(lines, tenant)
    if provenance_company:
        if not tenant_company or _company_matches_workday_tenant(provenance_company, tenant):
            return provenance_company
        return tenant_company
    return (
        tenant_company
        or _company_from_corroborated_page_title(lines, page_title)
        or _company_from_corroborated_title_suffix(non_evidence_lines, title)
    )


def _company_from_corroborated_title_suffix(
    lines: list[str], title: str | None
) -> str | None:
    if not lines or not title:
        return None
    headers = (
        _field_value(lines, "Page Title"),
        _field_value(lines, "Title", "Job Title", "Role", "Position"),
        lines[0],
    )
    title_key = " ".join(title.casefold().split())
    # A dash also separates role specializations. Only accept an employer suffix
    # owned by this role and independently introduced in the posting body.
    body_lines = [line for line in lines if not _field_label(line)]
    for header in headers:
        if not header:
            continue
        role, separator, suffix = header.rpartition(" - ")
        company = _clean_company_name(suffix)
        if not separator or not company or len(company) > 80:
            continue
        if not _is_plausible_job_title(role) or _is_non_title_line(role):
            continue
        if title_key not in {
            " ".join(value.casefold().split()) for value in (role, header)
        }:
            continue
        company_pattern = re.escape(company)
        if any(
            re.fullmatch(rf"About\s+{company_pattern}", line, re.IGNORECASE)
            or re.match(rf"At\s+{company_pattern},\s+we\b", line, re.IGNORECASE)
            for line in body_lines
        ):
            return company
    return None


def _company_from_corroborated_page_title(
    lines: list[str], page_title: str | None
) -> str | None:
    if not page_title or " | " not in page_title:
        return None
    identity = page_title.rsplit(" | ", 1)[0].strip()
    # Eluta repeats the title and employer on consecutive body lines. Match
    # that pair rather than splitting on slashes inside roles or company aliases.
    for role_line, company_line in zip(lines, lines[1:]):
        if identity != f"{role_line} / {company_line}":
            continue
        if _is_plausible_job_title(role_line) and not _is_non_title_line(role_line):
            return _clean_company_name(company_line)
    return None


def _workday_tenant(source_url: str) -> str:
    try:
        hostname = (urlparse(source_url).hostname or "").casefold()
    except ValueError:
        return ""
    labels = hostname.split(".")
    if (
        len(labels) < 3
        or re.fullmatch(r"wd\d+", labels[1]) is None
        or labels[2] != "myworkdayjobs"
    ):
        return ""
    return re.sub(r"[^a-z0-9]+", "", labels[0])


def _company_corroborated_by_workday_tenant(lines: list[str], tenant: str) -> str | None:
    if not tenant:
        return None
    body_lines = [
        line
        for line in lines
        if _provenance_field_semantic(_field_label(line)) is None
    ]
    for line in body_lines:
        candidates: list[str] = []
        about_match = re.fullmatch(r"\s*About\s+(?P<company>.+?)\s*", line, re.IGNORECASE)
        if about_match:
            candidates.append(about_match.group("company"))
        at_match = re.search(
            r"\b[Aa]t\s+(?P<company>[A-Z][A-Za-z0-9&.'’ -]{0,80}?)"
            r"(?:,|\?|\.|\s+is\b|\s+we\b)",
            line,
        )
        if at_match:
            candidates.append(at_match.group("company"))
        for candidate in candidates:
            company = _clean_company_name(candidate)
            if company and _company_matches_workday_tenant(company, tenant):
                return tenant.upper() if len(tenant) <= 5 else company
    return None


def _company_matches_workday_tenant(company: str, tenant: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "", company.casefold())
    if not normalized or not tenant:
        return False
    if normalized == tenant or normalized.startswith(tenant) or tenant.startswith(normalized):
        return True
    if len(tenant) > 5:
        return False
    words = re.findall(r"[a-z0-9]+", company.casefold())
    initialisms = {
        "".join(word[0] for word in words),
        "".join(word[0] for word in words if word not in {"and", "of", "the"}),
    }
    return tenant in initialisms


def _location_value(lines: list[str]) -> str | None:
    page_title = _page_title(lines)
    return _extract_location_value(
        lines,
        page_title_location=_location_from_linkedin_page_title(page_title or ""),
        blocked_page=_is_blocked_page_text(page_title or ""),
    )


def _page_title(lines: list[str]) -> str | None:
    explicit_page_title = _field_value(lines, "Page Title")
    if explicit_page_title:
        return explicit_page_title
    return next(
        (
            line
            for line in lines
            if re.search(r"\|\s*LinkedIn\s*$", line, flags=re.IGNORECASE)
            and _linkedin_page_title_parts(line)
        ),
        None,
    )


def _lines_excluding_labels(lines: list[str], labels: set[str]) -> list[str]:
    return [line for line in lines if _field_label(line) not in labels]


def _clean_company_name(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = _clean_field_value(value)
    for prefix in ("LinkedIn", "Indeed"):
        if cleaned.casefold().startswith(f"{prefix.casefold()} ") and len(cleaned.split()) > 1:
            cleaned = cleaned[len(prefix) :].strip()
    return cleaned or None


def _linkedin_page_title_parts(value: str) -> tuple[str, str, str | None] | None:
    stripped = value.strip()
    has_linkedin_suffix = bool(re.search(r"\s+\|\s+LinkedIn\s*$", stripped, re.IGNORECASE))
    if not has_linkedin_suffix:
        return None
    cleaned = re.sub(r"\s+\|\s+LinkedIn\s*$", "", stripped, flags=re.IGNORECASE)
    match = re.match(
        r"^(?P<company>.+?)\s+hiring\s+(?P<role_and_location>.+)$", cleaned, flags=re.IGNORECASE
    )
    if not match:
        segments = [_clean_field_value(segment) for segment in cleaned.split("|")]
        if len(segments) >= 2 and all(segments):
            role = " | ".join(segments[:-1])
            company = segments[-1]
            if not _is_generic_careers_title(role) and _is_plausible_job_title(role):
                return company, role, None
        return None

    company = match.group("company")
    role_and_location = match.group("role_and_location")
    title = role_and_location
    location = None
    if " in " in role_and_location:
        title_candidate, location_candidate = role_and_location.rsplit(" in ", 1)
        if _looks_like_comma_separated_location(location_candidate):
            title = title_candidate
            location = location_candidate
    return company, title, location


def _title_from_linkedin_page_title(value: str) -> str | None:
    parts = _linkedin_page_title_parts(value)
    if not parts:
        return None
    return _clean_field_value(parts[1])


def _company_from_linkedin_page_title(value: str) -> str | None:
    parts = _linkedin_page_title_parts(value)
    if not parts:
        return None
    return _clean_company_name(parts[0])


def _location_from_linkedin_page_title(value: str) -> str | None:
    parts = _linkedin_page_title_parts(value)
    if not parts:
        return None
    location = parts[2]
    return _clean_field_value(location) if location else None


def _dedupe_repeated_phrase(value: str) -> str:
    tokens = value.split()
    if len(tokens) >= 2 and len(tokens) % 2 == 0:
        halfway = len(tokens) // 2
        if tokens[:halfway] == tokens[halfway:]:
            return " ".join(tokens[:halfway])
    return value


def _is_non_title_line(line: str) -> bool:
    label = _field_label(line)
    if _is_structured_field_label(label):
        return True
    return _is_blocked_page_text(line) or _is_generic_careers_title(line)


def _is_blocked_page_text(value: str) -> bool:
    lower = value.casefold()
    return any(
        marker in lower
        for marker in (
            "blocked - indeed.com",
            "request blocked",
            "you have been blocked",
            "troubleshooting cloudflare errors",
        )
    )
