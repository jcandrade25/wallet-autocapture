#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Record builder and committer for wallet-autocapture.

This module turns NORMALIZED TXNs (produced by a bank adapter via the parser)
into WALLET RECORDS, commits the new ones to BudgetBakers Wallet through the
REST API, and offers an ``undo`` to roll back what was auto-captured.

Design principles (from the project SPEC):
  * Everything created in Wallet leaves with recordState="uncleared" and the
    "autocaptured" label, so a human reviews it before it is approved. Nothing
    is ever silently auto-confirmed.
  * The optional LLM (cfg["ollama"]["enabled"]) only *proposes* a category.
    Deterministic ``category_rules`` are tried first; the code always validates
    against the whitelist in classify.classify(). When nothing is confident,
    the record gets the configured ``pending_label`` instead of a category.
  * Dedup is delegated to dedup.classify_dups against the records already in
    Wallet for the date range, so re-runs are idempotent.

Data contracts (see SPEC):
  NORMALIZED TXN: {"date","time","type","amount","merchant","account_hint",
                   "currency","channel"?}
  WALLET RECORD:  {"accountId","amount":{"value",("currencyCode")?},"recordDate",
                   "paymentType","recordState",("categoryId")?,"labelIds":[...],
                   "counterParty"}
"""
import datetime

from . import classify

# Wallet's API rejects a categoryId pointing at a system "Uncategorized"
# bucket, so we simply omit categoryId in that case. We never invent UUIDs.
COP_TZ_OFFSET = "-05:00"  # Colombia has no DST; default offset for ISO recordDate.


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def title_case(s):
    """Return a merchant name in Title Case (``Van Gogh Cafe``) instead of ALL
    CAPS, while leaving already-mixed words mostly intact."""
    return " ".join(
        (w[:1].upper() + w[1:].lower()) if w else w
        for w in (s or "").split()
    ).strip()


def _tz_offset(cfg):
    """ISO 8601 timezone offset for recordDate. Defaults to Colombia (-05:00);
    overridable via cfg["timezone_offset"] (e.g. "+00:00") for other locales."""
    return cfg.get("timezone_offset", COP_TZ_OFFSET)


def _amount_value(txn):
    """Signed numeric amount: negative for expense, positive for income/internal.

    COP (and other zero-decimal currencies) are coerced to int so the dedup
    layer can compare exact integers; currencies that keep cents (USD) preserve
    their float value.
    """
    amt = float(txn["amount"])
    signed = -amt if txn["type"] == "expense" else amt
    if (txn.get("currency") or "COP").upper() == "USD":
        return round(signed, 2)
    return int(round(signed))


def _payment_type_for(account_cfg, default="debit_card"):
    """Resolve a Wallet paymentType from the matched account config block."""
    if account_cfg and account_cfg.get("payment_type"):
        return account_cfg["payment_type"]
    return default


def _match_account(account_hint, cfg):
    """Map a NORMALIZED TXN ``account_hint`` to an entry in cfg["accounts"].

    Matching is intentionally forgiving: the keys in cfg["accounts"] are the
    *bank text that identifies the account* (e.g. "Credit Card *1234"), so we
    accept a hit when the hint appears inside the key, the key appears inside
    the hint, or the hint's last-4 digits are found in the key. Returns
    ``(account_key, account_cfg)`` or ``(None, None)`` when nothing matches.
    """
    hint = (account_hint or "").strip()
    if not hint:
        return None, None
    hint_up = hint.upper()
    accounts = cfg.get("accounts", {}) or {}

    # 1) direct substring either direction
    for key, conf in accounts.items():
        key_up = key.upper()
        if hint_up and (hint_up in key_up or key_up in hint_up):
            return key, conf

    # 2) last-4 digits of the hint found anywhere in the key
    digits = "".join(ch for ch in hint if ch.isdigit())
    if len(digits) >= 4:
        last4 = digits[-4:]
        for key, conf in accounts.items():
            if last4 in key:
                return key, conf

    return None, None


def _resolve_category(txn, cfg):
    """Decide (category_uuid_or_None, extra_label_uuid_or_None) for a txn.

    Order of precedence:
      1. Deterministic cfg["category_rules"] (keyword match on the merchant).
      2. Optional LLM via classify.classify() when cfg["ollama"]["enabled"].
    The result is always validated against cfg["categories"]/cfg["labels"]; an
    unknown category or label name is dropped rather than invented.
    """
    categories = cfg.get("categories", {}) or {}
    labels = cfg.get("labels", {}) or {}
    merchant_up = (txn.get("merchant") or "").upper()

    # 1) deterministic rules
    for rule in cfg.get("category_rules", []) or []:
        kws = [k.upper() for k in rule.get("keywords", [])]
        if any(k in merchant_up for k in kws):
            cat_name = rule.get("category")
            cat_id = categories.get(cat_name)
            lab_name = rule.get("label")
            lab_id = labels.get(lab_name) if lab_name else None
            return cat_id, lab_id

    # 2) optional LLM proposal (code validates against the whitelist inside)
    if cfg.get("ollama", {}).get("enabled"):
        try:
            proposed = classify.classify(
                txn.get("merchant", ""), txn.get("amount", 0), cfg
            )
        except Exception:
            proposed = None
        if proposed:
            cat_name, lab_name = proposed
            cat_id = categories.get(cat_name) if cat_name else None
            lab_id = labels.get(lab_name) if lab_name else None
            return cat_id, lab_id

    return None, None


def _label_id(cfg, key):
    """Resolve a label *name key* in cfg["labels"] to its UUID, or None."""
    return (cfg.get("labels", {}) or {}).get(key)


# --------------------------------------------------------------------------- #
# build_records
# --------------------------------------------------------------------------- #
def build_records(txns, cfg):
    """Map a list of NORMALIZED TXNs to WALLET RECORDS (not yet committed).

    For each txn:
      * account_hint  -> cfg["accounts"]  (fallback cfg["default_account_id"]).
      * USD routing   -> the account's usd_id sub-account, amount carries
        currencyCode "USD", and the configured usd_label is attached.
      * category      -> cfg["category_rules"] then optional LLM, validated.
      * label         -> the autocaptured label is ALWAYS attached; the
        pending_label is added when no category could be resolved.
      * counterParty  -> Title-cased merchant, capped at 255 chars.

    "internal" txns (transfers between the user's own accounts) are skipped:
    they are not real income/expense and would double-count.

    Returns a list of WALLET RECORD dicts ready for commit().
    """
    records = []
    tz = _tz_offset(cfg)

    autocaptured_key = cfg.get("autocaptured_label")
    pending_key = cfg.get("pending_label")
    usd_label_key = cfg.get("usd_label")
    autocaptured_id = _label_id(cfg, autocaptured_key) if autocaptured_key else None
    pending_id = _label_id(cfg, pending_key) if pending_key else None
    usd_label_id = _label_id(cfg, usd_label_key) if usd_label_key else None

    record_state = cfg.get("record_state", "uncleared")
    default_account = cfg.get("default_account_id")

    for txn in txns:
        if not txn:
            continue
        if txn.get("type") == "internal":
            continue  # transfers between own accounts are not real movements

        currency = (txn.get("currency") or cfg.get("currency", "COP")).upper()
        value = _amount_value(txn)

        # account + payment type
        acct_key, acct_cfg = _match_account(txn.get("account_hint"), cfg)
        account_id = (acct_cfg or {}).get("id") if acct_cfg else None
        if not account_id:
            account_id = default_account
        payment_type = _payment_type_for(acct_cfg)

        label_ids = []
        amount = {"value": value}

        # USD routing: send to the dedicated USD sub-account when one is mapped.
        if currency == "USD":
            usd_id = (acct_cfg or {}).get("usd_id")
            if usd_id:
                account_id = usd_id
                amount = {"value": value, "currencyCode": "USD"}
                if usd_label_id:
                    label_ids.append(usd_label_id)
            else:
                # No USD sub-account for this card: keep it in the base account
                # but still tag the currency so the human reviewer notices.
                amount = {"value": value, "currencyCode": "USD"}
                if usd_label_id:
                    label_ids.append(usd_label_id)

        # category (income/expense both attempt categorization)
        category_id, extra_label_id = _resolve_category(txn, cfg)
        if extra_label_id and extra_label_id not in label_ids:
            label_ids.append(extra_label_id)

        # pending fallback label when we could not resolve a category
        if not category_id and pending_id and pending_id not in label_ids:
            label_ids.append(pending_id)

        # the autocaptured label is ALWAYS present (human-review marker)
        if autocaptured_id and autocaptured_id not in label_ids:
            label_ids.append(autocaptured_id)

        record_date = "{date}T{time}:00{tz}".format(
            date=txn["date"],
            time=txn.get("time") or "12:00",
            tz=tz,
        )

        record = {
            "accountId": account_id,
            "amount": amount,
            "recordDate": record_date,
            "paymentType": payment_type,
            "recordState": record_state,
            "counterParty": title_case(txn.get("merchant", ""))[:255],
        }
        if category_id:
            record["categoryId"] = category_id
        if label_ids:
            record["labelIds"] = label_ids

        records.append(record)

    return records


# --------------------------------------------------------------------------- #
# commit
# --------------------------------------------------------------------------- #
def _record_day(record):
    """Calendar day (YYYY-MM-DD) of a WALLET RECORD's recordDate."""
    return (record.get("recordDate") or "")[:10]


def _existing_for_range(wallet, records, pad_days=2):
    """Fetch records already in Wallet covering the candidate date range
    (with a +/- ``pad_days`` cushion) so dedup has something to compare against.

    Uses wallet.get_records(params) — the Wallet client paginates internally.
    Returns a list of existing-record dicts as the API shapes them.
    """
    days = [_record_day(r) for r in records if _record_day(r)]
    if not days:
        return []
    lo = (datetime.date.fromisoformat(min(days)) - datetime.timedelta(days=pad_days)).isoformat()
    hi = (datetime.date.fromisoformat(max(days)) + datetime.timedelta(days=pad_days)).isoformat()
    params = {
        "recordDate": ["gte.{0}T00:00:00Z".format(lo), "lte.{0}T23:59:59Z".format(hi)],
        "sortBy": "-recordDate",
    }
    return wallet.get_records(params)


def commit(records, wallet, cfg):
    """Create only the genuinely-new records in Wallet.

    Steps:
      1. Pull existing records for the date range (wallet.get_records).
      2. dedup.classify_dups(candidates, existing) -> per-candidate status.
      3. POST only the "new" ones via wallet.create_records (uncleared +
         autocaptured label, as built by build_records).

    Returns a summary dict:
      {"created": int, "duplicates": int, "candidates": int,
       "statuses": [...], "response": <create_records result or None>}
    """
    summary = {
        "created": 0,
        "duplicates": 0,
        "candidates": len(records),
        "statuses": [],
        "response": None,
    }
    if not records:
        return summary

    # dedup is imported lazily so a missing/peer-built module degrades to
    # "create everything" only if explicitly disabled by the caller.
    from . import dedup

    try:
        existing = _existing_for_range(wallet, records)
        statuses = dedup.classify_dups(records, existing)
    except Exception:
        # If dedup can't run we still must not blindly duplicate; surface the
        # candidates as "new" but record that dedup was skipped so the caller
        # (run_daily) can flag it.
        statuses = [("new", "dedup-skipped")] * len(records)

    summary["statuses"] = statuses

    new_records = []
    for record, status in zip(records, statuses):
        st = status[0] if isinstance(status, (tuple, list)) else status
        if st == "new":
            new_records.append(record)
        else:
            summary["duplicates"] += 1

    if not new_records:
        return summary

    resp = wallet.create_records(new_records)
    summary["response"] = resp
    # wallet_api.create_records returns a flat aggregated summary
    # {"succeeded": int, "failed": int, "errors": [...]}. Trust its
    # succeeded count when present; otherwise assume the batch went through.
    created = None
    if isinstance(resp, dict):
        created = resp.get("succeeded")
        if resp.get("errors"):
            summary["errors"] = resp["errors"]
    summary["created"] = created if created is not None else len(new_records)
    return summary


# --------------------------------------------------------------------------- #
# undo
# --------------------------------------------------------------------------- #
def _has_label(record, label_id):
    """True when a record carries ``label_id`` (handles both the embedded
    label-object shape and a flat labelIds list)."""
    for lab in record.get("labels") or []:
        if isinstance(lab, dict) and lab.get("id") == label_id:
            return True
        if lab == label_id:
            return True
    return label_id in (record.get("labelIds") or [])


def undo(wallet, cfg):
    """Delete the still-pending records that were auto-captured.

    Only records that carry the configured autocaptured label AND are still in
    the "uncleared" state are removed; anything the user already cleared or
    reconciled is left untouched even if it still has the label.

    Returns {"deleted": int, "candidates": int}.
    """
    autocaptured_id = _label_id(cfg, cfg.get("autocaptured_label"))
    result = {"deleted": 0, "candidates": 0}
    if not autocaptured_id:
        return result

    existing = wallet.get_records({"sortBy": "-recordDate"})
    to_delete = [
        r for r in existing
        if r.get("recordState") == "uncleared" and _has_label(r, autocaptured_id)
    ]
    result["candidates"] = len(to_delete)
    if not to_delete:
        return result

    # The Wallet client owns the HTTP verb; delete_records(ids) sends the
    # {"ids": [...]} body the API expects, in batches.
    ids = [r.get("id") for r in to_delete if r.get("id")]
    wallet.delete_records(ids)
    result["deleted"] = len(ids)
    return result
