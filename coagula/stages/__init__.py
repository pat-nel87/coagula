"""Stage implementations. See SPEC §6."""

from __future__ import annotations

from .dedup import Dedup
from .normalize import Normalize
from .prune import Prune

__all__ = ["Normalize", "Dedup", "Prune"]
