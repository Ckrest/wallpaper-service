#!/usr/bin/env python3
"""Machine-readable Settings Hub payloads for wallpaper-service."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

CONFIG_FILE = Path.home() / ".config" / "settings-hub" / "wallpaper.json"


def _load_config() -> dict[str, Any]:
    if not CONFIG_FILE.exists():
        return {}
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _active_source(config: dict[str, Any]) -> dict[str, Any]:
    mode = str(config.get("active_type", config.get("type", "video"))).strip() or "video"
    if mode == "image":
        path = str((config.get("image") or {}).get("path", "")).strip() if isinstance(config.get("image"), dict) else ""
        fit = str((config.get("image") or {}).get("fit", "fill")).strip() if isinstance(config.get("image"), dict) else "fill"
        exists = bool(path and Path(path).expanduser().exists())
        return {"mode": mode, "path": path, "exists": exists, "label": Path(path).name if path else "No image selected", "fit": fit}
    if mode == "video":
        path = str((config.get("video") or {}).get("path", "")).strip() if isinstance(config.get("video"), dict) else ""
        loop = bool((config.get("video") or {}).get("loop", True)) if isinstance(config.get("video"), dict) else True
        exists = bool(path and Path(path).expanduser().exists())
        return {"mode": mode, "path": path, "exists": exists, "label": Path(path).name if path else "No video selected", "loop": loop}
    color = str((config.get("solid") or {}).get("color", "#1a1a2e")).strip() if isinstance(config.get("solid"), dict) else "#1a1a2e"
    return {"mode": "solid", "color": color, "exists": True, "label": color}


def build_payload(view: str) -> Any:
    config = _load_config()
    active = _active_source(config)
    health_status = "ok" if active.get("exists") else "warn"
    mode_label = str(active.get("mode", "wallpaper")).replace("_", " ").title()
    detail = str(active.get("path") or active.get("color") or "")
    summary = {
        "badge": active.get("mode", ""),
        "text": active.get("label", ""),
        "status": health_status,
        "mode": active.get("mode"),
        "active_source": active,
        "health": {
            "status": health_status,
            "text": f"{mode_label} wallpaper ready" if health_status == "ok" else f"{mode_label} wallpaper needs a valid source",
            "detail": detail or "Choose a source to apply as the desktop background",
        },
        "config_file": str(CONFIG_FILE),
    }
    if view == "summary":
        return summary
    if view == "resolved":
        return {"summary": summary, "config": config}
    raise ValueError(f"Unsupported view: {view}")


def main() -> int:
    parser = argparse.ArgumentParser(description="settings-hub bridge for wallpaper-service")
    parser.add_argument("view", choices=["summary", "resolved"])
    args = parser.parse_args()
    print(json.dumps(build_payload(args.view), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
