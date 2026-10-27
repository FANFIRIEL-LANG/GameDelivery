from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QComboBox


_BOT_KEYS = {
    "enabled", "steamlogin", "steampassword", "steamtradetoken",
    "webproxy", "webproxyusername", "webproxypassword", "botbehaviour",
    "steamuserpermissions", "steamparentalsettings", "customizedtags",
    "gamesplayedwhileidle", "lootabletypes",
}


def configured_bot_names() -> list[str]:
    """Read bot names from the ASF config directory saved by the ASF settings page."""
    settings = QSettings("FANFIRIEL-LANG", "GameDelivery")
    config_text = str(settings.value("asf/config_path", "") or "").strip()
    config_dir = Path(config_text) if config_text else None
    if config_dir is None or not config_dir.is_dir():
        return []
    names = []
    for path in config_dir.glob("*.json"):
        if path.name.casefold() == "asf.json":
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and any(str(key).casefold() in _BOT_KEYS for key in payload):
            names.append(path.stem)
    return sorted(set(names), key=str.casefold)


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
