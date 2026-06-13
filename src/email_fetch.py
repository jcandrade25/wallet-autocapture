# -*- coding: utf-8 -*-
"""
Generic IMAP fetcher for bank transaction-alert emails.

This is the configurable, bank-agnostic generalization of a personal IMAP
script. It does NOT parse amounts/dates — it only returns clean Unicode text
snippets that the bank adapter (via ``src/parser.py``) then parses.

Configuration (``config.email``):
    {
      "host": "imap.gmail.com",
      "port": 993,
      "user": "you@gmail.com",
      "password_env": "WALLET_IMAP_PASSWORD",   # env var holding the password
      "password_file": "~/.wallet_imap",        # OR a file with the password
      "senders": ["yourbank", "otherbank"],      # From-address substrings
      "statement_subject": "statement",          # subject substring for statements
      "days": 3                                   # default look-back window
    }

The IMAP password should be an app-specific password (Gmail requires 2FA).
It is resolved by ``src/config.resolve_secret`` (env first, then file) so the
secret never lives in the repo. If neither is set, fetching degrades to an
empty list (the daily run logs a warning instead of crashing).

Public API:
    fetch_snippets(cfg) -> list[str]    # clean alert text snippets
    detect_statement(cfg) -> list[str]  # subjects of recently-arrived statements
"""
import datetime
import email
import email.policy
import html as htmllib
import imaplib
import re

from . import config as _config

# English month abbreviations for building IMAP date strings independently of
# the host's locale (IMAP wants ``DD-Mon-YYYY`` with an English month).
_EN_MONTHS = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


class NoCredentials(Exception):
    """No IMAP password is configured (neither env var nor file)."""


def _imap_date(d):
    """A date as IMAP's ``DD-Mon-YYYY`` with an English month (locale-independent)."""
    return f"{d.day:02d}-{_EN_MONTHS[d.month]}-{d.year}"


def _email_cfg(cfg):
    return cfg.get("email", {}) or {}


def _get_password(cfg):
    """Resolve the IMAP password via env var then file (see config.resolve_secret)."""
    ec = _email_cfg(cfg)
    pw = _config.resolve_secret(ec.get("password_env"), ec.get("password_file"))
    if not pw:
        raise NoCredentials(
            "No IMAP password found. Set the env var named in "
            "config.email.password_env, or put the password in the file at "
            "config.email.password_file."
        )
    # App passwords are often shown grouped ("abcd efgh ijkl mnop"); spaces are ignored.
    return re.sub(r"\s+", "", pw)


def to_clean_text(msg):
    """Return the email body as clean Unicode text.

    Prefers ``text/plain``; if only HTML is available, strips tags and
    unescapes entities. Collapses all whitespace to single spaces.
    """
    body = msg.get_body(preferencelist=("plain", "html"))
    if body is None:
        return ""
    text = body.get_content()  # decodes transfer-encoding + charset -> str
    if body.get_content_type() == "text/html":
        text = re.sub(r"<[^>]+>", " ", text)
        text = htmllib.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _all_mail_box(M):
    r"""Find the 'All Mail' mailbox by its ``\All`` attribute (language-independent).

    Gmail localizes the visible folder name ("Todos", "Tous", ...), but the
    special-use attribute ``\All`` is stable, so we match on that rather than
    on a hard-coded folder name.
    """
    try:
        typ, boxes = M.list()
        if typ == "OK" and boxes:
            for raw in boxes:
                line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
                if "\\All" in line:
                    m = re.search(r'"([^"]*)"\s*$', line)
                    if m:
                        return m.group(1)
    except Exception:
        pass
    return None


def _connect(cfg):
    """Open an authenticated, read-only IMAP connection on the best mailbox.

    Selects All Mail (any locale) if available, else INBOX. Caller is
    responsible for closing/logging out (see the ``try/finally`` in callers).
    """
    ec = _email_cfg(cfg)
    host = ec.get("host", "imap.gmail.com")
    port = int(ec.get("port", 993))
    user = ec.get("user")
    if not user:
        raise NoCredentials("config.email.user is not set.")
    M = imaplib.IMAP4_SSL(host, port)
    M.login(user, _get_password(cfg))
    box = _all_mail_box(M)
    for cand in ([box] if box else []) + ["INBOX"]:
        typ, _ = M.select(f'"{cand}"', readonly=True)
        if typ == "OK":
            return M
    raise imaplib.IMAP4.error("Could not select a mailbox (All Mail / INBOX).")


