from __future__ import annotations

import json
import os
from pathlib import Path

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QComboBox


_BOT_KEYS = {
    "enabled", "steamlogin", "steampassword", "steamtradetoken",
    "webproxy", "webproxyusername", "webproxypassword", "botbehaviour",
    "steamuserpermissions", "steamparentalsettings", "customizedtags",
    "gamesplayedwhileidle", "lootabletypes",
}


def discover_asf_roots() -> list[Path]:
    """Find likely ASF install/config roots without asking the user to browse manually."""
    settings = QSettings("FANFIRIEL-LANG", "GameDelivery")
    candidates: list[Path] = []
    saved_exe = str(settings.value("asf/executable_path", "") or "").strip()
    saved_config = str(settings.value("asf/config_path", "") or "").strip()
    if saved_config:
        candidates.append(Path(saved_config))
    if saved_exe:
        exe = Path(saved_exe)
        candidates.extend((exe.parent / "config", exe.parent / "Config"))
    home = Path.home()
    roots = [
        home, home / "ASF", home / "ArchiSteamFarm",
        Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local")),
        Path(os.environ.get("APPDATA", home / "AppData" / "Roaming")),
        Path(os.environ.get("PROGRAMFILES", "C:/Program Files")),
        Path(os.environ.get("PROGRAMFILES(X86)", "C:/Program Files (x86)")),
        Path.cwd(),
    ]
    for root in roots:
        candidates.extend((root / "config", root / "Config", root / "ArchiSteamFarm" / "config",
                           root / "ASF" / "config", root / "ArchiSteamFarm" / "Config"))
    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        key = str(resolved).casefold()
        if key not in seen and resolved.is_dir():
            seen.add(key)
            unique.append(resolved)
    return unique



def is_asf_config_dir(config_dir: Path) -> bool:
    """Return True only when a directory contains ASF.json or a recognizable bot config."""
    if not config_dir.is_dir():
        return False
    global_config = config_dir / "ASF.json"
    if global_config.is_file():
        try:
            payload = json.loads(global_config.read_text(encoding="utf-8-sig"))
            if isinstance(payload, dict):
                return True
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            pass
    for path in config_dir.glob("*.json"):
        if path.name.casefold() == "asf.json":
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and any(str(key).casefold() in _BOT_KEYS for key in payload):
            return True
    return False


def configured_bot_names() -> list[str]:
    """Read bot names from saved ASF config or automatically discovered config folders."""
    settings = QSettings("FANFIRIEL-LANG", "GameDelivery")
    config_text = str(settings.value("asf/config_path", "") or "").strip()
    config_dirs = [Path(config_text)] if config_text and Path(config_text).is_dir() else []
    config_dirs.extend(path for path in discover_asf_roots() if path not in config_dirs)
    names: set[str] = set()
    for config_dir in config_dirs:
        for path in config_dir.glob("*.json"):
            if path.name.casefold() == "asf.json":
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(payload, dict) and any(str(key).casefold() in _BOT_KEYS for key in payload):
                names.add(path.stem)
    return sorted(names, key=str.casefold)


def populate_bot_combo(combo: QComboBox, extra_names=()) -> None:
    """Populate a bot selector from ASF config plus optional locally known bot names."""
    previous = str(combo.currentData() or "")
    names = set(configured_bot_names())
    names.update(str(name).strip() for name in extra_names if str(name).strip())
    combo.blockSignals(True)
    combo.clear()
    combo.addItem("Выбери бота из списка", "")
    for name in sorted(names, key=str.casefold):
        combo.addItem(name, name)
    index = combo.findData(previous)
    combo.setCurrentIndex(index if index >= 0 else 0)
    combo.blockSignals(False)
