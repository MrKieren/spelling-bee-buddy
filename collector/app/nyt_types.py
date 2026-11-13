from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Snapshot:
    """What one sync read from NYT (private: includes the full answer list)."""

    game: dict
    found: set[str]
    logged_in: bool
    found_source: str
