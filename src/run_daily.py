#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Headless daily capture loop for wallet-autocapture.

Pipeline (idempotent, degrades gracefully):
  1. load_config()                          -> the user's config.json
  2. email_fetch.fetch_snippets(cfg)        -> bank alert text snippets via IMAP
  3. parser.to_txns(snippets, cfg)          -> NORMALIZED TXNs (via bank adapter)
  4. register.build_records(txns, cfg)      -> WALLET RECORDS (uncleared + label)
  5. register.commit(records, wallet, cfg)  -> creates ONLY the new ones (dedup)
  6. email_fetch.detect_statement(cfg)      -> notes if a monthly statement landed
  7. append a block to capturas_log.md and write a rotating file log under logs/

Meant to run unattended (e.g. Windows Task Scheduler / cron). It never creates
anything "approved": every record is uncleared and carries the autocaptured
label for human review. Missing credentials or a disabled/offline Ollama do not
crash the run -- they are reported as incidents and the rest proceeds.

Usage:
  python -m src.run_daily            # full run
  python -m src.run_daily --dry      # fetch + parse + build, but do NOT write
  python -m src.run_daily --days=7   # override the email lookback window
"""
import os
import sys
import logging
import datetime
from logging.handlers import RotatingFileHandler

# Support both "python -m src.run_daily" (package) and "python src/run_daily.py"
# (script) execution styles.
if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from src import config as config_mod
    from src import email_fetch
    from src import parser as parser_mod
    from src import register
    from src import wallet_api
else:
    from . import config as config_mod
    from . import email_fetch
    from . import parser as parser_mod
    from . import register
    from . import wallet_api


# Repo root and well-known output paths.
HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
LOG_DIR = os.path.join(REPO_ROOT, "logs")
CAPTURAS_LOG = os.path.join(REPO_ROOT, "capturas_log.md")
ERROR_FLAG = os.path.join(REPO_ROOT, "CAPTURE_ERROR.txt")


def setup_logging():
    """Rotating file log under logs/ so unattended runs leave a trail."""
    os.makedirs(LOG_DIR, exist_ok=True)
    handler = RotatingFileHandler(
        os.path.join(LOG_DIR, "run_daily.log"),
        maxBytes=512_000, backupCount=3, encoding="utf-8",
    )
    logging.basicConfig(
        level=logging.INFO, handlers=[handler],
        format="%(asctime)s %(levelname)s %(message)s",
    )


def parse_args(argv):
    """Tiny flag parser: --dry and --days=N."""
    opts = {"dry": False, "days": None}
    for a in argv:
        if a == "--dry":
            opts["dry"] = True
        elif a.startswith("--days="):
            try:
                opts["days"] = int(a.split("=", 1)[1])
            except ValueError:
                pass
    return opts


def now_local():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M")


def append_capturas_log(block):
    with open(CAPTURAS_LOG, "a", encoding="utf-8") as f:
        f.write("\n" + block.rstrip() + "\n")


def main():
    opts = parse_args(sys.argv[1:])
    setup_logging()

    # Clear any stale error flag from a previous run.
    try:
        os.remove(ERROR_FLAG)
    except OSError:
        pass

    incidents = []
    snippets = []
    txns = []
    records = []
    statement_txt = "none"
    created = duplicates = candidates = 0

    # --- 0) config ---------------------------------------------------------
    try:
        cfg = config_mod.load_config()
    except Exception as exc:
        logging.exception("Could not load config.json")
        msg = "Could not load config.json: {0} (run setup_wizard.py first).".format(exc)
        _write_error_flag([msg])
        print(msg)
        return 1

    # Honor a --days override for the email lookback window.
    if opts["days"] is not None:
        cfg.setdefault("email", {})["days"] = opts["days"]

    # --- 1) fetch email snippets ------------------------------------------
    try:
        snippets = email_fetch.fetch_snippets(cfg) or []
        logging.info("IMAP: %d snippets", len(snippets))
    except Exception as exc:
        incidents.append("Email fetch failed: {0} (check IMAP credentials).".format(exc))
        logging.warning("fetch_snippets failed: %s", exc)

    # --- 2) parse snippets -> NORMALIZED TXNs ------------------------------
    if snippets:
        try:
            txns = parser_mod.to_txns(snippets, cfg) or []
            logging.info("Parsed %d txns from %d snippets", len(txns), len(snippets))
        except Exception as exc:
            incidents.append("Parsing failed: {0}.".format(exc))
            logging.exception("to_txns failed")
    else:
        logging.info("No snippets; skipping parse.")

    # --- 3) build WALLET RECORDS ------------------------------------------
    if txns:
        try:
            records = register.build_records(txns, cfg) or []
            candidates = len(records)
            logging.info("Built %d candidate records", candidates)
        except Exception as exc:
            incidents.append("Building records failed: {0}.".format(exc))
            logging.exception("build_records failed")

    # --- 4) commit (skipped on --dry) -------------------------------------
    if records and not opts["dry"]:
        try:
            token = config_mod.get_token(cfg)
            if not token:
                incidents.append("No Wallet token; nothing committed. Add one via setup_wizard.py.")
            else:
                wallet = wallet_api.Wallet(token, cfg["wallet"]["api_base"])
                result = register.commit(records, wallet, cfg)
                created = result.get("created", 0)
                duplicates = result.get("duplicates", 0)
                if any(
                    (s[0] if isinstance(s, (tuple, list)) else s) == "new"
                    and (s[1] if isinstance(s, (tuple, list)) and len(s) > 1 else "") == "dedup-skipped"
                    for s in result.get("statuses", [])
                ):
                    incidents.append("Dedup could not run; created candidates without duplicate check.")
                logging.info("Committed: %d created, %d duplicates", created, duplicates)
        except Exception as exc:
            txt = str(exc)
            if "401" in txt or "auth" in txt.lower():
                incidents.append("Wallet token invalid/expired (HTTP 401) — regenerate it in Wallet web -> API.")
            else:
                incidents.append("Commit failed: {0}.".format(exc))
            logging.exception("commit failed")
    elif records and opts["dry"]:
        logging.info("--dry: %d candidate records NOT written.", candidates)

    # --- 5) detect monthly statement --------------------------------------
    try:
        statement = email_fetch.detect_statement(cfg) or []
        if statement:
            statement_txt = "ARRIVED -> " + " | ".join(statement[:3])
    except Exception as exc:
        logging.warning("detect_statement failed: %s", exc)

    # --- 6) write the log block -------------------------------------------
    title = "automatic capture (local)" + (" [DRY]" if opts["dry"] else "")
    created_txt = (
        str(created) if not opts["dry"]
        else "{0} (dry, not written)".format(candidates)
    )
    block = "\n".join([
        "## {0} — {1}".format(now_local(), title),
        "- New records created: {0}".format(created_txt),
        "- Duplicates skipped: {0} · Candidates: {1} · Snippets: {2} · Parsed txns: {3}".format(
            duplicates, candidates, len(snippets), len(txns)
        ),
        "- Statement: {0}".format(statement_txt),
        "- Incidents: {0}".format("; ".join(incidents) if incidents else "none"),
    ])
    try:
        append_capturas_log(block)
    except Exception as exc:
        logging.warning("Could not append to capturas_log.md: %s", exc)
    logging.info("LOG block:\n%s", block)

    # --- 7) visible error flag --------------------------------------------
    if incidents:
        _write_error_flag(incidents)

    print(block)
    return 2 if incidents else 0


def _write_error_flag(incidents):
    """Drop a plain-text flag at the repo root so a human notices failures
    without opening the log files."""
    try:
        os.makedirs(REPO_ROOT, exist_ok=True)
        with open(ERROR_FLAG, "w", encoding="utf-8") as f:
            f.write(
                "{0} — the daily capture reported incidents:\n- ".format(now_local())
                + "\n- ".join(incidents)
                + "\n\nSee logs/run_daily.log and capturas_log.md.\n"
            )
    except Exception:
        pass


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # Last-resort crash handler: never die silently in an unattended run.
        import traceback
        os.makedirs(LOG_DIR, exist_ok=True)
        tb = traceback.format_exc()
        with open(os.path.join(LOG_DIR, "crash.log"), "a", encoding="utf-8") as f:
            f.write("\n=== {0} ===\n{1}\n".format(datetime.datetime.now().isoformat(), tb))
        _write_error_flag(["The daily capture CRASHED. See logs/crash.log:\n" + tb])
        raise
