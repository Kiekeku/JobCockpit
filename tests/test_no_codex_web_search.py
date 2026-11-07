import ast
from pathlib import Path

from jobcockpit.core.config import Settings


PRODUCTION_ROOT = Path("apps/api/jobcockpit")


def test_production_codex_calls_never_enable_web_search():
    violations: list[str] = []

    for path in PRODUCTION_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function_name = (
                node.func.id
                if isinstance(node.func, ast.Name)
                else node.func.attr
                if isinstance(node.func, ast.Attribute)
                else ""
            )
            if function_name != "run_codex_prompt":
                continue
            for keyword in node.keywords:
                if keyword.arg != "search":
                    continue
                if not (isinstance(keyword.value, ast.Constant) and keyword.value.value is False):
                    violations.append(f"{path}:{node.lineno}")

    assert violations == []


def test_builtin_provider_registry_has_no_codex_search_path():
    source = Path("apps/api/jobcockpit/ingestion/llm_providers.py").read_text(encoding="utf-8")

    assert Settings.model_fields["posting_search_provider"].default == "serper"
    assert 'register_search_provider("serper"' in source
    assert 'register_search_provider("codex_cli"' not in source
    assert 'register_posting_resolver_provider("codex_cli"' not in source
    assert "CodexCliSearchClient" not in source
    assert "CodexCliAgenticPostingResolver" not in source
