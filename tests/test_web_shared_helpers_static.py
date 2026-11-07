from pathlib import Path
from struct import unpack


def test_web_app_has_compact_square_favicon():
    favicon = Path("apps/web/app/icon.png").read_bytes()

    assert favicon.startswith(b"\x89PNG\r\n\x1a\n")
    assert unpack(">II", favicon[16:24]) == (256, 256)
    assert len(favicon) < 100_000


def test_web_pages_share_api_and_format_helpers():
    api = Path("apps/web/app/lib/api.ts").read_text(encoding="utf-8")
    formatting = Path("apps/web/app/lib/format.ts").read_text(encoding="utf-8")

    assert "export async function fetchJson" in api
    assert "export function formatDurationMonths" in formatting
    for page in [
        "apps/web/app/page.tsx",
        "apps/web/app/profile/page.tsx",
        "apps/web/app/sources/page.tsx",
        "apps/web/app/jobs/[id]/page.tsx",
    ]:
        source = Path(page).read_text(encoding="utf-8")
        assert 'from "./lib/api"' in source or 'from "../lib/api"' in source or 'from "../../lib/api"' in source
        assert "async function fetchJson" not in source

    assert "function formatDurationMonths" not in Path("apps/web/app/page.tsx").read_text(encoding="utf-8")
    assert "function formatDurationMonths" not in Path("apps/web/app/jobs/[id]/page.tsx").read_text(encoding="utf-8")
