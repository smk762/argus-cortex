"""The name a peer goes by, shared by :mod:`argus_cortex.peers` and :mod:`argus_cortex.service`.

Standard library only: :mod:`argus_cortex.peers` must stay importable without pydantic, and
:mod:`argus_cortex.service` must not import :mod:`argus_cortex.peers` for it.
"""

from __future__ import annotations

__all__ = ["PEER_NAME_PATTERN"]

PEER_NAME_PATTERN = r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$"
"""A peer name: lower-case letters and digits, in words joined by single hyphens (``nudenet``, ``echo-xtts``)."""
