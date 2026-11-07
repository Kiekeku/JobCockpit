"""Package only the built extension runtime; optionally sign with the existing local key."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import zipfile


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_FILES = {
    "manifest.json", "sidepanel.html", "sidepanel.css", "dist/content.js",
    "dist/sidepanel.js", "dist/background.js", "dist/build-info.json",
}


def package_extension(*, sign: bool = False, chrome: str | None = None) -> Path:
    metadata = json.loads((ROOT / "var/extension/latest-build.json").read_text(encoding="utf-8"))
    stage = Path(metadata["directory"]).resolve()
    if not stage.is_relative_to((ROOT / "var/extension/builds").resolve()):
        raise RuntimeError("The extension build is outside the intended build directory")
    actual_files = {path.relative_to(stage).as_posix() for path in stage.rglob("*") if path.is_file()}
    if actual_files != RUNTIME_FILES or any(path.is_symlink() for path in stage.rglob("*")):
        raise RuntimeError("Unexpected files or symlinks in the extension runtime")
    target = ROOT / "var/extension" / f"jobcockpit-{metadata['version']}-{metadata['build_id']}.zip"
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(RUNTIME_FILES):
            archive.write(stage / name, name)
    with zipfile.ZipFile(target) as archive:
        if set(archive.namelist()) != RUNTIME_FILES or archive.testzip() is not None:
            raise RuntimeError("Extension package validation failed")
    print(f"Validated ZIP: {target}")
    if sign:
        key = ROOT / "apps/chrome-extension.pem"
        if not key.is_file():
            raise RuntimeError("No existing local signing key. ZIP is ready; no key was generated.")
        executable = Path(chrome) if chrome else find_chrome()
        packed = stage.with_suffix(".crx")
        # Chrome refuses to overwrite an existing packed file. Preserve it first.
        if packed.exists():
            preserved = packed.with_name(packed.stem + "-previous.crx")
            shutil.copy2(packed, preserved)
            if packed.parent != (ROOT / "var/extension/builds").resolve():
                raise RuntimeError("Unexpected packed extension output")
            packed.unlink()
        result = subprocess.run([
            str(executable), "--headless=new", "--no-first-run",
            f"--user-data-dir={ROOT / 'var/extension/pack-profile'}",
            f"--pack-extension={stage}", f"--pack-extension-key={key}",
        ], capture_output=True, timeout=60,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        if result.returncode or not packed.is_file():
            raise RuntimeError("Chrome did not produce the signed package; the validated ZIP is available.")
        with zipfile.ZipFile(packed) as archive:
            files = {item.filename for item in archive.infolist() if not item.is_dir()}
            directories = {item.filename for item in archive.infolist() if item.is_dir()}
            if files != RUNTIME_FILES or not directories <= {"dist/"}:
                raise RuntimeError("Signed package contains unexpected runtime files")
            for name in RUNTIME_FILES:
                if archive.read(name) != (stage / name).read_bytes():
                    raise RuntimeError("Signed package differs from the reviewed build")
        installed_package = ROOT / "apps/chrome-extension.crx"
        if installed_package.exists():
            old_hash = hashlib.sha256(installed_package.read_bytes()).hexdigest()[:12]
            shutil.copy2(installed_package, ROOT / "var/extension" / f"previous-{old_hash}.crx")
        shutil.copy2(packed, installed_package)
        print(f"Validated signed CRX: {installed_package}")
    return target


def find_chrome() -> Path:
    local = Path(os.environ.get("LOCALAPPDATA", ".")) / "ms-playwright"
    candidates = sorted(local.glob("chromium-*/chrome-win64/chrome.exe"), reverse=True)
    candidates.extend([Path(os.environ.get("PROGRAMFILES", ".")) / "Google/Chrome/Application/chrome.exe"])
    found = next((candidate for candidate in candidates if candidate.is_file()), None)
    if not found:
        raise RuntimeError("Pass --chrome with a Chrome or Chromium executable to sign the CRX")
    return found


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sign", action="store_true", help="Use the existing local CRX signing key")
    parser.add_argument("--chrome")
    arguments = parser.parse_args()
    package_extension(sign=arguments.sign, chrome=arguments.chrome)
