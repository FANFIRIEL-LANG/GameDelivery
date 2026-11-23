"""Strict normalization for opaque physical inventory asset identifiers."""

from __future__ import annotations

from typing import Any


def validate_asset_id(value: Any) -> str:
    """Accept only a real string asset ID and return its trimmed form."""
    if type(value) is not str:
        raise ValueError("asset_id должен быть непустой строкой")
    normalized = value.strip()
    if not normalized:
        raise ValueError("asset_id должен быть непустой строкой")
    return normalized


def normalize_asf_asset_id(value: Any) -> str:
    """Normalize ASF JSON AssetID values at the external response boundary.

    SteamKit/ASF may serialize the UInt64 AssetID as a JSON number, while
    internal GameDelivery manifests intentionally keep IDs as strings.
    """
    if isinstance(value, int) and not isinstance(value, bool):
        if 0 < value <= 0xFFFFFFFFFFFFFFFF:
            return str(value)
        raise ValueError("asset_id должен быть положительным UInt64")
    return validate_asset_id(value)


def asset_id_with_key(value: Any) -> tuple[str, str]:
    """Return the preserved validated ID and its reservation comparison key.

    Steam asset IDs are opaque strings, but ASCII decimal representations of
    the same positive integer identify the same resource.  Keep the original
    spelling for manifests and persistence while comparing those IDs without
    leading zeroes.  Zero-only IDs intentionally retain their exact spelling.
    """
    asset_id = validate_asset_id(value)
    if asset_id and all("0" <= char <= "9" for char in asset_id):
        significant = asset_id.lstrip("0")
        if significant:
            return asset_id, significant
    return asset_id, asset_id


def asset_id_key(value: Any) -> str:
    """Return the validated reservation comparison key for an asset ID."""
    return asset_id_with_key(value)[1]
