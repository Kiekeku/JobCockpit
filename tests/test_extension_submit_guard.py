"""Behavior is exercised through real Chromium IPC in test_extension_browser.py."""
import json
from pathlib import Path


def test_extension_sidepanel_permissions():
    root = Path('apps/chrome-extension')
    manifest = json.loads((root / 'manifest.json').read_text())
    assert manifest['side_panel']['default_path'] == 'sidepanel.html'
    assert 'default_popup' not in manifest['action']
    assert set(manifest['permissions']) == {'activeTab', 'scripting', 'storage', 'sidePanel'}
    assert set(manifest['host_permissions']) == {'http://localhost/*', 'http://127.0.0.1/*'}
    assert 'content_scripts' not in manifest
    assert not (root / 'popup.html').exists()


def test_packager_allowlist_contains_only_runtime():
    from scripts.package_extension import RUNTIME_FILES
    assert RUNTIME_FILES == {'manifest.json', 'sidepanel.html', 'sidepanel.css',
        'dist/content.js', 'dist/sidepanel.js', 'dist/background.js', 'dist/build-info.json'}
