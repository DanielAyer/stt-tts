"""Zenoh channel discovery.

Zenoh has no central "channel list", but a router exposes an admin space
under `@/` that can be queried with an ordinary get:

    @/<router-id>/router/subscriber/**   key expressions with live subscribers
    @/<router-id>/router/publisher/**    key expressions with declared publishers
    @/<router-id>/router/queryable/**    key expressions with queryables

Limits, documented in the README:
- Requires a zenohd router; peer-to-peer meshes have no admin space to ask.
- The router's admin space must be readable (it is by default).
- Only keys with a live subscriber/publisher/queryable appear. A topic that is
  published with plain put() and has no declared publisher will not show up.
- The admin-space layout has varied between Zenoh versions.
"""

from __future__ import annotations

from dataclasses import dataclass

from .adapters.zenoh_adapter import open_session

KINDS = ("subscriber", "publisher", "queryable")


@dataclass(frozen=True)
class Channel:
    key: str
    kind: str  # subscriber | publisher | queryable


def parse_admin_key(admin_key: str) -> Channel | None:
    """'@/<zid>/router/subscriber/voice/in/audio' -> Channel('voice/in/audio', 'subscriber')."""
    parts = admin_key.split("/")
    if len(parts) < 5 or parts[0] != "@" or parts[2] != "router" or parts[3] not in KINDS:
        return None
    return Channel(key="/".join(parts[4:]), kind=parts[3])


def discover(cfg: dict, timeout_s: float = 2.0) -> list[Channel]:
    """Return the channels a reachable router knows about, sorted by key."""
    session = open_session(cfg)
    found: set[Channel] = set()
    try:
        for kind in KINDS:
            replies = session.get(f"@/*/router/{kind}/**", timeout=timeout_s)
            for reply in replies:
                if reply.ok is None:
                    continue
                channel = parse_admin_key(str(reply.ok.key_expr))
                if channel:
                    found.add(channel)
    finally:
        session.close()
    return sorted(found, key=lambda c: (c.key, c.kind))


def unique_keys(channels: list[Channel]) -> list[str]:
    return sorted({c.key for c in channels if not c.key.startswith("@")})