def _search_uids(M, *terms):
    """Run a UID SEARCH and return the matching UIDs (list of bytes)."""
    typ, data = M.uid("SEARCH", None, *terms)
    if typ != "OK" or not data or not data[0]:
        return []
    return data[0].split()


def _senders(cfg):
    """Sender substrings to search for: config first, else adapter default.

    Falls back to the configured bank adapter's ``SENDERS`` so a minimal
    config that only sets ``bank_adapter`` still works.
    """
    senders = _email_cfg(cfg).get("senders")
    if senders:
        return list(senders)
    try:
        from .banks import get_adapter
        return list(getattr(get_adapter(cfg.get("bank_adapter", "")), "SENDERS", []))
    except Exception:
        return []


def fetch_snippets(cfg):
    """Fetch recent bank-alert emails and return their clean text snippets.

    Searches, per configured sender substring, for messages ``SINCE`` the
    look-back window (``--days`` overrides via ``cfg["_days"]`` set by the
    runner, else ``config.email.days``, else 3). De-duplicates UIDs, filters
    to transactional senders, and returns one clean-text string per message.

    Returns ``[]`` (without raising) if credentials are missing, so the daily
    run degrades gracefully — the runner is expected to log the cause.
    """
    days = int(cfg.get("_days") or _email_cfg(cfg).get("days") or 3)
    since = _imap_date(datetime.date.today() - datetime.timedelta(days=days))
    senders = _senders(cfg)
    if not senders:
        return []
    try:
        M = _connect(cfg)
    except NoCredentials:
        return []
    try:
        uids = []
        for snd in senders:
            uids += _search_uids(M, "SINCE", since, "FROM", snd)
        seen, snippets = set(), []
        for uid in uids:
            if uid in seen:
                continue
            seen.add(uid)
            typ, raw = M.uid("FETCH", uid, "(RFC822)")
            if typ != "OK" or not raw or raw[0] is None:
                continue
            msg = email.message_from_bytes(raw[0][1], policy=email.policy.default)
            frm = (msg.get("From") or "").lower()
            if not any(s in frm for s in senders):
                continue
            text = to_clean_text(msg)
            if text:
                snippets.append(text)
        return snippets
    finally:
        try:
            M.close()
        except Exception:
            pass
        try:
            M.logout()
        except Exception:
            pass


def detect_statement(cfg):
    """Return the subjects of recently-arrived statement emails (or ``[]``).

    Searches the configured senders for messages whose SUBJECT contains
    ``config.email.statement_subject`` within the look-back window (minimum 2
    days). Only headers are fetched. Degrades to ``[]`` if no credentials or no
    statement subject is configured.
    """
    ec = _email_cfg(cfg)
    subject = ec.get("statement_subject")
    if not subject:
        return []
    days = max(int(cfg.get("_days") or ec.get("days") or 3), 2)
    since = _imap_date(datetime.date.today() - datetime.timedelta(days=days))
    senders = _senders(cfg)
    if not senders:
        return []
    try:
        M = _connect(cfg)
    except NoCredentials:
        return []
    try:
        uids = []
        for snd in senders:
            uids += _search_uids(M, "SINCE", since, "FROM", snd, "SUBJECT", subject)
        seen, found = set(), []
        for uid in uids:
            if uid in seen:
                continue
            seen.add(uid)
            typ, raw = M.uid("FETCH", uid, "(BODY.PEEK[HEADER.FIELDS (SUBJECT FROM)])")
            if typ != "OK" or not raw or raw[0] is None:
                continue
            hdr = email.message_from_bytes(raw[0][1], policy=email.policy.default)
            subj = (hdr.get("Subject") or "").strip()
            if subj:
                found.append(subj)
        return found
    finally:
        try:
            M.close()
        except Exception:
            pass
        try:
            M.logout()
        except Exception:
            pass
