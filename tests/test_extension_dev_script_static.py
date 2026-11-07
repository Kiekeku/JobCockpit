import json
from pathlib import Path


def test_extension_dev_script_is_exposed_from_package_json():
    package = json.loads(Path("package.json").read_text(encoding="utf-8"))

    assert package["scripts"]["extension:dev"] == "node scripts/extension-dev.mjs"


def test_extension_dev_script_builds_and_launches_dedicated_chrome_profile():
    script = Path("scripts/extension-dev.mjs").read_text(encoding="utf-8")

    assert "Usage: pnpm extension:dev" in script
    assert "@jobcockpit/chrome-extension" in script
    assert "build" in script
    assert "var/chrome-extension-dev-profile" in script
    assert "--user-data-dir=" in script
    assert "--load-extension=" in script
    assert "--disable-extensions-except=" in script
    assert "JOBCOCKPIT_CHROME_PATH" in script
    assert "JOBCOCKPIT_EXTENSION_CHROME_PROFILE_DIR" in script
    assert "JOBCOCKPIT_EXTENSION_DEV_URL" in script
    assert "JOBCOCKPIT_WEB_PORT" in script
    assert "defaultStartupUrl" in script
    assert "http://localhost:3010" not in script
    assert "--no-build" in script
    assert "--profile-dir" in script
    assert "--url" in script


def test_extension_setup_docs_include_one_command_dev_launcher():
    docs = Path("docs/chrome-extension.md").read_text(encoding="utf-8")
    readme = Path("README.md").read_text(encoding="utf-8")

    assert "pnpm extension:dev" in docs
    assert "var/chrome-extension-dev-profile" in docs
    assert "--chrome" in docs
    assert "Manual fallback" in docs
    assert "pnpm extension:dev" in readme
    assert "Manual fallback" in readme
    assert "http://localhost:3010" not in docs
    assert "http://localhost:3010" not in readme
    assert "JOBCOCKPIT_WEB_PORT" in docs
    assert "JOBCOCKPIT_WEB_PORT" in readme
