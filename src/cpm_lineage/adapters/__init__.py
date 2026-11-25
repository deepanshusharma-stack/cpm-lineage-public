"""Adapters: the only place (with ``store/``) where I/O is allowed.

An adapter turns one platform's export into the neutral records of
``cpm_lineage.schema`` and declares the platform's semantics through a
``PlatformProfile``.
"""

from .base import (
    AdTypeCapability,
    BaseAdapter,
    Dataset,
    DEFAULT_PLAYBOOK,
    PlatformAdapter,
    PlatformProfile,
    PositionSemantics,
    build_records,
)

__all__ = [
    "AdTypeCapability", "BaseAdapter", "Dataset", "DEFAULT_PLAYBOOK",
    "PlatformAdapter", "PlatformProfile", "PositionSemantics", "build_records",
]
