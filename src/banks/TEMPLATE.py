# -*- coding: utf-8 -*-
"""
TEMPLATE bank adapter — copy this file to write an adapter for YOUR bank.

============================================================================
WHAT IS A BANK ADAPTER?
============================================================================
Your bank emails you a short alert every time you spend or receive money
("You spent $42.00 at CAFE X on 03/06/2026..."). wallet-autocapture fetches
those emails and hands each one to YOUR adapter as a plain-text string. Your
job: read that string and report the facts as a NORMALIZED TXN dict.

You only teach the code your bank's TEXT FORMAT. You do NOT put any personal
data here — no real names, account numbers, payee phone numbers, or tokens.
All of that lives in config.json (identity.*) and is applied automatically
after your adapter runs. Keep this file safe to commit to a public repo.

============================================================================
THE CONTRACT
============================================================================
You must define exactly two names:

    SENDERS : list[str]
        From-address substrings that identify your bank's alert emails, e.g.
        ["mybank", "alerts@mybank.com"]. The IMAP layer searches Gmail/IMAP
        "FROM" against these. Lower-case substrings are fine.

    parse(text: str) -> dict | None
        Given ONE cleaned alert string, return a NORMALIZED TXN dict, or None
        if the text is not a transaction you understand (marketing, OTP codes,
        monthly-statement notices, account-enrollment confirmations, ...).

A NORMALIZED TXN dict looks like this:

    {
      "date":         "YYYY-MM-DD",            # required, ISO date
      "time":         "HH:MM",                 # required (use "12:00" if absent)
      "type":         "expense" | "income" | "internal",
      "amount":       <float, ALWAYS POSITIVE>, # the sign is added later
      "merchant":     "<who you paid / who paid you>",
      "account_hint": "<last 4 digits or any text naming the account>",
      "currency":     "COP" | "USD" | ...,     # ISO currency code
      "channel":      "<optional: pos, web, qr, transfer, atm, ...>",
    }

Rules of thumb for ``type``:
  - "expense"  : money left your account (a purchase, an outgoing transfer).
  - "income"   : money arrived (payroll, a transfer someone sent you).
  - "internal" : a move that is NOT real spend/income — paying your own credit
                 card, moving between your own accounts. If you can tell purely
                 from the text that it's structurally internal (e.g. "paid your
                 credit card *1234 from account *5678"), set it here. If it only
                 *might* be internal because the other party is YOU, leave it as
                 expense/income — the identity layer (config.identity.self_names
                 / own_accounts) will re-classify it. The adapter shouldn't know
                 who "you" are.

``amount`` is always a positive number. register.py decides whether it becomes
negative (expense) or positive (income) in Wallet.

============================================================================
HOW TO WRITE parse()
============================================================================
Most bank alerts are one sentence with a fixed shape. Regular expressions are
the simplest tool. A few reusable building blocks:

    import re
    DATE  = r"(\\d{2}/\\d{2}/\\d{2,4})"   # 03/06/2026 or 03/06/26
    TIME  = r"(\\d{2}:\\d{2})"            # 17:10
    MONEY = r"\\$?\\s*([\\d.,]+)"          # $1,234.56 / 1.234,56 / 42

Match the MOST SPECIFIC patterns first and fall through to more general ones.
Return as soon as a pattern matches.

PARSING AMOUNTS IS THE #1 SOURCE OF BUGS. Decide what your bank's decimal
separator is and write a tiny helper:

    # US/UK style: "1,234.56" -> comma = thousands, dot = decimal
    def parse_amount(s):
        return float(s.replace(",", ""))

    # European/Latin-American style: "1.234,56" -> dot = thousands, comma = decimal
    def parse_amount(s):
        return float(s.replace(".", "").replace(",", "."))

See ``bancolombia.py`` for a real example that handles three different amount
shapes from the same bank, plus separate COP and USD parsing.

============================================================================
HOW TO TEST
============================================================================
1. Drop a few real (anonymized!) alert strings into the project's ``sample/``
   folder, one per line in a .txt file, or as a JSON list of strings.
2. Add a quick self-test at the bottom of your adapter (see the ``if __name__``
   block below) and run it:

       python -m src.banks.<your_adapter>

   Each TESTS entry is (input_string, expected_or_None). Tweak your regexes
   until everything passes. Keep the test strings free of personal data.
3. Point ``config.json`` at your adapter:

       "bank_adapter": "<your_adapter>",
       "email": { ..., "senders": ["mybank"] }

   then run ``python -m src.run_daily --dry --days=7`` to see it end-to-end
   without writing anything to Wallet.
============================================================================
"""
import re

