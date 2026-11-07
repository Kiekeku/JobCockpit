from __future__ import annotations

from pathlib import Path
import secrets

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def default_codex_cli_workspace() -> Path:
    return Path(__file__).resolve().parents[4]


class Settings(BaseSettings):
    database_url: str = "sqlite:///./var/jobcockpit.db"
    artifact_root: Path = Field(default=Path("var/private/storage"))
    browser_health_enabled: bool = True
    source_sweep_interval_seconds: float = Field(default=60.0, ge=1.0, le=3600.0)
    background_workers_enabled: bool = True
    background_worker_poll_seconds: float = Field(default=1.0, ge=0.1, le=60.0)
    background_worker_shutdown_timeout_seconds: float = Field(default=5.0, ge=0.0, le=30.0)
    background_worker_lease_seconds: float = Field(default=15.0, ge=5.0, le=300.0)
    extension_token_secret: str | None = None
    ats_mock_port: int = Field(default=8001, ge=1, le=65535)
    gmail_provider: str = Field(default="synthetic", pattern="^(synthetic|google)$")
    gmail_oauth_client_secret_path: Path | None = None
    gmail_oauth_token_path: Path = Field(default=Path("var/private/gmail/token.json"))
    gmail_oauth_local_server_port: int = Field(default=0, ge=0, le=65535)
    gmail_max_results: int = Field(default=25, ge=1, le=500)
    gmail_scan_max_results: int = Field(default=250, ge=1, le=5000)
    gmail_request_min_interval_seconds: float = Field(default=1.0, ge=0, le=60)
    gmail_read_max_retries: int = Field(default=6, ge=0, le=8)
    posting_resolver_provider: str = Field(default="search", pattern="^[a-z][a-z0-9_]*$")
    posting_search_provider: str = Field(default="serper", pattern="^[a-z][a-z0-9_]*$")
    posting_search_max_results: int = Field(default=5, ge=1, le=20)
    serper_api_key: SecretStr | None = None
    serper_country: str = Field(default="ca", pattern="^[a-zA-Z]{2}$")
    serper_language: str = Field(default="en", pattern="^[a-zA-Z]{2}$")
    serper_timeout_seconds: float = Field(default=15.0, ge=1.0, le=60.0)
    serper_max_attempts: int = Field(default=2, ge=1, le=2)
    rankalpha_jobs_api_token: SecretStr | None = None
    publication_enabled: bool = False
    publication_policy_path: Path | None = None
    publication_write_token: SecretStr | None = None
    publication_timeout_seconds: float = Field(default=20, ge=1, le=120)
    publication_max_attempts: int = Field(default=5, ge=1, le=20)
    publication_assessment_timeout_seconds: int = Field(default=120, ge=1, le=600)
    posting_rendered_fallback_provider: str = Field(default="invisible_playwright", pattern="^[a-z][a-z0-9_]*$")
    posting_rendered_fallback_timeout_seconds: int = Field(default=180, ge=1, le=600)
    posting_rendered_reasoning_effort: str = Field(
        default="medium", pattern="^(minimal|low|medium|high|xhigh|max)$"
    )
    posting_rendered_fallback_retries: int = Field(default=1, ge=0, le=3)
    posting_rendered_fallback_min_interval_seconds: float = Field(default=5.0, ge=0.0, le=120.0)
    posting_rendered_fallback_concurrency: int = Field(default=2, ge=1, le=4)
    scheduled_rendered_extra_domain_groups: dict[str, tuple[str, ...]] = Field(
        default_factory=dict
    )
    ingestion_link_concurrency: int = Field(default=4, ge=1, le=4)
    posting_external_resolution_concurrency: int = Field(default=4, ge=1, le=10)
    posting_direct_fetch_timeout_seconds: float = Field(default=5.0, ge=0.1, le=60.0)
    playwright_user_data_dir: Path = Field(default=Path("var/playwright-profile"))
    playwright_profile_directory: str | None = None
    playwright_channel: str | None = None
    playwright_headless: bool = True
    invisible_playwright_user_data_dir: Path = Field(default=Path("var/invisible-playwright-profile"))
    invisible_playwright_seed: int = Field(default=42, ge=1, le=2_147_483_647)
    invisible_playwright_timezone: str = Field(default="America/Denver", min_length=1)
    invisible_playwright_locale: str = Field(default="en-US", min_length=1)
    invisible_playwright_auto_prepare: bool = True
    invisible_playwright_prepare_timeout_seconds: int = Field(default=60, ge=5, le=180)
    invisible_playwright_prepare_cooldown_seconds: int = Field(default=300, ge=30, le=3600)
    playwright_auth_wall_wait_seconds: float = Field(default=120.0, ge=0.0, le=600.0)
    playwright_auth_wall_settle_seconds: float = Field(default=5.0, ge=0.0, le=60.0)
    chrome_cdp_endpoint_url: str = Field(default="http://127.0.0.1:9222", max_length=255)
    chrome_cdp_auto_start: bool = False
    chrome_cdp_executable_path: Path | None = None
    chrome_cdp_user_data_dir: Path = Field(default=Path("var/chrome-cdp-profile"))
    chrome_cdp_profile_directory: str | None = None
    posting_parser_provider: str = Field(default="heuristic", pattern="^[a-z][a-z0-9_]*$")
    posting_parser_timeout_seconds: int = Field(default=60, ge=1, le=300)
    posting_parser_max_input_chars: int = Field(default=12000, ge=1000, le=50000)
    posting_parser_reasoning_effort: str = Field(
        default="medium", pattern="^(minimal|low|medium|high|xhigh|max)$"
    )
    fit_analysis_provider: str = Field(default="disabled", pattern="^[a-z][a-z0-9_]*$")
    fit_analysis_timeout_seconds: int = Field(default=60, ge=1, le=300)
    fit_analysis_max_input_chars: int = Field(default=16000, ge=1000, le=50000)
    fit_analysis_reasoning_effort: str = Field(
        default="medium", pattern="^(minimal|low|medium|high|xhigh|max)$"
    )
    company_profile_research_provider: str = Field(
        default="codex_cli", pattern="^(disabled|codex_cli)$"
    )
    company_profile_research_timeout_seconds: int = Field(default=180, ge=1, le=600)
    company_profile_reasoning_effort: str = Field(
        default="medium", pattern="^(minimal|low|medium|high|xhigh|max)$"
    )
    cover_letter_generator_provider: str = Field(default="heuristic", pattern="^[a-z][a-z0-9_]*$")
    cover_letter_generator_timeout_seconds: int = Field(default=360, ge=1, le=600)
    cover_letter_generator_max_input_chars: int = Field(default=20000, ge=2000, le=100000)
    cover_letter_generator_model: str | None = Field(default=None, min_length=1, max_length=100)
    cover_letter_verification_model: str | None = Field(default=None, min_length=1, max_length=100)
    cover_letter_generator_reasoning_effort: str = Field(
        default="high", pattern="^(minimal|low|medium|high|xhigh|max)$"
    )
    cover_letter_verification_reasoning_effort: str = Field(
        default="low", pattern="^(minimal|low|medium|high|xhigh|max)$"
    )
    cover_letter_verification_max_attempts: int = Field(default=2, ge=1, le=5)
    cover_letter_timezone: str = Field(default="America/Edmonton", min_length=1, max_length=100)
    codex_cli_command: str = "codex"
    codex_cli_model: str = Field(default="gpt-6-luna", min_length=1, max_length=100)
    codex_cli_workspace: Path = Field(default_factory=default_codex_cli_workspace)
    codex_cli_ignore_user_config: bool = True
    codex_cli_rendered_ignore_user_config: bool = False
    extension_answer_provider: str = Field(default="codex_cli", pattern="^(disabled|codex_cli)$")
    extension_answer_model: str = Field(default="gpt-6-sol", min_length=1, max_length=100)
    extension_answer_reasoning_effort: str = Field(default="medium", pattern="^(low|medium|high|xhigh|max)$")
    extension_answer_timeout_seconds: int = Field(default=180, ge=10, le=600)
    extension_answer_max_input_chars: int = Field(default=80000, ge=10000, le=150000)

    model_config = SettingsConfigDict(env_prefix="JOBCOCKPIT_", env_file=".env", extra="ignore")

    @field_validator("publication_policy_path", mode="before")
    @classmethod
    def empty_publication_path(cls, value):
        return None if value == "" else value

    def allowed_localhost_urls(self) -> set[str]:
        return {f"localhost:{self.ats_mock_port}", f"127.0.0.1:{self.ats_mock_port}"}

    def get_extension_token_secret(self) -> str:
        if self.extension_token_secret:
            return self.extension_token_secret

        secret_path = self.artifact_root.parent / "extension-token-secret"
        if secret_path.exists():
            return secret_path.read_text(encoding="utf-8").strip()

        secret_path.parent.mkdir(parents=True, exist_ok=True)
        secret = secrets.token_urlsafe(32)
        secret_path.write_text(secret, encoding="utf-8")
        try:
            secret_path.chmod(0o600)
        except OSError:
            pass
        return secret
