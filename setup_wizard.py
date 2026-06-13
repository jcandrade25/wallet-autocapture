#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Interactive setup wizard for wallet-autocapture.

What it does:
  1. Prompts for your BudgetBakers Wallet API token (using getpass, so it is
     NOT echoed to the terminal and never printed back).
  2. Connects to the Wallet REST API and downloads your accounts, labels, and
     categories.
  3. Starts from config.example.json (placeholders only) and writes config.json
     with your *real* category/label/account UUIDs filled in.
  4. Leaves identity / category_rules / email as placeholders for you to edit by
     hand, and prints clear next steps.

Privacy: the token is written only to the token file referenced by
wallet.token_file (kept out of git via .gitignore). It is never echoed and
never embedded in config.json.

Usage:
  python setup_wizard.py
"""
import os
import sys
import json
import copy
import getpass

# Make the src package importable whether run from the repo root or elsewhere.
REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from src import wallet_api  # noqa: E402

EXAMPLE_PATH = os.path.join(REPO_ROOT, "config.example.json")
CONFIG_PATH = os.path.join(REPO_ROOT, "config.json")
DEFAULT_API_BASE = "https://rest.budgetbakers.com/wallet/v1/api"

# SPEC-derived fallback template, used only if config.example.json is missing
# so the wizard can still produce a valid config.json standalone. Mirrors the
# schema in the project SPEC; contains ONLY placeholders, never real data.
FALLBACK_TEMPLATE = {
    "wallet": {"api_base": DEFAULT_API_BASE, "token_file": ".wallet_token"},
    "currency": "COP",
    "identity": {
        "self_names": ["YOUR NAME", "your.payment.key"],
        "own_accounts": ["1234", "5678"],
        "known_payees": {"3001234567": "A friend"},
    },
    "accounts": {
        "<bank text that identifies the account, e.g. 'Credit Card *1234'>": {
            "id": "<wallet-account-uuid>",
            "usd_id": "<optional-usd-subaccount-uuid>",
            "payment_type": "credit_card",
        }
    },
    "default_account_id": "<wallet-account-uuid>",
    "categories": {},
    "labels": {},
    "autocaptured_label": "reviewme",
    "pending_label": "needs-category",
    "usd_label": "usd",
    "record_state": "uncleared",
    "category_rules": [
        {"keywords": ["UBER", "TAXI"], "category": "Taxi", "label": "transport"}
    ],
    "email": {
        "host": "imap.gmail.com",
        "port": 993,
        "user": "you@gmail.com",
        "password_env": "WALLET_IMAP_PASSWORD",
        "password_file": "~/.wallet_imap",
        "senders": ["yourbank", "otherbank"],
        "statement_subject": "statement",
        "days": 3,
    },
    "bank_adapter": "bancolombia",
    "ollama": {
        "enabled": False,
        "url": "http://localhost:11434",
        "classify_model": "qwen2.5:7b",
        "rescue_model": "qwen2.5:7b",
        "embed_model": "all-minilm",
        "embed_threshold": 0.66,
    },
    "noise_categories": ["Transfer", "Loan, interests", "Others", "Uncategorized"],
}


def _strip_comments(obj):
    """Recursively drop ``__``-prefixed comment keys (the example file uses
    ``__comment__`` / ``__README__`` etc.) so the generated config.json is
    clean. The real config loader ignores them, but we don't want to copy
    them forward."""
    if isinstance(obj, dict):
        return {
            k: _strip_comments(v)
            for k, v in obj.items()
            if not (isinstance(k, str) and k.startswith("__"))
        }
    if isinstance(obj, list):
        return [_strip_comments(v) for v in obj]
    return obj


def load_template():
    """Load config.example.json, falling back to the SPEC-derived template.

    Comment keys (``__*``) are stripped so the produced config.json is tidy.
    """
    if os.path.exists(EXAMPLE_PATH):
        try:
            with open(EXAMPLE_PATH, encoding="utf-8") as f:
                print("Loaded template from config.example.json")
                return _strip_comments(json.load(f))
        except Exception as exc:
            print("  ! Could not parse config.example.json ({0}); using built-in template.".format(exc))
    else:
        print("config.example.json not found; using built-in template.")
    return copy.deepcopy(FALLBACK_TEMPLATE)


def prompt_token():
    """Ask for the API token without echoing it."""
    print("\nPaste your BudgetBakers Wallet API token.")
    print("(Generate it in Wallet web -> Settings -> API. Input is hidden.)")
    try:
        token = getpass.getpass("Token: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\nAborted.")
        sys.exit(1)
    if not token:
        print("No token entered. Aborting.")
        sys.exit(1)
    return token


def connect(token, api_base):
    """Build a Wallet client and verify the token by pulling accounts.

    Returns (wallet, accounts, labels, categories). Exits with a clear message
    if the token is rejected.
    """
    wallet = wallet_api.Wallet(token, api_base)
    try:
        accounts = wallet.get_accounts()
        labels = wallet.get_labels()
        categories = wallet.get_categories()
    except Exception as exc:
        txt = str(exc)
        if "401" in txt or "403" in txt or "auth" in txt.lower():
            print("\n  X Token rejected (HTTP 401/403). Double-check it in Wallet web -> Settings -> API.")
        else:
            print("\n  X Could not reach the Wallet API: {0}".format(exc))
        sys.exit(1)
    return wallet, accounts, labels, categories


def _name_id_map(items):
    """Build a {name: id} dict from a list of API objects, skipping nameless
    or idless entries and de-duplicating on name (first wins)."""
    out = {}
    for it in items or []:
        name = (it.get("name") or "").strip()
        uuid = it.get("id")
        if name and uuid and name not in out:
            out[name] = uuid
    return out


def write_config(cfg):
    """Write config.json (refuses to silently clobber without confirming)."""
    if os.path.exists(CONFIG_PATH):
        ans = input("\nconfig.json already exists. Overwrite? (type YES): ").strip()
        if ans != "YES":
            print("Left existing config.json untouched.")
            return False
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    return True


def write_token(token, cfg):
    """Persist the token to the file referenced by wallet.token_file.

    Resolved relative to the repo root when not absolute. Never echoed.
    """
    token_file = cfg.get("wallet", {}).get("token_file", ".wallet_token")
    token_file = os.path.expanduser(token_file)
    if not os.path.isabs(token_file):
        token_file = os.path.join(REPO_ROOT, token_file)
    with open(token_file, "w", encoding="utf-8") as f:
        f.write(token)
    return token_file


def main():
    print("=" * 70)
    print(" wallet-autocapture — setup wizard")
    print("=" * 70)

    template = load_template()
    api_base = template.get("wallet", {}).get("api_base", DEFAULT_API_BASE)

    token = prompt_token()
    print("\nConnecting to Wallet ...")
    _, accounts, labels, categories = connect(token, api_base)

    cat_map = _name_id_map(categories)
    label_map = _name_id_map(labels)
    print("  Downloaded {0} accounts, {1} labels, {2} categories.".format(
        len(accounts or []), len(label_map), len(cat_map)
    ))

    # Build the config from the template, filling in the real UUIDs.
    cfg = copy.deepcopy(template)
    cfg["categories"] = cat_map
    cfg["labels"] = label_map

    # Pre-fill accounts as a name->block map so the user only edits the bank
    # identifying text / USD sub-account / payment type. We key by the Wallet
    # account name and put the real UUID in place.
    acct_block = {}
    first_account_id = None
    for acc in accounts or []:
        name = (acc.get("name") or "").strip()
        uuid = acc.get("id")
        if not uuid:
            continue
        if first_account_id is None:
            first_account_id = uuid
        if name:
            acct_block[name] = {
                "id": uuid,
                "usd_id": "<optional-usd-subaccount-uuid>",
                "payment_type": "credit_card",
            }
    if acct_block:
        cfg["accounts"] = acct_block
    if first_account_id:
        cfg["default_account_id"] = first_account_id

    # identity / category_rules / email keep their placeholder values for the
    # user to edit by hand (we never guess personal data).

    if not write_config(cfg):
        return 1

    token_file = write_token(token, cfg)

    # NOTE: we deliberately never print the token value.
    print("\n" + "=" * 70)
    print(" Done. config.json written.")
    print("=" * 70)
    print("Next steps:")
    print("  1. Edit config.json -> 'accounts': set each KEY to the exact bank")
    print("     text that identifies that account (e.g. 'Credit Card *1234'),")
    print("     and fill 'usd_id'/'payment_type' where relevant.")
    print("  2. Fill 'identity' (self_names, own_accounts, known_payees) so your")
    print("     own transfers are treated as internal, not as expenses.")
    print("  3. Set 'email' (host/user, the IMAP app-password via env or file,")
    print("     and your bank 'senders').")
    print("  4. Pick the right 'bank_adapter' in config.json (module in src/banks/).")
    print("  5. Tweak 'category_rules', and set 'autocaptured_label'/'pending_label'/")
    print("     'usd_label' to label NAMES that now exist in your 'labels' map.")
    print("  6. (Optional) enable Ollama under 'ollama' for LLM categorization.")
    print("\nToken stored at: {0}  (kept out of git).".format(token_file))
    print("Then test with:  python -m src.run_daily --dry")
    return 0


if __name__ == "__main__":
    sys.exit(main())
