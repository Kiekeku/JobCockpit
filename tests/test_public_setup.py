from pathlib import Path
import os

from jobcockpit.core.config import Settings
from jobcockpit.extension.tokens import (
    create_extension_download_token,
    verify_extension_download_token,
)


def test_shared_configuration_uses_local_providers_and_installation_secret(tmp_path, monkeypatch):
    for name in list(os.environ):
        if name.startswith("JOBCOCKPIT_"):
            monkeypatch.delenv(name)

    def settings_at(directory):
        return Settings(
            _env_file=Path(".env.example"),
            artifact_root=directory / "private" / "storage",
            database_url=f"sqlite:///{directory / 'local.db'}",
        )

    settings = settings_at(tmp_path / "first")
    assert settings.gmail_provider == "synthetic"
    assert settings.posting_parser_provider == "heuristic"
    assert settings.cover_letter_generator_provider == "heuristic"
    assert settings.company_profile_research_provider == "disabled"
    assert settings.extension_answer_provider == "disabled"
    assert settings.posting_resolver_provider == "disabled"
    assert settings.posting_search_provider == "disabled"
    assert settings.posting_rendered_fallback_provider == "disabled"
    assert settings.fit_analysis_provider == "disabled"
    assert settings.publication_enabled is False
    assert settings.publication_policy_path is None
    assert settings.publication_write_token is None
    assert settings.serper_api_key is None or not settings.serper_api_key.get_secret_value()
    assert (settings.rankalpha_jobs_api_token is None
            or not settings.rankalpha_jobs_api_token.get_secret_value())
    assert settings.extension_token_secret is None

    token = create_extension_download_token(settings, package_id="demo", document_id="demo")
    assert verify_extension_download_token(settings_at(tmp_path / "first"), token) is not None
    assert verify_extension_download_token(settings_at(tmp_path / "second"), token) is None
