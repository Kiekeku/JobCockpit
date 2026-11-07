from jobcockpit.ingestion.source_resolution import (
    aggregator_fallback_source_resolution,
    OfficialDiscoveryOutcome,
    canonical_official_discovery_reason,
    official_discovery_outcome,
    official_discovery_status_from_reason,
)


def test_aggregator_fallback_source_resolution_rejects_resolved_discovery_status():
    source_resolution = aggregator_fallback_source_resolution(
        path="aggregator_rendered_fallback",
        reason="official_posting_not_found",
        fallback_url="https://www.example-job-board.test/jobs/view/123",
        official_discovery_status="resolved",
    )

    assert source_resolution == {
        "kind": "aggregator_fallback",
        "path": "aggregator_rendered_fallback",
        "official_discovery_status": "not_found",
        "reason_code": "official_posting_not_found",
        "official_discovery_error": "official_posting_not_found",
        "fallback_url": "https://www.example-job-board.test/jobs/view/123",
    }


def test_aggregator_fallback_source_resolution_rejects_unknown_discovery_status():
    source_resolution = aggregator_fallback_source_resolution(
        path="aggregator_rendered_fallback",
        reason="codex_agentic_resolver_timeout",
        fallback_url="https://www.example-job-board.test/jobs/view/123",
        official_discovery_status="blocked",
    )

    assert source_resolution["official_discovery_status"] == "timeout"
    assert source_resolution["reason_code"] == "codex_agentic_resolver_timeout"


def test_aggregator_fallback_source_resolution_preserves_valid_failure_status():
    source_resolution = aggregator_fallback_source_resolution(
        path="aggregator_rendered_fallback",
        reason="manual_auth_wall",
        fallback_url="https://www.example-job-board.test/jobs/view/123",
        official_discovery_status="auth_blocked",
    )

    assert source_resolution["official_discovery_status"] == "auth_blocked"
    assert source_resolution["reason_code"] == "manual_auth_wall"


def test_official_discovery_status_classifies_rendered_gate_as_auth_blocked():
    assert (
        official_discovery_status_from_reason("chrome_cdp_failed: blocked_rendered_page")
        == "auth_blocked"
    )


def test_official_discovery_status_keeps_url_policy_block_unsafe():
    assert (
        official_discovery_status_from_reason(
            "codex_browser_url_policy_blocked: localhost_not_allowlisted"
        )
        == "unsafe_url"
    )
    assert official_discovery_status_from_reason("blocked_by_policy") == "unsafe_url"


def test_canonical_official_discovery_reason_discards_provider_output():
    reason = "codex_cli_failed: private command output | page evidence"

    assert canonical_official_discovery_reason(reason) == "codex_cli_failed"
    assert canonical_official_discovery_reason("posting_resolver_failed") == "posting_resolver_failed"
    assert canonical_official_discovery_reason("private provider output") == "official_discovery_failed"


def test_official_discovery_outcome_is_canonical_and_typed():
    outcome = official_discovery_outcome(
        "codex_cli_failed: private command output | page evidence",
        status="failed",
    )

    assert outcome.reason_code == "codex_cli_failed"
    assert outcome.status == "failed"


def test_official_discovery_outcome_recognizes_verified_posting():
    # Observed in six company-link resolutions: success was stored as failed.
    outcome = official_discovery_outcome("official_posting_resolved")
    assert outcome.status == "resolved"
    assert outcome.reason_code == "official_posting_resolved"



def test_official_discovery_outcome_distinguishes_search_candidates_from_posting():
    outcome = official_discovery_outcome(
        "official_candidates_found",
        status="candidates_found",
    )

    assert outcome.status == "candidates_found"
    assert outcome.reason_code == "official_candidates_found"


def test_official_discovery_outcome_derives_candidates_from_canonical_reason():
    outcome = official_discovery_outcome(
        "official_candidates_found: private provider detail",
    )

    assert outcome.status == "candidates_found"
    assert outcome.reason_code == "official_candidates_found"


def test_official_discovery_outcome_constructor_cannot_hold_raw_output():
    outcome = OfficialDiscoveryOutcome(
        status="failed",
        reason_code="private provider output with candidate evidence",
    )

    assert outcome.reason_code == "official_discovery_failed"


def test_official_discovery_outcome_derives_status_before_sanitizing_reason():
    outcome = official_discovery_outcome(
        "chrome_cdp_failed: blocked_rendered_page",
    )

    assert outcome.status == "auth_blocked"
    assert outcome.reason_code == "chrome_cdp_failed"
