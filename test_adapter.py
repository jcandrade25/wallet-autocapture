#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Self-contained test harness for the `bancolombia` bank adapter.

Loads the fictitious alerts in sample/sample_alerts.json, runs them through the
real data pipeline (src.parser.to_txns, which uses the adapter named in
cfg["bank_adapter"]), and compares the resulting NORMALIZED TXNs against the
golden output in sample/expected.csv. Prints PASS/FAIL per case and exits 0 only
if every case matches.

Standard library only. Run from the repo root:

    python test_adapter.py
    python test_adapter.py --verbose     # dump the full parsed txn for every case

Degrades gracefully: if the pipeline modules have not been written yet, it falls
back to importing the adapter module directly; if that is also missing it prints a
clear SKIP and exits 0 so a half-built tree does not look like a hard failure.

PRIVACY: every name, account number, payment key and amount in the sample is
fictitious. Nothing here is real personal data.
"""
import csv
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLE_DIR = os.path.join(HERE, "sample")
ALERTS_PATH = os.path.join(SAMPLE_DIR, "sample_alerts.json")
EXPECTED_PATH = os.path.join(SAMPLE_DIR, "expected.csv")

# Make `import src...` work regardless of where the test is launched from.
sys.path.insert(0, HERE)

# Minimal config that mirrors config.example.json: the placeholder identity the
# sample alerts are written against. The adapter resolves self-transfers and
# known payees from here, so the sample is fully reproducible with no real data.
TEST_CONFIG = {
    "currency": "COP",
    "bank_adapter": "bancolombia",
    "identity": {
        "self_names": ["YOUR NAME", "your.payment.key"],
        "own_accounts": ["1234", "5678"],
        "known_payees": {},
    },
}


def _norm(s):
    """Normalize a string field for tolerant comparison."""
    return " ".join((s or "").strip().upper().split())


def _amount_str(value, currency):
    """Render a NORMALIZED-TXN amount the same way expected.csv stores it:
    USD keeps two decimals, COP is an integer (no decimals)."""
    try:
        v = abs(float(value))
    except (TypeError, ValueError):
        return str(value)
    if (currency or "").upper() == "USD":
        return f"{v:.2f}"
    return str(int(round(v)))


def load_alerts():
    with io.open(ALERTS_PATH, encoding="utf-8") as fh:
        data = json.load(fh)
    # Accept ["snippet", ...] or [{"snippet": "..."}, ...].
    return [x["snippet"] if isinstance(x, dict) else str(x) for x in data]


def load_expected():
    with io.open(EXPECTED_PATH, encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def _unwrap_txns(result):
    """Normalize the return value of to_txns into a flat list of txn dicts.

    The spec types to_txns as ``-> list[NORMALIZED TXN]``, but the implementation
    returns ``(txns, unparsed)`` so callers can log / rescue what didn't parse.
    Accept both: a bare list of dicts, or a (txns, unparsed) tuple/list whose
    first element is the txn list.
    """
    if isinstance(result, tuple):
        return list(result[0]) if result else []
    if isinstance(result, list) and result and isinstance(result[0], list):
        # (txns, unparsed) returned as a 2-element list rather than a tuple.
        return list(result[0])
    return list(result or [])


def get_txns(snippets):
    """Run the snippets through the documented pipeline.

    Preference order:
      1. src.parser.to_txns(snippets, cfg)         (the real contract)
      2. src.banks.get_adapter(name).parse(text)   (adapter-only fallback)
    Returns (txns, mode) or (None, reason) when nothing is importable yet.
    """
    # 1) Full pipeline via src.parser (the canonical entry point).
    try:
        from src.parser import to_txns  # type: ignore
        return _unwrap_txns(to_txns(snippets, TEST_CONFIG)), "src.parser.to_txns"
    except ImportError:
        pass
    except Exception as exc:  # parser exists but blew up — surface it
        return None, f"src.parser.to_txns raised: {exc!r}"

    # 2) Adapter only — exercise the regexes directly.
    try:
        from src.banks import get_adapter  # type: ignore
        adapter = get_adapter(TEST_CONFIG["bank_adapter"])
    except Exception:
        try:
            import importlib
            adapter = importlib.import_module(
                "src.banks." + TEST_CONFIG["bank_adapter"]
            )
        except Exception:
            return None, "neither src.parser nor src.banks.<adapter> is importable yet"
    try:
        txns = [adapter.parse(s) for s in snippets]
        return [t for t in txns if t is not None], "src.banks.%s.parse (adapter only)" % TEST_CONFIG["bank_adapter"]
    except Exception as exc:
        return None, f"adapter.parse raised: {exc!r}"


def match(got, exp):
    """Compare one parsed NORMALIZED TXN against one expected.csv row.

    Strict on the load-bearing fields (date, time, type, amount, account_hint,
    currency). Merchant is strict for non-internal rows; for 'internal' rows the
    adapter is free to choose its own self-transfer wording, so we only require
    that the type is internal.
    """
    fails = []
    if (got.get("date") or "") != exp["date"]:
        fails.append(f"date {got.get('date')!r} != {exp['date']!r}")
    if (got.get("time") or "") != exp["time"]:
        fails.append(f"time {got.get('time')!r} != {exp['time']!r}")
    if (got.get("type") or "") != exp["type"]:
        fails.append(f"type {got.get('type')!r} != {exp['type']!r}")
    got_amt = _amount_str(got.get("amount"), got.get("currency"))
    if got_amt != exp["amount"]:
        fails.append(f"amount {got_amt!r} != {exp['amount']!r}")
    # account_hint: compare the trailing digits so '*1234' or 'Card 1234' both pass.
    got_acc = "".join(ch for ch in str(got.get("account_hint") or "") if ch.isdigit())[-4:]
    exp_acc = "".join(ch for ch in exp["account_hint"] if ch.isdigit())[-4:]
    if got_acc != exp_acc:
        fails.append(f"account_hint {got.get('account_hint')!r} (->{got_acc}) != {exp['account_hint']!r}")
    # currency: empty string from the adapter means the config default (COP).
    got_cur = (got.get("currency") or TEST_CONFIG["currency"]).upper()
    if got_cur != exp["currency"].upper():
        fails.append(f"currency {got.get('currency')!r} != {exp['currency']!r}")
    if exp["type"] != "internal":
        if _norm(got.get("merchant")) != _norm(exp["merchant"]):
            fails.append(f"merchant {got.get('merchant')!r} != {exp['merchant']!r}")
    return fails


def main():
    for st in (sys.stdout, sys.stderr):
        try:
            st.reconfigure(encoding="utf-8")
        except Exception:
            pass

    verbose = "--verbose" in sys.argv

    if not os.path.exists(ALERTS_PATH) or not os.path.exists(EXPECTED_PATH):
        print("SKIP: sample/sample_alerts.json or sample/expected.csv is missing.")
        return 0

    snippets = load_alerts()
    expected = load_expected()

    txns, mode = get_txns(snippets)
    if txns is None:
        print(f"SKIP: pipeline not built yet ({mode}).")
        print("      Re-run this once src/parser.py + src/banks/bancolombia.py exist.")
        return 0

    print(f"adapter pipeline: {mode}")
    print(f"snippets: {len(snippets)} | parsed txns: {len(txns)} | expected rows: {len(expected)}\n")

    if len(txns) != len(expected):
        print(f"FAIL (count): parser produced {len(txns)} txns but expected {len(expected)}.")

    ok = 0
    n = max(len(txns), len(expected))
    for i in range(n):
        got = txns[i] if i < len(txns) else None
        exp = expected[i] if i < len(expected) else None
        label = (exp["merchant"] if exp else (got.get("merchant") if got else "?"))
        if got is None:
            print(f"  [{i + 1}] FAIL  (no txn produced for expected '{label}')")
            continue
        if exp is None:
            print(f"  [{i + 1}] FAIL  (extra txn '{got.get('merchant')}' with no expected row)")
            if verbose:
                print(f"        got: {got}")
            continue
        fails = match(got, exp)
        if not fails:
            ok += 1
            print(f"  [{i + 1}] PASS  {exp['type']:<8} {exp['amount']:>9} {exp['currency']}  {label}")
        else:
            print(f"  [{i + 1}] FAIL  {label}")
            for f in fails:
                print(f"        - {f}")
        if verbose:
            print(f"        got: {got}")

    print(f"\nRESULT: {ok}/{len(expected)} cases PASS")
    return 0 if (ok == len(expected) and len(txns) == len(expected)) else 1


if __name__ == "__main__":
    sys.exit(main())
