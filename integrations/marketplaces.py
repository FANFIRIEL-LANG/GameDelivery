"""Extensible marketplace integration contracts and registry.

Each marketplace is an adapter; UI and order workflows should not depend on a
single vendor's API or session format.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class MarketplaceInfo:
    platform_id: str
    display_name: str
    website_url: str
    implemented: bool = True


class MarketplaceAdapter(ABC):
    """Common contract implemented by each marketplace connector."""

    info: MarketplaceInfo

    @abstractmethod
    def request_login_code(self, email: str) -> dict[str, Any]:
        """Request an authentication code without storing the email or code."""
        raise NotImplementedError

    @abstractmethod
    def confirm_login_code(self, email: str, code: str) -> dict[str, Any]:
        """Confirm the emailed code and return a sanitized connection result."""
        raise NotImplementedError

    @abstractmethod
    def confirm_second_factor(self, code: str) -> dict[str, Any]:
        """Complete a marketplace's optional second-factor step."""
        raise NotImplementedError

    @abstractmethod
    def restore_session(self) -> dict[str, Any]:
        """Validate a saved session and return a sanitized profile."""
        raise NotImplementedError

    @abstractmethod
    def disconnect(self) -> dict[str, Any]:
        """Close and remove the locally saved session."""
        raise NotImplementedError

    @abstractmethod
    def fetch_orders(self) -> list[dict[str, Any]]:
        """Fetch orders in the adapter-neutral representation."""
        raise NotImplementedError


class MarketplaceRegistry:
    """Runtime registry; adding a platform does not require changing order UI."""

    def __init__(self) -> None:
        self._adapters: dict[str, MarketplaceAdapter] = {}

    def register(self, adapter: MarketplaceAdapter) -> None:
        platform_id = adapter.info.platform_id.strip().lower()
        if not platform_id:
            raise ValueError("platform_id не должен быть пустым")
        if platform_id in self._adapters:
            raise ValueError(f"Платформа уже зарегистрирована: {platform_id}")
        self._adapters[platform_id] = adapter

    def get(self, platform_id: str) -> MarketplaceAdapter:
        key = platform_id.strip().lower()
        try:
            return self._adapters[key]
        except KeyError as exc:
            raise KeyError(f"Неизвестная платформа: {key}") from exc

    def list_platforms(self) -> tuple[MarketplaceInfo, ...]:
        return tuple(adapter.info for adapter in self._adapters.values())

    def __len__(self) -> int:
        return len(self._adapters)
