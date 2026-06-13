# -*- coding: utf-8 -*-
"""
Snippet -> NORMALIZED TXN pipeline.

Two responsibilities:

1. ``clean_text(raw_email_bytes)`` — turn a raw RFC822 email into clean Unicode
   body text (MIME-aware: prefers text/plain, strips HTML, collapses
   whitespace). Mirrors ``email_fetch.to_clean_text`` but takes raw bytes, for
   callers that already have message bytes on hand (e.g. tests, file replay).

2. ``to_txns(snippets, cfg)`` — run each snippet through the configured bank
   adapter (``cfg["bank_adapter"]``) to get a NORMALIZED TXN, then apply the
   user's *identity* rules from ``cfg["identity"]``:
     - ``self_names``: if the counterparty looks like the user themselves, the
       transaction is re-typed as ``"internal"`` (a move between own accounts /
       own keys), not real income/expense.
     - ``own_accounts``: if the destination account hint is one of the user's
       own accounts, it's also ``"internal"``.
     - ``known_payees``: map an opaque account/phone hint to a friendly name so
       the merchant reads as a person rather than a number.

   The adapter stays personal-data-free; ALL of "who is me / who is this payee"
   lives here, driven entirely by config.

The output of ``to_txns`` is the list of NORMALIZED TXNs consumed by
``src/register.py`` (which decides sign, account routing, category and labels).
"""
import email
import email.policy
import html as htmllib
import re

from .banks import get_adapter


def clean_text(raw_email_bytes):
    """MIME bytes -> clean Unicode body text.

    Accepts ``bytes`` (raw RFC822) or ``str`` (already-decoded message). Prefers
    the ``text/plain`` part; if only HTML exists, strips tags and unescapes
    entities. Whitespace is collapsed to single spaces.
    """
    if isinstance(raw_email_bytes, str):
        raw_email_bytes = raw_email_bytes.encode("utf-8", "replace")
    msg = email.message_from_bytes(raw_email_bytes, policy=email.policy.default)
    body = msg.get_body(preferencelist=("plain", "html"))
    if body is None:
        return ""
    text = body.get_content()
    if body.get_content_type() == "text/html":
        text = re.sub(r"<[^>]+>", " ", text)
        text = htmllib.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _identity(cfg):
    ident = cfg.get("identity", {}) or {}
    self_names = [s.upper() for s in (ident.get("self_names") or [])]
    own_accounts = set(str(a) for a in (ident.get("own_accounts") or []))
    known_payees = {str(k): v for k, v in (ident.get("known_payees") or {}).items()}
    return self_names, own_accounts, known_payees


def _is_self(name, self_names):
    """True if ``name`` matches any of the user's self-identifiers (substring, case-insensitive)."""
    up = (name or "").upper()
    return any(sn and sn in up for sn in self_names)


def _account_matches(hint, own_accounts):
    """True if an account hint refers to one of the user's own accounts.

    Digit-only comparison so '*1234' and '1234' both match. The full number is
    always compared; the last-4-digits fallback is only applied to SHORT hints
    (<= 6 digits, i.e. genuine card/account stubs) to avoid a long external
    account number like ``9988776655`` falsely matching own account ``6655``
    just because it happens to end in those digits.
    """
    if not hint:
        return False
    digits = re.sub(r"\D", "", str(hint))
    if not digits:
        return False
    if digits in own_accounts:
        return True
    if len(digits) <= 6 and digits[-4:] in own_accounts:
        return True
    return False


def apply_identity(txn, cfg):
    """Apply identity rules from config to one NORMALIZED TXN (mutates and returns it).

    - A self-name counterparty -> re-typed ``internal`` (self transfer).
    - A destination hint that is one of the user's own accounts -> ``internal``.
    - A merchant/hint matching ``known_payees`` -> friendly name substituted.

    This never changes ``amount``, ``date``, ``time`` or ``currency`` — only
    ``type`` and ``merchant``. ``register.py`` decides the final sign from
    ``type``.
    """
    self_names, own_accounts, known_payees = _identity(cfg)
    merchant = txn.get("merchant", "")
    hint = txn.get("account_hint", "")

    # 1) self transfer: counterparty (or the account key behind the merchant) is the user.
    if _is_self(merchant, self_names):
        txn["type"] = "internal"
        txn["merchant"] = "Self transfer"
        return txn

    # 2) known payee: substitute a friendly name for an opaque account/phone hint.
    #    The bank's transfer merchant often reads like "Cuenta *9988776655" — pull
    #    its digits and look them up; also try the raw merchant string and the hint.
    merch_digits = re.sub(r"\D", "", merchant)
    for key in (merch_digits, merchant, str(hint)):
        if key and key in known_payees:
            txn["merchant"] = known_payees[key]
            break

    # 3) own destination account -> internal move. For transfers, the destination
    #    is encoded in the merchant ("Cuenta *NNNN"); compare its digits too.
    if txn.get("type") in ("expense", "income"):
        if _account_matches(merch_digits, own_accounts):
            txn["type"] = "internal"
            if not txn.get("merchant") or txn["merchant"].lower().startswith("cuenta"):
                txn["merchant"] = "Self transfer"
    return txn


def to_txns(snippets, cfg):
    """Parse a list of text snippets into NORMALIZED TXNs using the configured adapter.

    Steps per snippet:
      1. ``adapter.parse(snippet)`` -> NORMALIZED TXN or ``None``.
      2. If a txn, ``apply_identity`` to fold in the user's self/payee rules.

    Snippets the adapter cannot parse are silently dropped from the result, but
    returned separately so the caller can log / LLM-rescue them.

    Returns ``(txns, unparsed)`` where ``txns`` is ``list[NORMALIZED TXN]`` and
    ``unparsed`` is ``list[str]`` of snippets that produced no transaction.
    """
    adapter = get_adapter(cfg.get("bank_adapter", ""))
    txns, unparsed = [], []
    for snip in snippets:
        try:
            txn = adapter.parse(snip)
        except Exception:
            txn = None
        if txn is None:
            unparsed.append(snip)
            continue
        txns.append(apply_identity(txn, cfg))
    return txns, unparsed
