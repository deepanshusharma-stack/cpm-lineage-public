"""The action ledger: what was predicted, what was done, what happened.

Three tables behind a ``StorageBackend``:

* ``plans``   one row per saved plan (everything but its cards);
* ``actions`` one row per card: the full card body plus the three fields an
  operator changes (``status``, ``applied_at``, ``skip_reason``), which
  override the body on load;
* ``ledger``  one row per ``(plan, card, horizon)``, written at emit time
  for *every* card, including the ones that were vetoed or lost a conflict.
  After the horizon has elapsed the row is paired with the realised
  before / after efficiency and an outcome label
  (see ``cpm_lineage.outcomes.pair_outcomes``).

Applied rows form the *realised* set; rows that were never applied but lost
a conflict, were left in tension or were vetoed form the *counterfactual*
set, scored on whether reality moved the way the losing card said it would.
That is what lets the priority rules themselves be audited.

Outcome labels: ``On Track``, ``Review``, ``Off Track`` (the three that
train confidence), ``No signal``, ``Too early``, ``Unreadable``, and the
counterfactual ``cf-right``, ``cf-wrong``, ``cf-nosignal``.

No method reads the wall clock unless the caller omits ``today`` / ``now``;
pass them explicitly for reproducible runs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Callable, Mapping

from ..schema import AppliedAction
from .base import StorageBackend, to_plain
from .memory import MemoryBackend

__all__ = [
    "LedgerConfig", "ActionLedger", "WIN_OUTCOMES", "CF_OUTCOMES", "OUTCOMES",
    "PLANS", "ACTIONS", "LEDGER",
]

_OPERATOR_STATUSES = ("applied", "skipped")

PLANS = "plans"
ACTIONS = "actions"
LEDGER = "ledger"

WIN_OUTCOMES: tuple[str, ...] = ("On Track", "Review", "Off Track")
CF_OUTCOMES: tuple[str, ...] = ("cf-right", "cf-wrong", "cf-nosignal")
OUTCOMES: tuple[str, ...] = WIN_OUTCOMES + ("No signal", "Too early", "Unreadable") + CF_OUTCOMES


@dataclass(frozen=True)
class LedgerConfig:
    """Ledger horizons, windows and vocabularies."""

    horizons: tuple[int, ...] = (7, 14, 30)               # days after apply at which an action is measured
    markable_statuses: tuple[str, ...] = ("applied", "skipped", "pending")  # the only statuses an operator may set
    unappliable_statuses: tuple[str, ...] = ("superseded", "vetoed", "merged")  # a loser must never be recorded as executed
    counterfactual_statuses: tuple[str, ...] = ("superseded", "tension", "vetoed")  # not-taken cards worth scoring
    status_order: tuple[str, ...] = ("pending", "tension", "applied", "skipped", "superseded", "vetoed")  # load order
    win_rate_horizon: int = 7                             # only the first horizon trains confidence
    win_rate_days: int = 90                               # emitted within this many days
    cf_calibration_days: int = 120                        # counterfactual audit window
    cf_count_nosignal: bool = True                        # True: unreadable counterfactuals stay in the denominator
    accept_rate_days: int = 90                            # plans created within this many days
    applied_lookback_plans: int = 10                      # plans scanned for applied actions


def _d(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


class ActionLedger:
    """Plans, cards and the predicted-to-realised ledger for any number of accounts.

    ``account`` is the advertiser key and ``platform`` the ad platform name;
    together they namespace everything.  ``clock`` supplies "now" when a
    method is called without ``today`` / ``now``.
    """

    def __init__(
        self,
        backend: StorageBackend | None = None,
        cfg: LedgerConfig | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.backend: StorageBackend = backend if backend is not None else MemoryBackend()
        self.cfg = cfg or LedgerConfig()
        self._clock = clock or datetime.now

    # -- time -----------------------------------------------------------
    def _now(self, now: datetime | None = None) -> datetime:
        return now or self._clock()

    def _today(self, today: date | None = None) -> date:
        return today or self._clock().date()

    # -- plans and cards ------------------------------------------------
    def save_plan(self, account: str, platform: str, plan: Any, now: datetime | None = None) -> str:
        """Insert or replace a plan and all its cards; returns the plan id.

        ``plan`` is a mapping, a dataclass or an object with ``as_dict()``;
        it must carry ``id`` (or ``plan_id``) and ``actions`` (cards as
        ``ActionCard`` or dicts with at least ``id``).  Plans are ordered by
        ``created`` (taken from the plan, else today) and then by first-save
        sequence, so plan ids need not be numeric.

        Operator decisions survive a re-save.  When a card row already
        exists with an operator decision (``applied`` or ``skipped``) and the
        incoming card carries none (a regenerated plan with the same id, not
        a loaded copy), the stored ``status`` / ``applied_at`` /
        ``skip_reason`` are kept and only the card body is refreshed.  A
        card that itself carries an operator status (a plan you loaded and
        edited) writes it.  The status the pipeline gave the card is kept
        separately as ``pipeline_status`` and is what the apply guard reads.
        """
        doc = to_plain(plan)
        pid = str(doc.get("id") or doc.get("plan_id"))
        if not pid or pid == "None":
            raise ValueError("plan has no id")
        cards = doc.pop("actions", None) or []
        with self.backend.transaction():
            old = self.backend.get(PLANS, (account, platform, pid))
            seq = old["seq"] if old else len(self.backend.scan(PLANS, (account, platform))) + 1
            created = str(doc.get("created") or (old or {}).get("created") or self._now(now).date().isoformat())
            self.backend.put(PLANS, (account, platform, pid), {"plan_id": pid, "created": created, "seq": seq, "meta": doc})
            for i, card in enumerate(cards):
                aid = str(card["id"])
                key = (account, platform, pid, aid)
                prev = self.backend.get(ACTIONS, key)
                status = card.get("status") or "pending"
                row = {"action_id": aid, "pos": i, "body": card, "status": status,
                       "applied_at": card.get("applied_at"), "skip_reason": card.get("skip_reason"),
                       "pipeline_status": status}
                if status in _OPERATOR_STATUSES:
                    # a loaded copy: its pipeline status is whatever the store already knew
                    row["pipeline_status"] = (prev or {}).get("pipeline_status") or "pending"
                elif prev is not None and prev.get("status") in _OPERATOR_STATUSES:
                    # a regenerated plan must not erase what the operator did
                    row["status"], row["applied_at"] = prev["status"], prev.get("applied_at")
                    row["skip_reason"] = prev.get("skip_reason")
                    row["body"] = dict(card, status=prev["status"], applied_at=prev.get("applied_at"),
                                       skip_reason=prev.get("skip_reason"))
                self.backend.put(ACTIONS, key, row)
        return pid

    def _plan_rows(self, account: str, platform: str) -> list[dict[str, Any]]:
        rows = [r for _k, r in self.backend.scan(PLANS, (account, platform))]
        rows.sort(key=lambda r: (str(r.get("created") or ""), r.get("seq") or 0), reverse=True)
        return rows

    def _cards(self, account: str, platform: str, plan_id: str) -> list[dict[str, Any]]:
        out = []
        for _k, r in self.backend.scan(ACTIONS, (account, platform, plan_id)):
            card = dict(r["body"])
            card["status"] = r.get("status") or card.get("status")
            card["applied_at"] = r.get("applied_at")
            card["skip_reason"] = r.get("skip_reason")
            card["_pos"] = r.get("pos", 0)
            out.append(card)
        order = {s: i for i, s in enumerate(self.cfg.status_order)}
        out.sort(key=lambda c: (order.get(c.get("status"), 9), -(c.get("confidence") or 0),
                                -(c.get("stake") or 0), c.pop("_pos")))
        return out

    def load(self, account: str, platform: str, limit: int = 10) -> list[dict[str, Any]]:
        """The newest ``limit`` plans as plain dicts, newest first.

        Each plan is its saved metadata plus ``actions``: card dicts with the
        operator columns applied, sorted by status (pending, tension,
        applied, skipped, superseded, vetoed, then anything else), then
        confidence descending, then stake descending.
        """
        out = []
        for p in self._plan_rows(account, platform)[: max(int(limit), 0)]:
            doc = dict(p.get("meta") or {})
            doc.setdefault("id", p["plan_id"])
            doc["created"] = p.get("created")
            doc["actions"] = self._cards(account, platform, p["plan_id"])
            out.append(doc)
        return out

    def mark(
        self,
        account: str,
        platform: str,
        plan_id: str,
        action_id: str,
        status: str,
        reason: str | None = None,
        today: date | None = None,
    ) -> bool:
        """Record an operator decision on a card.  Returns False when refused.

        Only ``applied``, ``skipped`` and ``pending`` are accepted.  Every
        mark is refused when the pipeline left the card ``superseded``,
        ``vetoed`` or ``merged``: a conflict loser or gate-vetoed card must
        never count as executed, and it cannot be laundered through
        ``pending`` or ``skipped`` first.  Applying is also refused, inside
        one atomic scope, when the card is in a tension group and another
        card of the same plan and group is already applied (exactly one
        apply per group).

        ``pending`` (un-apply / un-skip) restores the status the pipeline
        gave the card (for example ``tension``), not a blanket ``pending``.
        ``applied_at`` is set to ``today`` for ``applied`` and cleared
        otherwise; ``skip_reason`` is kept only for ``skipped``.  The
        ledger's ``applied_at`` is synced for every horizon row that is not
        yet paired, so un-applying moves an action out of the realised set
        and back into the counterfactual set.
        """
        if status not in self.cfg.markable_statuses:
            return False
        key = (account, platform, str(plan_id), str(action_id))
        with self.backend.transaction():
            row = self.backend.get(ACTIONS, key)
            if row is None:
                return False
            emitted = row.get("pipeline_status")
            if emitted is None:                       # rows written before the column existed
                emitted = row.get("status") if row.get("status") not in _OPERATOR_STATUSES else "pending"
            if emitted in self.cfg.unappliable_statuses or row.get("status") in self.cfg.unappliable_statuses:
                return False
            if status == "applied":
                group = (row.get("body") or {}).get("tension_group")
                if group:
                    for k, other in self.backend.scan(ACTIONS, (account, platform, str(plan_id))):
                        if (k[-1] != str(action_id) and other.get("status") == "applied"
                                and (other.get("body") or {}).get("tension_group") == group):
                            return False
            applied_at = self._today(today).isoformat() if status == "applied" else None
            row["status"] = emitted if status == "pending" else status
            row["pipeline_status"] = emitted
            row["applied_at"] = applied_at
            row["skip_reason"] = reason if status == "skipped" else None
            self.backend.put(ACTIONS, key, row)
            for k, led in self.backend.scan(LEDGER, key):
                if led.get("paired_at") is None:
                    led["applied_at"] = applied_at
                    self.backend.put(LEDGER, k, led)
        return True

    # -- ledger ---------------------------------------------------------
    def record_emit(self, account: str, platform: str, plan: Any, now: datetime | None = None) -> int:
        """Write the predictions: one ledger row per card and horizon, insert-if-absent.

        Every card is recorded, including superseded and vetoed ones.  Each
        row carries ``entity_id, segment, family, direction, stake,
        status_at_emit, emitted_at, applied_at``; outcome columns start
        empty.  Existing rows are never touched.  Returns the rows written.
        """
        doc = to_plain(plan)
        pid = str(doc.get("id") or doc.get("plan_id"))
        stamp = self._now(now).isoformat(timespec="seconds")
        n = 0
        with self.backend.transaction():
            for card in doc.get("actions") or []:
                for hz in self.cfg.horizons:
                    n += self.backend.put_if_absent(LEDGER, (account, platform, pid, str(card["id"]), str(hz)), {
                        "account": account, "platform": platform, "plan_id": pid, "action_id": str(card["id"]),
                        "entity_id": card.get("entity_id"), "segment": card.get("segment"),
                        "family": card.get("family"), "direction": card.get("direction"),
                        "stake": card.get("stake"), "status_at_emit": card.get("status"),
                        "emitted_at": stamp, "applied_at": card.get("applied_at"), "horizon": hz,
                        "tide": None, "roas_before": None, "roas_after": None, "outcome": None, "paired_at": None})
        return n

    def refresh_emit(self, account: str, platform: str, plan: Any, now: datetime | None = None) -> int:
        """Re-sync the ledger with a plan's final card statuses.

        Late-born cards land (``record_emit`` again), then ``status_at_emit``
        and ``applied_at`` are updated on every row that is not yet paired.
        Paired rows are immutable.  Returns the rows updated or inserted.
        """
        doc = to_plain(plan)
        pid = str(doc.get("id") or doc.get("plan_id"))
        with self.backend.transaction():
            n = self.record_emit(account, platform, doc, now)
            for card in doc.get("actions") or []:
                for k, led in self.backend.scan(LEDGER, (account, platform, pid, str(card["id"]))):
                    if led.get("paired_at") is not None:
                        continue
                    stored = self.backend.get(ACTIONS, (account, platform, pid, str(card["id"]))) or {}
                    status = card.get("status")
                    if status in _OPERATOR_STATUSES:          # a loaded copy: keep what the pipeline emitted
                        status = stored.get("pipeline_status") or led.get("status_at_emit")
                    # the stored operator decision, not the regenerated card, says whether it was applied
                    applied_at = (stored.get("applied_at") if stored.get("status") in _OPERATOR_STATUSES
                                  else card.get("applied_at"))
                    new = (status, applied_at)
                    if (led.get("status_at_emit"), led.get("applied_at")) != new:
                        led["status_at_emit"], led["applied_at"] = new
                        self.backend.put(LEDGER, k, led)
                        n += 1
        return n

    def ledger_rows(self, account: str, platform: str, plan_id: str | None = None) -> list[dict[str, Any]]:
        """Raw ledger rows for an account (optionally one plan), sorted by plan, card, horizon."""
        prefix = (account, platform) if plan_id is None else (account, platform, str(plan_id))
        rows = [r for _k, r in self.backend.scan(LEDGER, prefix)]
        rows.sort(key=lambda r: (r["plan_id"], r["action_id"], r["horizon"]))
        return rows

    def due_pairs(
        self,
        account: str,
        platform: str,
        cf: bool = False,
        today: date | None = None,
    ) -> list[dict[str, Any]]:
        """Unpaired ledger rows whose horizon has elapsed.

        * realised (``cf=False``): ``applied_at`` set; anchor ``d0`` =
          ``applied_at``;
        * counterfactual (``cf=True``): never applied and ``status_at_emit``
          in (superseded, tension, vetoed); anchor ``d0`` = emit date.

        A row is due when ``(today - d0).days >= horizon``.  Rows without an
        entity are excluded on purpose: they can never be measured and would
        otherwise sit in front of measurable rows behind the pairing cap.
        Each returned row gains ``d0`` (ISO date) and ``counterfactual``.
        Sorted by anchor date, plan, card, horizon.
        """
        t = self._today(today)
        out = []
        for r in self.ledger_rows(account, platform):
            if r.get("paired_at") is not None or not r.get("entity_id"):
                continue
            applied = _d(r.get("applied_at"))
            if cf:
                if applied is not None or r.get("status_at_emit") not in self.cfg.counterfactual_statuses:
                    continue
                d0 = _d(r.get("emitted_at"))
            else:
                d0 = applied
            if d0 is None or (t - d0).days < int(r["horizon"]):
                continue
            out.append({**r, "d0": d0.isoformat(), "counterfactual": bool(cf)})
        out.sort(key=lambda r: (r["d0"], r["plan_id"], r["action_id"], r["horizon"]))
        return out

    def store_pair(
        self,
        account: str,
        platform: str,
        plan_id: str,
        action_id: str,
        horizon: int,
        *,
        outcome: str,
        tide: float | None = 1.0,
        roas_before: float | None = None,
        roas_after: float | None = None,
        now: datetime | None = None,
    ) -> bool:
        """Write the measured outcome for one ``(plan, card, horizon)``; pairing is permanent.

        Returns False when the row does not exist or is already paired.
        """
        key = (account, platform, str(plan_id), str(action_id), str(horizon))
        with self.backend.transaction():
            led = self.backend.get(LEDGER, key)
            if led is None or led.get("paired_at") is not None:
                return False
            led.update(tide=tide, roas_before=roas_before, roas_after=roas_after, outcome=outcome,
                       paired_at=self._now(now).isoformat(timespec="seconds"))
            self.backend.put(LEDGER, key, led)
        return True

    def paired(self, account: str, platform: str, plan_id: str) -> dict[str, dict[str, Any]]:
        """Already-measured outcomes of one plan, keyed by card id.

        Rows are read in horizon order, so the widest elapsed horizon wins.
        A local read only: calling it never triggers a measurement.
        """
        out: dict[str, dict[str, Any]] = {}
        for r in self.ledger_rows(account, platform, plan_id):
            if r.get("paired_at") is not None:
                out[r["action_id"]] = {k: r.get(k) for k in
                                       ("horizon", "tide", "roas_before", "roas_after", "outcome", "paired_at")}
        return out

    # -- rates ----------------------------------------------------------
    def _rate(self, rows: list[dict[str, Any]], win: str) -> dict[str, tuple[float, int]]:
        tally: dict[str, list[int]] = {}
        for r in rows:
            t = tally.setdefault(r.get("family") or "", [0, 0])
            t[0] += r.get("outcome") == win
            t[1] += 1
        return {f: (w / n, n) for f, (w, n) in tally.items() if n}

    def win_rates(
        self,
        account: str,
        platform: str,
        horizon: int | None = None,
        days: int | None = None,
        today: date | None = None,
    ) -> dict[str, tuple[float, int]]:
        """Realised win rate per family: ``{family: (wins / n, n)}``.

        Counts rows at ``horizon`` (default 7) that are paired, applied,
        emitted within ``days`` (default 90) and labelled ``On Track``,
        ``Review`` or ``Off Track``; a win is ``On Track``.  ``Unreadable``,
        ``No signal``, ``Too early`` and counterfactual labels never enter.
        Consumers apply their own minimum ``n`` before trusting a rate.
        """
        hz = self.cfg.win_rate_horizon if horizon is None else horizon
        cut = self._today(today) - timedelta(days=self.cfg.win_rate_days if days is None else days)
        rows = [r for r in self.ledger_rows(account, platform)
                if int(r["horizon"]) == hz and r.get("paired_at") is not None and r.get("applied_at")
                and r.get("outcome") in WIN_OUTCOMES and (_d(r.get("emitted_at")) or date.min) >= cut]
        return self._rate(rows, "On Track")

    def cf_calibration(
        self,
        account: str,
        platform: str,
        days: int | None = None,
        today: date | None = None,
    ) -> dict[str, tuple[float, int]]:
        """How often the cards that were *not* taken turned out right: ``{family: (rate, n)}``.

        ``rate = count(cf-right) / n`` over paired counterfactual rows
        emitted within ``days`` (default 120).  With
        ``cfg.cf_count_nosignal`` (the default) ``n``
        includes ``cf-nosignal`` rows, which biases the rate downward; set it
        to False to divide by readable rows only.
        """
        cut = self._today(today) - timedelta(days=self.cfg.cf_calibration_days if days is None else days)
        rows = [r for r in self.ledger_rows(account, platform)
                if r.get("paired_at") is not None and str(r.get("outcome") or "").startswith("cf-")
                and (self.cfg.cf_count_nosignal or r.get("outcome") != "cf-nosignal")
                and (_d(r.get("emitted_at")) or date.min) >= cut]
        return self._rate(rows, "cf-right")

    def accept_rates(
        self,
        account: str,
        platform: str,
        days: int | None = None,
        today: date | None = None,
    ) -> dict[str, tuple[float, int]]:
        """Operator acceptance per family: ``{family: (applied / (applied + skipped), n)}``.

        Counts cards of plans created within ``days`` (default 90).  The
        source accepted a ``days`` argument and then ignored it; the window
        is applied here as intended.
        """
        cut = self._today(today) - timedelta(days=self.cfg.accept_rate_days if days is None else days)
        tally: dict[str, list[int]] = {}
        for p in self._plan_rows(account, platform):
            if (_d(p.get("created")) or date.max) < cut:
                continue
            for _k, r in self.backend.scan(ACTIONS, (account, platform, p["plan_id"])):
                if r.get("status") in ("applied", "skipped"):
                    t = tally.setdefault((r.get("body") or {}).get("family") or "", [0, 0])
                    t[0] += r.get("status") == "applied"
                    t[1] += 1
        return {f: (a / n, n) for f, (a, n) in tally.items() if n}

    # -- history for gates ----------------------------------------------
    def applied_actions(
        self,
        account: str,
        platform: str,
        plans: int | None = None,
        exclude_plan: str | None = None,
    ) -> list[AppliedAction]:
        """Applied cards of the newest ``plans`` plans as ``AppliedAction`` records.

        ``kind`` is the card type (``"bid"``, ``"budget"`` ...).  Feed the
        result to ``verdict.open_entities`` / ``recent_bid_downs`` /
        ``owned_entities`` and to outcome pairing.  ``exclude_plan`` drops
        one plan (the action lock ignores the plan being generated).
        """
        limit = self.cfg.applied_lookback_plans if plans is None else plans
        out = []
        for p in self._plan_rows(account, platform)[:limit]:
            if exclude_plan is not None and p["plan_id"] == str(exclude_plan):
                continue
            for _k, r in self.backend.scan(ACTIONS, (account, platform, p["plan_id"])):
                when = _d(r.get("applied_at"))
                if r.get("status") != "applied" or when is None:
                    continue
                body: Mapping[str, Any] = r.get("body") or {}
                out.append(AppliedAction(kind=str(body.get("type") or ""), entity_id=body.get("entity_id"),
                                         applied_on=when, card_id=r.get("action_id"),
                                         family=body.get("family"), direction=body.get("direction")))
        return out
