import json
from pathlib import Path

import tomllib

from jobcockpit.core.config import Settings


def test_pytest_speed_workflow_is_documented_and_scripted():
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    package = json.loads(Path("package.json").read_text(encoding="utf-8"))
    test_script = Path("scripts/test.mjs").read_text(encoding="utf-8")
    guide = Path("docs/user-guide.md").read_text(encoding="utf-8")
    ci_workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")

    pytest_options = pyproject["tool"]["pytest"]["ini_options"]
    scripts = package["scripts"]

    assert "slow: slower end-to-end or integration-style tests" in pytest_options["markers"]
    assert "integration: tests that exercise multiple app layers" in pytest_options["markers"]
    assert scripts["test"] == "node scripts/test.mjs"
    assert scripts["test:fast"] == "node scripts/test.mjs --fast"
    assert scripts["test:slow"] == "node scripts/test.mjs --slow"
    assert scripts["test:serial"] == "node scripts/test.mjs --serial"
    assert 'mkdirSync(".pytest-tmp", { recursive: true })' in test_script
    assert 'mkdirSync(".tmp", { recursive: true })' in test_script
    assert '".tmp/uv-run-cache"' in test_script
    assert '"--no-sync"' in test_script
    assert '"--basetemp"' in test_script
    assert "pytest-basetemp-" in test_script
    assert '"-n"' in test_script
    assert "PYTEST_XDIST_WORKERS" in test_script
    assert "availableParallelism" in test_script
    assert "Math.min(16, availableParallelism())" in test_script
    assert '"uv.exe"' in test_script
    assert '"no:cacheprovider"' in test_script
    assert '"not slow"' in test_script
    assert '"slow"' in test_script
    assert "uv run pytest -n auto" in ci_workflow
    assert any(
        dependency.startswith("pytest-xdist")
        for dependency in pyproject["dependency-groups"]["dev"]
    )
    assert "pnpm test:fast" in guide
    assert "pnpm test:slow" in guide
    assert "pnpm test:serial" in guide
    assert "PYTEST_XDIST_WORKERS" in guide
    assert "/.pytest-tmp/" in Path(".gitignore").read_text(encoding="utf-8")


def test_pytest_runtime_uses_inert_provider_defaults():
    settings = Settings(_env_file=None)

    assert settings.gmail_provider == "synthetic"
    assert settings.posting_resolver_provider == "disabled"
    assert settings.posting_search_provider == "disabled"
    assert settings.posting_rendered_fallback_provider == "disabled"
    assert settings.posting_parser_provider == "heuristic"
    assert settings.fit_analysis_provider == "disabled"
    assert settings.cover_letter_generator_provider == "heuristic"
    assert settings.cover_letter_generator_timeout_seconds == 360
    assert settings.cover_letter_verification_reasoning_effort == "low"
    assert settings.cover_letter_verification_max_attempts == 2
    assert settings.chrome_cdp_auto_start is False
    assert settings.playwright_auth_wall_wait_seconds == 0
    assert settings.playwright_auth_wall_settle_seconds == 0
    assert settings.posting_direct_fetch_timeout_seconds == 0.1

    explicit = Settings(
        posting_resolver_provider="unit_test_resolver",
        posting_search_provider="unit_test_search",
        posting_rendered_fallback_provider="unit_test_rendered",
        posting_parser_provider="unit_test_parser",
        fit_analysis_provider="unit_test_fit",
    )

    assert explicit.posting_resolver_provider == "unit_test_resolver"
    assert explicit.posting_search_provider == "unit_test_search"
    assert explicit.posting_rendered_fallback_provider == "unit_test_rendered"
    assert explicit.posting_parser_provider == "unit_test_parser"


def test_codex_model_settings_can_be_loaded_from_dotenv(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "JOBCOCKPIT_CODEX_CLI_MODEL=gpt-env-model\n"
        "JOBCOCKPIT_POSTING_PARSER_REASONING_EFFORT=low\n"
        "JOBCOCKPIT_COMPANY_PROFILE_REASONING_EFFORT=medium\n"
        "JOBCOCKPIT_COVER_LETTER_GENERATOR_REASONING_EFFORT=high\n",
        encoding="utf-8",
    )

    settings = Settings(_env_file=env_file)

    assert settings.codex_cli_model == "gpt-env-model"
    assert settings.posting_parser_reasoning_effort == "low"
    assert settings.company_profile_reasoning_effort == "medium"
    assert settings.cover_letter_generator_reasoning_effort == "high"


def test_helper_only_modules_stay_in_fast_suite():
    modules = {
        Path(
            "tests/test_reprocess_source_hints.py"
        ): "test_reprocess_hint_prefers_linkedin_source_evidence",
        Path(
            "tests/test_gmail_adapters_and_url_policy.py"
        ): "test_email_link_extractor_ignores_footer_links",
    }

    for module, representative_test in modules.items():
        source = module.read_text(encoding="utf-8")
        assert representative_test in source
        assert "pytestmark" not in source
        assert "@pytest.mark.slow" not in source

    assert "pytestmark = [pytest.mark.integration, pytest.mark.slow]" in Path(
        "tests/test_jobs_applications_documents.py"
    ).read_text(encoding="utf-8")
    assert "pytestmark = [pytest.mark.integration, pytest.mark.slow]" in Path(
        "tests/test_url_policy_and_gmail.py"
    ).read_text(encoding="utf-8")
def test_backend_ci_installs_typescript_dependencies_before_pytest():
    import yaml

    workflow = yaml.safe_load(Path(".github/workflows/ci.yml").read_text(encoding="utf-8"))
    steps = workflow["jobs"]["backend"]["steps"]
    pnpm_setup = next(i for i, step in enumerate(steps)
                      if step.get("uses", "").startswith("pnpm/action-setup@"))
    node_setup = next(i for i, step in enumerate(steps)
                      if step.get("uses", "").startswith("actions/setup-node@"))
    install = next(i for i, step in enumerate(steps)
                   if step.get("run") == "pnpm install --frozen-lockfile")
    pytest_step = next(i for i, step in enumerate(steps) if "pytest" in step.get("run", ""))
    assert pnpm_setup < node_setup < install < pytest_step
    assert steps[node_setup]["with"]["node-version"] == 22
