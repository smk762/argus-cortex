"""Content hashes the stages compare and record: one spelling of sha256, so every stage writes the same digest."""

from __future__ import annotations

import hashlib


def sha256_hex(data: bytes) -> str:
    """The sha256 of *data* as 64 lowercase hex characters."""
    return hashlib.sha256(data).hexdigest()
