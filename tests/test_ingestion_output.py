from jobcockpit.ingestion.output import normalize_ats_ingestion, normalize_gmail_ingestion


EXPECTED_TOP_LEVEL_KEYS = {
    "schema_version",
    "operation",
    "status",
    "provider",
    "runs",
    "totals",
    "alerts",
}
EXPECTED_RUN_KEYS = {
    "run_id",
    "provider",
    "source_id",
    "source_url",
    "status",
    "counts",
    "items",
    "alerts",
    "metadata",
}
EXPECTED_COUNT_KEYS = {
    "items_seen_count",
    "jobs_created_count",
    "jobs_reused_count",
    "items_skipped_count",
    "items_needing_review_count",
    "items_failed_count",
    "runs_failed_count",
}
EXPECTED_ITEM_KEYS = {
    "item_id",
    "source_url",
    "resolved_url",
    "canonical_url",
    "status",
    "linked_job_id",
    "error_message",
    "retryable",
    "metadata",
}


def test_ats_and_gmail_ingestion_use_the_same_output_shape():
    ats = normalize_ats_ingestion(
        {
            "provider": "hanzilla",
            "source_url": "https://jobs.hanzilla.co/internships/",
            "jobs_seen_count": 1,
            "jobs_created_count": 1,
            "jobs_duplicate_count": 0,
            "jobs_skipped_count": 0,
            "jobs_language_skipped_count": 0,
            "ingestion_run": {
                "ingestion_run_id": "ingestrun_1",
                "status": "completed",
            },
            "items": [
                {
                    "ingestion_item_id": "ingestitem_1",
                    "source_url": "https://example.test/job/1",
                    "resolved_url": "https://example.test/job/1",
                    "canonical_url": "https://example.test/job/1",
                    "status": "imported",
                    "linked_job_id": "job_1",
                    "error_message": None,
                    "retryable": False,
                }
            ],
            "alerts": [],
            "jobs": [{"id": "job_1"}],
            "skipped_jobs": [],
            "language_skipped_jobs": [],
            "link_failures": [],
        }
    )
    gmail = normalize_gmail_ingestion(
        [
            {
                "source_id": "alertsrc_1",
                "display_name": "Job alerts",
                "email_ingestion_run_id": "emailrun_1",
                "processed_messages": [{"message_review_id": "message_1"}],
                "jobs_created_count": 1,
                "jobs_discovered_count": 1,
                "jobs_reused_count": 0,
                "messages_needing_review_count": 0,
                "unresolved_links_count": 0,
                "mailbox_mutations": [],
                "items": [
                    {
                        "item_id": "link_1",
                        "source_url": "https://example.test/job/1",
                        "resolved_url": "https://example.test/job/1",
                        "canonical_url": "https://example.test/job/1",
                        "status": "imported",
                        "linked_job_id": "job_1",
                        "error_message": None,
                        "retryable": False,
                        "metadata": {"message_id": "email_1"},
                    }
                ],
            }
        ],
        [],
    )

    assert set(ats) == set(gmail) == EXPECTED_TOP_LEVEL_KEYS
    assert set(ats["runs"][0]) == set(gmail["runs"][0]) == EXPECTED_RUN_KEYS
    assert set(ats["totals"]) == set(gmail["totals"]) == EXPECTED_COUNT_KEYS
    assert set(ats["runs"][0]["counts"]) == set(gmail["runs"][0]["counts"])
    assert (
        set(ats["runs"][0]["items"][0]) == set(gmail["runs"][0]["items"][0]) == EXPECTED_ITEM_KEYS
    )


def test_gmail_failed_source_is_a_failed_run_in_the_standard_output():
    output = normalize_gmail_ingestion(
        [],
        [
            {
                "source_id": "alertsrc_failed",
                "status": "failed",
                "error": "provider unavailable",
            }
        ],
    )

    assert output["status"] == "failed"
    assert output["totals"]["items_failed_count"] == 0
    assert output["totals"]["runs_failed_count"] == 1
    assert output["runs"][0]["status"] == "failed"
    assert output["runs"][0]["alerts"] == ["provider unavailable"]
