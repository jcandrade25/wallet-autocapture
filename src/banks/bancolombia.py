# -*- coding: utf-8 -*-
"""
Bancolombia / RappiPay bank adapter — REFERENCE implementation.

A bank adapter understands ONE thing: the *text format* of a given bank's
transaction-alert emails. It turns one alert snippet into a NORMALIZED TXN
(see the contract below) or returns ``None`` when the text is not a
transaction we can read.

What an adapter MUST NOT do
---------------------------
- It must NOT know anything about *you*. No real names, no account numbers,
  no payee phone numbers, no tokens. Those live in ``config.json`` under
  ``identity`` and are applied later by ``src/parser.py``. The adapter only
  reports raw facts pulled from the email text (e.g. ``account_hint`` is the
  last 4 digits the bank printed, ``merchant`` is the literal counterparty
  string). Mapping "is this me?" / "who is account *1234?" is the parser's job.

NORMALIZED TXN contract (the dict ``parse`` returns)
----------------------------------------------------
    {
      "date":         "YYYY-MM-DD",            # required
      "time":         "HH:MM",                 # required (default "12:00")
      "type":         "expense"|"income"|"internal",
      "amount":       <float, always positive>, # sign is decided later by register.py
      "merchant":     "<counterparty / destination as printed>",
      "account_hint": "<last 4 digits or text that identifies the account>",
      "currency":     "COP"|"USD",
      "channel":      "<optional free text: pos, web, qr, transfer, ...>",
    }
    or ``None`` if the snippet is not a transaction (marketing, statement,
    account-enrollment confirmations, ...).

SENDERS is the list of From-substrings the email layer searches for. The
generic IMAP layer (``src/email_fetch.py``) reads its sender list from
``config.email.senders``; SENDERS here is the adapter's *suggested* default
and is also what ``setup_wizard`` can seed the config with.
"""
import re

# From-address substrings that carry Bancolombia / RappiPay transaction alerts.
# "bancolombia" matches both an.notificacionesbancolombia.com and bancolombia.com.co;
# "rappipay" matches noreply@rappipay.co.
SENDERS = ["bancolombia", "rappipay"]

# --- reusable sub-patterns -------------------------------------------------
DATE = r"(\d{2}/\d{2}/\d{2,4})"   # DD/MM/YY or DD/MM/YYYY
TIME = r"(\d{2}:\d{2})"           # HH:MM
MONEY = r"\$?\s*([\d.,]+)"        # an amount with optional $, dots and commas


def parse_amount(s):
    """Parse a Colombian/European-formatted COP amount string to an int (pesos).

    Bancolombia prints amounts in THREE shapes — this is a historical source of
    bugs, so the rule is explicit:

      1. ``COP55.500,00``  -> the COMMA is the decimal separator and the dot is
         a thousands separator. We keep only the integer part before the comma
         and strip the dots: -> 55500.
      2. ``$134029.00``    -> a trailing ``.dd`` with no thousands grouping is a
         DECIMAL point. We drop the ``.00`` (pesos have no cents in practice)
         and strip any dots: -> 134029.
      3. ``$23000`` / ``$7.318.703`` -> plain integers where every dot is a
         thousands separator: -> 23000 / 7318703.

    Returns an ``int`` number of pesos.
    """
    raw = s.strip().rstrip(".")
    if "," in raw:                       # case 1: comma = decimal
        return int(raw.split(",")[0].replace(".", "") or 0)
    if re.search(r"\.\d{2}$", raw):      # case 2: trailing .dd = decimal point
        return int(raw[:-3].replace(".", "") or 0)
    return int(raw.replace(".", "") or 0)  # case 3: dots = thousands


def parse_usd(s):
    """Parse a USD amount that uses European decimals, e.g. ``USD1,00`` -> 1.00.

    Dots are thousands separators, the comma is the decimal point.
    """
    return float(s.strip().rstrip(".").replace(".", "").replace(",", "."))


def _fix_date(d):
    """Normalize ``DD/MM/YY`` or ``DD/MM/YYYY`` -> ``YYYY-MM-DD``."""
    dd, mm, yy = d.split("/")
    if len(yy) == 2:
        yy = "20" + yy
    return f"{yy}-{mm}-{dd}"


