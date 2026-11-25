"""Persistence: the action ledger, the history store and the change log.

Three different things that are easy to confuse, each with its own name:

* ``ActionLedger``: plans, cards and the predicted-to-realised ledger that
  trains confidence (``ledger.py``);
* ``HistoryStore``: keyword / product / geo history across all campaigns,
  used as a cold-start prior (``history.py``);
* ``ChangeLog``: configuration changes and the before / after read around
  them (``changelog.py``).

Plus ``ActionLog``, the per-entity recommendation log read by the
difference-in-differences evaluator.  All four sit on a ``StorageBackend``;
``MemoryBackend`` and ``SQLiteBackend`` are provided.  This package and
``adapters`` are the only places in the library that perform I/O.
"""

from .actionlog import ACCOUNT_LEVEL, ActionLog, ExpectedChange, LoggedAction
from .base import Key, StorageBackend, to_plain
from .changelog import ChangeLog, ChangeLogConfig, ConfigVersion, diff_versions, input_change_ledger
from .history import (
    HistoryConfig, HistoryStore, build_entity_history, geo_history, keyword_history, product_history,
)
from .ledger import CF_OUTCOMES, OUTCOMES, WIN_OUTCOMES, ActionLedger, LedgerConfig
from .memory import MemoryBackend
from .sqlite import SQLiteBackend

__all__ = [
    "Key", "StorageBackend", "MemoryBackend", "SQLiteBackend", "to_plain",
    "ActionLedger", "LedgerConfig", "WIN_OUTCOMES", "CF_OUTCOMES", "OUTCOMES",
    "HistoryStore", "HistoryConfig", "build_entity_history",
    "keyword_history", "product_history", "geo_history",
    "ChangeLog", "ChangeLogConfig", "ConfigVersion", "diff_versions", "input_change_ledger",
    "ActionLog", "LoggedAction", "ExpectedChange", "ACCOUNT_LEVEL",
]
