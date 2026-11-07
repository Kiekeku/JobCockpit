from pathlib import Path


def test_node_scripts_share_dotenv_loader():
    helper = Path("scripts/env.mjs").read_text(encoding="utf-8")

    assert "export function loadDotEnv" in helper
    assert "export function unquoteEnvValue" in helper
    for script in ["scripts/dev.mjs", "scripts/extension-dev.mjs", "scripts/stop.mjs"]:
        source = Path(script).read_text(encoding="utf-8")
        assert 'from "./env.mjs"' in source
        assert "function loadDotEnv" not in source
        assert "function unquoteEnvValue" not in source
