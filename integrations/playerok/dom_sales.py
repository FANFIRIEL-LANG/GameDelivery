"""Pure, fail-closed parser for the observed Playerok sales-card DOM.

This module deliberately returns route IDs as candidates only. It has no Qt,
WebEngine, network, GraphQL, persistence, or delivery dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
import re
from typing import Literal
from urllib.parse import urlsplit


_UUID_PATH_RE = re.compile(
    r"^/deal/([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$"
)
_ALLOWED_HOSTS = frozenset({"playerok.com", "www.playerok.com"})
_MAX_HREF_LENGTH = 2048


@dataclass(frozen=True, slots=True)
class PlayerokDealDomCandidate:
    """An unverified UUID-shaped value from an observed ``/deal/{UUID}`` link."""

    candidate_deal_id: str
    listing_id: None = None
    chat_id: None = None


@dataclass(frozen=True, slots=True)
class PlayerokSalesDomSnapshot:
    """Partial DOM parse; this parser can never establish list completeness."""

    candidates: tuple[PlayerokDealDomCandidate, ...]
    snapshot_complete: Literal[False] = False


class _AnchorHrefCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hrefs: list[str | None] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        href = next((value for name, value in attrs if name.lower() == "href"), None)
        self.hrefs.append(href)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)


def _candidate_from_href(href: str | None) -> str | None:
    if not isinstance(href, str) or not href or len(href) > _MAX_HREF_LENGTH:
        return None

    try:
        parsed = urlsplit(href)
        if parsed.scheme or parsed.netloc:
            if (
                parsed.scheme.lower() != "https"
                or parsed.hostname not in _ALLOWED_HOSTS
                or parsed.username is not None
                or parsed.password is not None
                or parsed.port not in (None, 443)
            ):
                return None
        elif not href.startswith("/") or href.startswith("//"):
            return None
    except ValueError:
        return None

    match = _UUID_PATH_RE.fullmatch(parsed.path)
    return match.group(1) if match else None


def parse_playerok_sales_dom(html: str) -> PlayerokSalesDomSnapshot:
    """Extract unique deal-link candidates from HTML without asserting identity.

    Only anchors whose path is exactly ``/deal/{UUID}`` are considered. Unknown
    markup and attributes are ignored. Duplicate UUID-shaped links are returned
    once in first-seen order. ``listing_id`` and ``chat_id`` always remain null,
    and ``snapshot_complete`` is always false regardless of card count or markup.
    """

    if not isinstance(html, str):
        raise TypeError("html must be str")

    collector = _AnchorHrefCollector()
    collector.feed(html)
    collector.close()

    candidates: list[PlayerokDealDomCandidate] = []
    seen: set[str] = set()
    for href in collector.hrefs:
        candidate_id = _candidate_from_href(href)
        if candidate_id is None:
            continue
        key = candidate_id.casefold()
        if key in seen:
            continue
        seen.add(key)
        candidates.append(PlayerokDealDomCandidate(candidate_deal_id=candidate_id))

    return PlayerokSalesDomSnapshot(candidates=tuple(candidates))