# 1) Tell the email layer which senders carry your bank's alerts.
SENDERS = ["mybank"]  # TODO: replace with your bank's From-address substrings.

# 2) Reusable sub-patterns (adjust to your bank's date/time/amount notation).
DATE = r"(\d{2}/\d{2}/\d{2,4})"
TIME = r"(\d{2}:\d{2})"
MONEY = r"\$?\s*([\d.,]+)"


def parse_amount(s):
    """Convert your bank's amount string to a positive float.

    EXAMPLE shown for US/UK formatting ("1,234.56" -> 1234.56). If your bank
    uses European/Latin-American formatting ("1.234,56"), swap the replaces:
        return float(s.replace(".", "").replace(",", "."))
    """
    return float(s.strip().replace(",", ""))


def _fix_date(d):
    """Normalize ``DD/MM/YY``-ish dates to ISO ``YYYY-MM-DD``.

    Adjust the field order if your bank prints MM/DD/YYYY instead.
    """
    dd, mm, yy = d.split("/")
    if len(yy) == 2:
        yy = "20" + yy
    return f"{yy}-{mm}-{dd}"


def parse(text):
    """One alert string -> NORMALIZED TXN dict, or None.

    Replace the example branches below with your bank's real phrasings.
    """
    s = re.sub(r"\s+", " ", text or "").strip()
    if not s:
        return None

    # --- EXAMPLE: a purchase alert ---
    # "You spent $42.00 at CAFE X on 03/06/2026 at 17:10 with card *1234"
    m = re.search(
        r"You spent " + MONEY + r" at (.+?) on " + DATE + r" at " + TIME
        + r".*?card \*(\d{4})", s, re.I)
    if m:
        amount, merchant, d, h, card = m.groups()
        return {
            "date": _fix_date(d),
            "time": h,
            "type": "expense",
            "amount": parse_amount(amount),
            "merchant": merchant.strip(),
            "account_hint": card,
            "currency": "USD",
            "channel": "pos",
        }

    # --- EXAMPLE: an incoming transfer ---
    # "You received $500.00 from JANE DOE on 03/06/2026 at 09:00 in account *5678"
    m = re.search(
        r"You received " + MONEY + r" from (.+?) on " + DATE + r" at " + TIME
        + r".*?account \*(\d{4})", s, re.I)
    if m:
        amount, sender, d, h, acct = m.groups()
        return {
            "date": _fix_date(d),
            "time": h,
            "type": "income",
            "amount": parse_amount(amount),
            "merchant": sender.strip(),
            "account_hint": acct,
            "currency": "USD",
            "channel": "transfer",
        }

    # --- EXAMPLE: a structurally-internal credit-card payment ---
    # "You paid $200.00 toward card *1234 from account *5678 on 03/06/2026 12:00"
    m = re.search(
        r"You paid " + MONEY + r" toward card \*(\d{4}) from account \*(\d{4}) on "
        + DATE + r"\s+" + TIME, s, re.I)
    if m:
        amount, card, acct, d, h = m.groups()
        return {
            "date": _fix_date(d),
            "time": h,
            "type": "internal",
            "amount": parse_amount(amount),
            "merchant": f"Card *{card} payment",
            "account_hint": acct,
            "currency": "USD",
            "channel": "transfer",
        }

    # Not a transaction we understand -> let the caller collect it for review.
    return None


# ---------------------------------------------------------------------------
# Optional self-test. Run with:  python -m src.banks.<your_adapter>
# Each entry is (input, expected) where expected is (type, amount, merchant)
# or None for "should not parse". Keep strings free of personal data.
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    TESTS = [
        ("You spent $42.00 at CAFE X on 03/06/2026 at 17:10 with card *1234",
         ("expense", 42.0, "CAFE X")),
        ("You received $500.00 from JANE DOE on 03/06/2026 at 09:00 in account *5678",
         ("income", 500.0, "JANE DOE")),
        ("You paid $200.00 toward card *1234 from account *5678 on 03/06/2026 12:00",
         ("internal", 200.0, "Card *1234 payment")),
        ("Your monthly statement is ready.", None),
    ]
    ok = fail = 0
    for text, want in TESTS:
        got = parse(text)
        if want is None:
            if got is None:
                ok += 1
            else:
                print(f"  FAIL (should be None): {text!r} -> {got}")
                fail += 1
            continue
        if got and (got["type"], got["amount"], got["merchant"]) == want:
            ok += 1
        else:
            print(f"  FAIL: {text!r}\n     want {want} got {got}")
            fail += 1
    print(f"SELF-TEST: {ok} ok / {fail} fail of {len(TESTS)}")
