"""The recommendation log read by the difference-in-differences evaluator.

One entry per ``(account, platform, entity, day)``: what was recommended on
an entity, what its numbers were at the time, and (structured, not prose)
which changes the recommendation expected.  ``cpm_lineage.outcomes.evaluate``
later checks each entry against what happened and whether the change was
actually made.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from datetime import date
from typing import Any, Mapping

from .base import StorageBackend
from .memory import MemoryBackend

__all__ = ["ExpectedChange", "LoggedAction", "ActionLog", "ACTION_LOG", "ACCOUNT_LEVEL"]

ACTION_LOG = "action_log"
ACCOUNT_LEVEL = "*"      # entity id used by account-level runs, which are never logged


@dataclass(frozen=True)
class ExpectedChange:
    """One change a recommendation expects, in structured form.

    ``lever``: ``"budget"``, ``"bid"``, ``"keyword"`` or ``"status"``.
    ``direction``: ``"up"``, ``"down"`` or ``None`` when the recommendation
    did not commit to one.  ``targets``: the targeting units it names.
    """

    lever: str
    direction: str | None = None
    targets: tuple[str, ...] = ()
    verb: str = ""
    headline: str = ""


@dataclass(frozen=True)
class LoggedAction:
    """A logged recommendation on one entity on one day."""

    date: date
    account: str
    platform: str
    entity_id: str | None
    name: str | None = None
    status: str = "ok"                       # diagnosis status; only "ok" entries are logged
    mode: str | None = None
    tier: str | None = None
    verdict: str | None = None
    confidence: float | None = None
    roas_at: float | None = None
    spend_at: float | None = None
    budget_daily_at: float | None = None
    expected: tuple[ExpectedChange, ...] = ()


def _from_row(row: Mapping[str, Any]) -> LoggedAction:
    names = {f.name for f in fields(LoggedAction)}
    data = {k: v for k, v in row.items() if k in names}
    data["date"] = date.fromisoformat(str(data["date"])[:10])
    data["expected"] = tuple(
        ExpectedChange(lever=e.get("lever", ""), direction=e.get("direction"),
                       targets=tuple(e.get("targets") or ()), verb=e.get("verb") or "",
                       headline=e.get("headline") or "")
        for e in (row.get("expected") or ()))
    return LoggedAction(**data)


class ActionLog:
    """Append-only log of recommendations, one per entity per day."""

    def __init__(self, backend: StorageBackend | None = None, max_expected: int = 3) -> None:
        self.backend: StorageBackend = backend if backend is not None else MemoryBackend()
        self.max_expected = max_expected       # only the top few recommended changes are kept

    def log(self, entry: LoggedAction) -> bool:
        """Append an entry.  Returns False (and writes nothing) when it does not qualify.

        Not logged: a diagnosis whose status is not ``"ok"``, an
        account-level run (no entity, or the ``"*"`` entity), and a second
        entry for the same entity on the same day.  Never raises on a
        duplicate.
        """
        if entry.status != "ok" or not entry.entity_id or entry.entity_id == ACCOUNT_LEVEL:
            return False
        row = asdict(entry)
        row["expected"] = row["expected"][: self.max_expected]
        key = (entry.account, entry.platform, entry.entity_id, entry.date.isoformat())
        return self.backend.put_if_absent(ACTION_LOG, key, row)

    def entries(self, account: str, platform: str, entity_id: str | None = None) -> list[LoggedAction]:
        """Logged entries, newest first."""
        prefix = (account, platform) if entity_id is None else (account, platform, entity_id)
        out = [_from_row(r) for _k, r in self.backend.scan(ACTION_LOG, prefix)]
        out.sort(key=lambda e: (e.date, e.entity_id or ""), reverse=True)
        return out
