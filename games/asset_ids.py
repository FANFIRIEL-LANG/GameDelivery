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