def _txn(date, time, type_, amount, merchant, account_hint, currency, channel):
    """Build a NORMALIZED TXN dict (single place that defines the shape)."""
    return {
        "date": _fix_date(date),
        "time": time,
        "type": type_,
        "amount": float(amount),
        "merchant": (merchant or "").strip(),
        "account_hint": account_hint or "",
        "currency": currency,
        "channel": channel,
    }


def parse(text):
    """Turn one alert snippet into a NORMALIZED TXN, or ``None``.

    Each branch matches one Bancolombia/RappiPay alert phrasing. Order matters:
    the more specific patterns are tried first. ``type`` here is purely about
    the *direction of money* as the bank describes it; deciding whether an
    "income"/"expense" is actually an internal transfer between the user's own
    accounts is left to ``src/parser.py`` (it has ``identity.self_names`` /
    ``own_accounts``). The one exception is a credit-card payment ("Pagaste ...
    en la tarjeta de credito ... desde la cuenta ..."), which is *structurally*
    internal regardless of identity, so we mark it ``internal`` here.
    """
    s = re.sub(r"\s+", " ", text or "").strip()
    if not s:
        return None

    # --- USD purchase: "Compraste USD1,00 en X ... el DD/MM/YYYY a las HH:MM ... T.Cred *NNNN" ---
    m = re.search(
        r"Compraste USD([\d.,]+) en (.+?)(?:,| con tu T\.?Cred)?(?: \*?(\d{4}))?, el "
        + DATE + r" a las " + TIME, s)
    if m:
        usd, com, acc, d, h = m.groups()
        if not acc:
            m2 = re.search(r"T\.?Cred \*(\d{4})", s)
            acc = m2.group(1) if m2 else ""
        return _txn(d, h, "expense", parse_usd(usd), com, acc, "USD", "web")
    m = re.search(
        r"Compraste USD([\d.,]+) en (.+?), el " + DATE + r" a las " + TIME
        + r".*?T\.?Cred \*(\d{4})", s)
    if m:
        usd, com, d, h, acc = m.groups()
        return _txn(d, h, "expense", parse_usd(usd), com, acc, "USD", "web")

    # --- COP purchase (card present / app): "Compraste COP55.500,00 en X con tu T.Cred *NNNN, el ... a las ..." ---
    m = re.search(
        r"Compraste COP([\d.,]+) en (.+?) con tu T\.?Cred \*(\d{4}), el "
        + DATE + r" a las " + TIME, s)
    if m:
        v, com, acc, d, h = m.groups()
        return _txn(d, h, "expense", parse_amount(v), com, acc, "COP", "pos")
    # variant: "... el ... a las ... Esta compra esta asociada a T.Cred *NNNN."
    m = re.search(
        r"Compraste COP([\d.,]+) en (.+?), el " + DATE + r" a las " + TIME
        + r".*?asociada a T\.?Cred \*(\d{4})", s)
    if m:
        v, com, d, h, acc = m.groups()
        return _txn(d, h, "expense", parse_amount(v), com, acc, "COP", "app")

    # --- Credit-card payment (structurally internal): "Pagaste $X en la tarjeta de credito *NNNN desde la cuenta *MMMM, el DD/MM/YYYY HH:MM" ---
    m = re.search(
        r"Pagaste " + MONEY + r" en la tarjeta de credito \*(\d{4}) desde la cuenta \*(\d{4}), el "
        + DATE + r"\s+" + TIME, s)
    if m:
        v, tc, cta, d, h = m.groups()
        return _txn(d, h, "internal", parse_amount(v), f"Pago TC *{tc}", cta, "COP", "transfer")

    # --- QR payment (expense): "...pagaste $X por codigo QR desde tu cuenta *NNNN a la llave LLLL el DD/MM/YYYY a las HH:MM" ---
    m = re.search(
        r"pagaste " + MONEY + r" por codigo QR desde tu cuenta \*(\d{4}) a la llave (\S+) el "
        + DATE + r" a las " + TIME, s, re.I)
    if m:
        v, cta, key, d, h = m.groups()
        return _txn(d, h, "expense", parse_amount(v), f"QR {key}", cta, "COP", "qr")

    # --- Incoming transfer: variant "de NOMBRE por $X en tu cuenta *NNNN ... el DD/MM/YY a las HH:MM" ---
    m = re.search(
        r"recibiste una transferencia de (.+?) por " + MONEY
        + r" en tu cuenta \*?\*?(\d{4}).*?el " + DATE + r" a las? " + TIME, s, re.I)
    if m:
        nom, v, cta, d, h = m.groups()
        return _txn(d, h, "income", parse_amount(v), nom, cta, "COP", "transfer")
    # variant "por $X de NOMBRE en tu cuenta **NNNN, el DD/MM/YYYY a las HH:MM"
    m = re.search(
        r"Recibiste una transferencia por " + MONEY + r" de (.+?) en tu cuenta \*?\*?(\d{4}), el "
        + DATE + r" a las " + TIME, s)
    if m:
        v, nom, cta, d, h = m.groups()
        return _txn(d, h, "income", parse_amount(v), nom, cta, "COP", "transfer")

    # --- Incoming payroll/PSE payment: "Recibiste un pago por $X de NOMBRE a tu cuenta AHORROS, el HH:MM a las DD/MM/YYYY" ---
    # NOTE the bank prints time and date INVERTED here, so we detect which is which.
    m = re.search(
        r"Recibiste un pago por " + MONEY + r" de (.+?) a tu cuenta AHORROS, el (\S+) a las (\S+)", s)
    if m:
        v, nom, p1, p2 = m.groups()
        d = p2 if "/" in p2 else p1
        h = p1 if ":" in p1 else p2
        return _txn(d.rstrip("."), h.rstrip("."), "income", parse_amount(v),
                    nom, "AHORROS", "COP", "transfer")

    # --- Card payment received via PSE/Wompi (internal): "Recibimos pago por $X a tu tarjeta de credito **NNNN ..." ---
    m = re.search(
        r"Recibimos pago por " + MONEY + r" a tu tarjeta de credito \*\*?(\d{4})", s)
    if m:
        v, tc = m.groups()
        md = re.search(DATE + r"\s+" + TIME, s) or re.search(DATE, s)
        d = md.group(1) if md else ""
        h = md.group(2) if (md and md.lastindex and md.lastindex >= 2) else "12:00"
        if not d:
            return None
        return _txn(d, h, "internal", parse_amount(v), f"Pago TC *{tc} (PSE)", tc, "COP", "transfer")

    # --- Outgoing transfer to an ACCOUNT: "Transferiste $X desde tu cuenta *NNNN a la cuenta *MMMM el DD/MM/YYYY a las HH:MM" ---
    # We can't know if the destination is the user's own account here (that's
    # identity, handled by parser.py via own_accounts/known_payees), so we
    # report it as an expense with the destination account as both merchant and hint.
    m = re.search(
        r"Transferiste " + MONEY + r" desde tu cuenta \*?(\d{4}) a la cuenta \*?(\d+) el "
        + DATE + r" a las " + TIME, s, re.I)
    if m:
        v, org, dst, d, h = m.groups()
        return _txn(d, h, "expense", parse_amount(v), f"Cuenta *{dst}", org, "COP", "transfer")

    # --- Outgoing transfer to a KEY/person (Bre-b): "transferiste $X a la llave LLL desde tu cuenta *NNNN a NOMBRE el DD/MM/YYYY a las HH:MM" ---
    m = re.search(
        r"transferiste " + MONEY + r" a la llave (\S+) desde tu cuenta \*(\d{4}) a (.+?) el "
        + DATE + r" a las " + TIME, s, re.I)
    if m:
        v, key, org, nom, d, h = m.groups()
        return _txn(d, h, "expense", parse_amount(v), nom, org, "COP", "transfer")

    # Known non-transactional noise -> not a transaction.
    if re.search(r"Inscribiste la cuenta|extracto|rentabilidad|solicitud Bancolombia", s, re.I):
        return None

    # Unrecognized: let the caller (parser.py) collect it for review / LLM rescue.
    return None
