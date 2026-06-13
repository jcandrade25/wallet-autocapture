#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Duplicate detection between freshly-built candidate records and the records
that already exist in Wallet.

Two kinds of duplicates are detected so that re-running the daily capture is
idempotent and never double-books a transaction:

  * **dup-exact**  -- a single existing record on the same account, within
    +/-1 calendar day, with the same absolute amount and matching type. Amounts
    are compared by absolute value because an internal transfer can appear with
    the opposite sign to the candidate (e.g. a withdrawal from one account vs.
    an income on another).
  * **dup-split**  -- the candidate's amount equals the *sum of a subset* of
    several existing records on the same account/day. This catches the common
    case where one bank alert (a total) was already entered in Wallet as
    several itemized records.

Existing records are *consumed* as they are matched, so two identical
candidates do not both match the same existing record.

Only the Python standard library is used. No personal data lives here.
"""
import datetime
from collections import defaultdict


def _to_local_day(iso, offset_hours=-5):
    """Convert an ISO ``recordDate`` to a local calendar day ``YYYY-MM-DD``.

    Wallet stores ``recordDate`` with a timezone (often UTC ``...Z``). The
    dedup buckets transactions by *local* day, so a purchase late at night
    isn't split across two UTC days. The offset defaults to UTC-5 (Colombia)
    but only matters for bucketing; the +/-1 day window absorbs the rest.
    """
    try:
        s = (iso or "").replace("Z", "+00:00")
        dt = datetime.datetime.fromisoformat(s)
        if dt.tzinfo is not None:
            dt = dt.astimezone(datetime.timezone(datetime.timedelta(hours=offset_hours)))
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return (iso or "")[:10]


def _record_type(value):
    """Sign of the amount -> 'expense' (negative) or 'income' (positive)."""
    return "expense" if value < 0 else "income"


def _normalize_existing(rec):
    """Project a raw Wallet record into the fields the matcher compares."""
    amount = rec.get("amount") or {}
    return {
        "acc": rec.get("accountId"),
        "day": _to_local_day(rec.get("recordDate", "")),
        "val": int(round(amount.get("value", 0))),
        "type": rec.get("recordType") or rec.get("type"),
        "id": rec.get("id"),
        "used": False,
    }


def _normalize_candidate(rec):
    """Project a candidate WALLET RECORD into the fields the matcher compares.

    Candidates carry their date in ``recordDate`` (ISO with offset). We bucket
    by the date portion directly -- candidates are already in local time.
    """
    amount = rec.get("amount") or {}
    value = amount.get("value", 0)
    return {
        "acc": rec.get("accountId"),
        "day": (rec.get("recordDate") or "")[:10],
        "val": int(round(value)),
        "type": _record_type(value),
    }


def _subset_sum(target, items, max_items=20):
    """Find a subset of ``items`` whose absolute values sum to ``abs(target)``.

    Args:
        target: target amount (sign ignored).
        items: list of ``(index, value)`` pairs; only values whose absolute
            value does not exceed the target are considered.
        max_items: guardrail against combinatorial blow-up; returns ``None`` if
            there are more candidate items than this.

    Returns:
        list[int] | None: the matching indices (from the input pairs) or
        ``None`` if no subset sums exactly to the target.
    """
    target = abs(target)
    vals = [(i, abs(v)) for i, v in items if 0 < abs(v) <= target]
    n = len(vals)
    if n == 0 or n > max_items:
        return None
    best = [None]

    def rec(k, remaining, chosen):
        if best[0] is not None:
            return
        if remaining == 0:
            best[0] = list(chosen)
            return
        if k >= n or remaining < 0:
            return
        chosen.append(vals[k][0])      # include item k
        rec(k + 1, remaining - vals[k][1], chosen)
        chosen.pop()
        rec(k + 1, remaining, chosen)  # exclude item k

    rec(0, target, [])
    return best[0]


def classify_dups(candidates, existing):
    """Label each candidate record as new / duplicate of an existing record.

    Args:
        candidates: list of candidate WALLET RECORD dicts (the records about to
            be created). Each needs ``accountId``, ``recordDate`` and
            ``amount.value``.
        existing: list of raw Wallet records already in the account (as
            returned by :meth:`wallet_api.Wallet.get_records`). Each needs
            ``accountId``, ``recordDate``, ``amount.value`` and a record type
            (``recordType`` or ``type``) plus an ``id``.

    Returns:
        list[tuple[str, str]]: one ``(status, detail)`` per candidate, aligned
        by index with ``candidates``. ``status`` is one of ``"new"``,
        ``"dup-exact"`` or ``"dup-split"``. ``detail`` is a short
        human-readable explanation (empty for ``"new"``).
    """
    cands = [_normalize_candidate(c) for c in candidates]

    # Index existing records by (account, local-day) so the day-pool lookup is
    # cheap. Each entry carries a mutable 'used' flag to prevent double-matching.
    idx = defaultdict(list)
    for raw in existing:
        e = _normalize_existing(raw)
        idx[(e["acc"], e["day"])].append(e)

    def day_pool(acc, day):
        """All existing records on ``acc`` within +/-1 day of ``day``."""
        try:
            d0 = datetime.date.fromisoformat(day)
        except ValueError:
            return idx.get((acc, day), [])
        pool = []
        for delta in (-1, 0, 1):
            dd = (d0 + datetime.timedelta(days=delta)).isoformat()
            pool += idx.get((acc, dd), [])
        return pool

    n = len(cands)
    status = [None] * n
    detail = [""] * n

    # Process largest amounts first so a big total isn't greedily consumed by a
    # smaller exact match before its split parts are considered.
    order = sorted(range(n), key=lambda i: -abs(cands[i]["val"]))

    # Pass 1: exact matches (compare absolute amount; allow transfer on either
    # side, since an internal transfer can carry the opposite sign).
    for i in order:
        c = cands[i]
        want = abs(c["val"])
        pool = [
            e for e in day_pool(c["acc"], c["day"])
            if not e["used"] and (e["type"] == c["type"] or e["type"] == "transfer")
        ]
        hit = next((e for e in pool if abs(e["val"]) == want), None)
        if hit:
            hit["used"] = True
            status[i] = "dup-exact"
            tag = "transfer" if hit["type"] == "transfer" else (hit["id"] or "")[:8]
            detail[i] = f"= existing {tag}".rstrip()

    # Pass 2: split matches (subset-sum over whatever is still unconsumed and of
    # the same type as the candidate).
    for i in order:
        if status[i]:
            continue
        c = cands[i]
        pool = [
            e for e in day_pool(c["acc"], c["day"])
            if not e["used"] and e["type"] == c["type"]
        ]
        sub = _subset_sum(c["val"], [(j, e["val"]) for j, e in enumerate(pool)])
        if sub and len(sub) >= 2:
            for j in sub:
                pool[j]["used"] = True
            parts = "+".join(str(int(pool[j]["val"])) for j in sub)
            status[i] = "dup-split"
            detail[i] = f"= split {parts}"
        else:
            status[i] = "new"

    return [(status[i] or "new", detail[i]) for i in range(n)]
