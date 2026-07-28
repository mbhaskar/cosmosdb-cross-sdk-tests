"""Topology synthesizer for the mitmproxy addon (pure, no mitmproxy import).

WHY
---
Testing the SDK's FeedRange / PartitionKeyRange cache normally needs a real
multi-partition account: enough data/RU to force the service to split into many
physical partitions, so ``ReadPartitionKeyRanges`` returns multiple ranges over
multiple pages. That's expensive and slow.

Instead, this engine lets the mitmproxy addon **fabricate** a multi-range,
multi-page ``/pkranges`` response over a SINGLE-partition emulator. The real SDK
then exercises its actual routing-map / feed-range cache: fetch pkranges, follow
``x-ms-continuation`` across pages, assemble the full range set, and cache it.

This is gateway-mode only (mitmproxy is HTTP; direct/rntbd is binary TCP) and it
validates the SDK's cache/pagination/assembly logic -- not real storage range
boundaries (a real account stays the gold standard for those).

HOW THE PAGES WORK
------------------
Arm with ``ranges=N`` and (optionally) ``page_size=P``. A ``/pkranges`` read then
returns P ranges per page; every page but the last carries an ``x-ms-continuation``
token (just the next offset). The SDK echoes that token in the ``x-ms-continuation``
request header on the follow-up call, which we parse to serve the next slice.
The collection ``_rid`` is taken from the request path so the SDK keys its cache
correctly; the ETag is stable across pages so the assembled map is consistent.

Pure and side-effect-free (apart from armed-state), so it unit-tests without
mitmproxy.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

# Global EPK bounds: "" is the min key, "FF" the max (single-byte hex form the
# routing map accepts). Intermediate boundaries are evenly-spaced hex bytes.
_MIN = ""
_MAX = "FF"


def _boundary(k: int, m: int) -> str:
    """The k-th of m+1 EPK boundaries. b0="" (global min), bm="FF" (global max),
    interior boundaries are evenly-spaced 2-hex-digit bytes, strictly increasing."""
    if k <= 0:
        return _MIN
    if k >= m:
        return _MAX
    return format(min(255, round(k * 256 / m)), "02X")


class TopologyEngine:
    """Holds the armed synthetic topology and renders one ``/pkranges`` page."""

    def __init__(self) -> None:
        self._armed = False
        self._ranges = 1
        self._page_size = 0  # 0 => all ranges in a single page

    # -- arming / clearing ------------------------------------------------- #
    def arm(self, query: Dict[str, str]) -> Dict[str, Any]:
        """Arm from a control-channel query mapping. Keys: ``ranges`` (>=1),
        ``page_size`` (0/absent => single page)."""
        ranges = int(query.get("ranges", "4") or "4")
        self._ranges = max(1, ranges)
        page = query.get("page_size")
        self._page_size = int(page) if page not in (None, "") else 0
        self._armed = True
        return self.status()

    def clear(self) -> Dict[str, Any]:
        self._armed = False
        return self.status()

    @property
    def armed(self) -> bool:
        return self._armed

    # -- request classification ------------------------------------------- #
    @staticmethod
    def is_pkranges(path: str) -> bool:
        """True for a ReadPartitionKeyRanges request (``.../pkranges``)."""
        return "/pkranges" in path.split("?", 1)[0].lower()

    @staticmethod
    def collection_rid(path: str) -> str:
        """Extract the collection rid from ``/dbs/<x>/colls/<rid>/pkranges``.
        Falls back to a stable placeholder if the path isn't shaped as expected."""
        parts = [p for p in path.split("?", 1)[0].split("/") if p]
        if "colls" in parts:
            i = parts.index("colls")
            if i + 1 < len(parts):
                return parts[i + 1]
        return "SYNTHtopology=="

    @staticmethod
    def parse_offset(continuation: Optional[str]) -> int:
        """Our continuation token is just the next range offset as decimal."""
        if not continuation:
            return 0
        try:
            return max(0, int(str(continuation).strip().strip('"')))
        except (TypeError, ValueError):
            return 0

    # -- page rendering ---------------------------------------------------- #
    def _range(self, i: int, coll_rid: str, etag: str) -> Dict[str, Any]:
        return {
            "_rid": f"{coll_rid}-pk{i}",
            "id": str(i),
            "minInclusive": _boundary(i, self._ranges),
            "maxExclusive": _boundary(i + 1, self._ranges),
            "ridPrefix": i,
            "throughputFraction": round(1.0 / self._ranges, 6),
            "status": "online",
            "parents": [],
            "_self": f"dbs/SYNTH/colls/{coll_rid}/pkranges/{i}/",
            "_etag": etag,
            "_ts": 0,
            "lsn": 1,
        }

    def page(self, offset: int, coll_rid: str, etag: str
             ) -> Tuple[bytes, Optional[str], int]:
        """Render the page starting at ``offset``.

        Returns ``(body_bytes, next_continuation, item_count)`` where
        ``next_continuation`` is ``None`` on the final page.
        """
        size = self._page_size if self._page_size > 0 else self._ranges
        offset = max(0, min(offset, self._ranges))
        end = min(offset + size, self._ranges)
        ranges: List[Dict[str, Any]] = [self._range(i, coll_rid, etag) for i in range(offset, end)]
        body = json.dumps({
            "_rid": coll_rid,
            "PartitionKeyRanges": ranges,
            "_count": len(ranges),
        }).encode()
        next_cont = str(end) if end < self._ranges else None
        return body, next_cont, len(ranges)

    def status(self) -> Dict[str, Any]:
        return {
            "armed": self._armed,
            "ranges": self._ranges,
            "page_size": self._page_size,
        }

    def etag(self) -> str:
        """Stable ETag for the currently-armed topology. The SDK drains pkranges
        with change-feed semantics: it re-reads with ``If-None-Match: <etag>`` and
        expects ``304 Not Modified`` once it already holds this topology. A stable
        etag (unchanged across pages/drains) is what lets that drain terminate."""
        return f'"topo-{self._ranges}"'

    @staticmethod
    def etag_matches(if_none_match: Optional[str], etag: str) -> bool:
        """True if the request's If-None-Match denotes the given etag (tolerant of
        surrounding quotes/whitespace/``W/`` weak-validator prefixes)."""
        if not if_none_match:
            return False

        def _norm(v: str) -> str:
            v = v.strip()
            if v.startswith("W/"):
                v = v[2:]
            return v.strip().strip('"')

        return _norm(if_none_match) == _norm(etag)
