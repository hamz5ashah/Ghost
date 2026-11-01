"""Regression guards for the desktop app's outbound network policy."""

from __future__ import annotations

import json
import plistlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
TAURI_CONFIG = ROOT / "frontend" / "src-tauri" / "tauri.conf.json"
MACOS_INFO_PLIST = ROOT / "frontend" / "src-tauri" / "Info.plist"
APPS_WORKFLOW = ROOT / ".github" / "workflows" / "build-apps.yml"
MOBILE_TAURI_CONFIG = ROOT / "mobile" / "src-tauri" / "tauri.conf.json"


def _csp_sources(directive: str) -> set[str]:
    config = json.loads(TAURI_CONFIG.read_text(encoding="utf-8"))
    csp = config["app"]["security"]["csp"]
    directives = {
        parts[0]: set(parts[1:]) for item in csp.split(";") if (parts := item.split())
    }
    return directives[directive]


def test_desktop_csp_allows_remote_api_servers() -> None:
    """The user-configured API URL may point beyond localhost (#649)."""
    connect_sources = _csp_sources("connect-src")

    assert {"http:", "https:", "ws:", "wss:"} <= connect_sources


def test_desktop_csp_allows_local_synthesized_audio() -> None:
    """Read-aloud plays a blob URL inside the Tauri webview (#1038)."""
    media_sources = _csp_sources("media-src")

    assert {"'self'", "blob:"} <= media_sources
    assert media_sources.isdisjoint({"http:", "https:", "*"})


def test_macos_webview_allows_user_configured_http_servers() -> None:
    """CSP alone cannot override App Transport Security for public hosts."""
    info = plistlib.loads(MACOS_INFO_PLIST.read_bytes())

    assert info["NSAppTransportSecurity"]["NSAllowsArbitraryLoadsInWebContent"] is True


def test_local_build_does_not_require_updater_signing_key() -> None:
    """Updater artifacts are a release concern, not a local-build default."""
    config = json.loads(TAURI_CONFIG.read_text(encoding="utf-8"))

    assert config["bundle"]["createUpdaterArtifacts"] is False


def test_apps_workflow_bundles_ollama_without_updater_signing() -> None:
    """Installers ship Ollama and never need an updater signing key."""
    workflow = APPS_WORKFLOW.read_text(encoding="utf-8")

    assert '"externalBin": ["binaries/ollama"]' in workflow
    assert '"createUpdaterArtifacts": false' in workflow
    assert "TAURI_SIGNING_PRIVATE_KEY" not in workflow
    assert "--config src-tauri/tauri.build.conf.json" in workflow


def test_desktop_installer_bundles_the_backend_source() -> None:
    """First launch copies the backend from the installer (no private clone)."""
    resources = json.loads(TAURI_CONFIG.read_text(encoding="utf-8"))["bundle"]["resources"]

    assert resources["../../pyproject.toml"] == "backend/pyproject.toml"
    assert resources["../../src/"] == "backend/src/"
    assert resources["../../rust/crates/"] == "backend/rust/crates/"
    # Never ship the Rust build directory.
    assert not any(src.rstrip("/").endswith("rust") for src in resources)


def test_android_app_can_reach_a_pc_on_the_local_network() -> None:
    """The phone talks to http://<pc-ip>:8000, so CSP and Android must allow it."""
    config = json.loads(MOBILE_TAURI_CONFIG.read_text(encoding="utf-8"))
    connect_src = next(
        part for part in config["app"]["security"]["csp"].split(";")
        if part.strip().startswith("connect-src")
    )
    assert "http:" in connect_src.split()
    assert config["build"]["frontendDist"].endswith("frontend/dist-mobile")

    workflow = APPS_WORKFLOW.read_text(encoding="utf-8")
    assert "npm run android:init" in workflow
    package = json.loads((ROOT / "mobile" / "package.json").read_text(encoding="utf-8"))
    assert "patch-android.mjs" in package["scripts"]["android:init"]
