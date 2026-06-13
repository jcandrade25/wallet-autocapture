# Installation & Setup

This is the detailed, do-it-once setup guide for **wallet-autocapture**. Follow
the sections in order. By the end you'll have a daily job that reads your bank
alert emails and drops pending records into your BudgetBakers Wallet for review.

> **Golden rule:** secrets (Wallet token, email app password) and your real
> `config.json` **never** go into the repo. They live in git-ignored files. The
> repo ships only placeholders.

**Contents**

1. [Requirements](#1-requirements)
2. [Wallet API token](#2-wallet-api-token)
3. [Setup wizard → config.json](#3-setup-wizard--configjson)
4. [Email access (IMAP app password)](#4-email-access-imap-app-password)
5. [Bank adapter](#5-bank-adapter)
6. [Optional: local LLM (Ollama)](#6-optional-local-llm-ollama)
7. [First run](#7-first-run)
8. [Scheduling](#8-scheduling)
9. [Security & troubleshooting](#9-security--troubleshooting)

---

## 1. Requirements

- **Python 3.8 or newer.** Check with `python --version` (on some systems
  `python3 --version`). No pip packages are required for the core tool — it uses
  only the standard library.
- **A BudgetBakers Wallet account with Premium.** The REST API (which this tool
  uses to read and create records) is a **Premium** feature. A free account can
  use the app but cannot issue an API token.
- **An email account that receives your bank's transaction alerts**, with IMAP
  access available (Gmail, Outlook/Microsoft 365, iCloud, Fastmail, most
  providers).
- *(Optional)* **[Ollama](https://ollama.com)** if you want local-LLM
  categorization — see section 6.

Clone the repo and enter it:

```bash
git clone https://github.com/<your-org>/wallet-autocapture.git
cd wallet-autocapture
```

`pip install -r requirements.txt` is effectively a no-op (no hard deps) but is
safe to run.

---

## 2. Wallet API token

This tool uses the **official BudgetBakers Wallet REST API** with a personal
**Bearer token**.

### Generate the token

1. Open **[web.budgetbakers.com](https://web.budgetbakers.com)** and sign in.
2. Go to **Settings → API** (Premium accounts only).
3. **Create / generate an API token.** This token grants **read and write**
   access to your records, accounts, labels, and categories — treat it like a
   password.
4. Copy it.

### Where to put it

Save it where the tool can find it, **outside anything that syncs publicly**.
The token is read in this order:

1. The environment variable **`WALLET_API_TOKEN`**, or
2. The file named by `wallet.token_file` in `config.json` (default
   **`.wallet_token`**, relative to `config.json`).

Simplest: create a one-line file `.wallet_token` in the repo root:

```bash
# macOS / Linux
printf '%s' 'PASTE_YOUR_TOKEN_HERE' > .wallet_token

# Windows PowerShell
'PASTE_YOUR_TOKEN_HERE' | Out-File -NoNewline -Encoding ascii .wallet_token
```

`.wallet_token` is already in `.gitignore`, so it will **never** be committed.

### Token (REST API) vs. the Wallet MCP — what's the difference?

You may have seen a **Wallet MCP** (Model Context Protocol) server. They are
*not* the same thing:

- **The MCP** is for *interactive* use with an AI assistant (e.g. Claude). You
  ask the assistant in chat and it calls Wallet through the MCP. Great for
  ad-hoc questions, not for an unattended daily job.
- **This repo** uses the **plain REST API with a token** — a headless,
  scriptable client (`src/wallet_api.py`) that runs on a schedule with no AI in
  the loop. That's what you want for a hands-off capture pipeline.

You need **write scope** because the tool *creates* records (`POST /records`).
The token from Settings → API covers this. The tool only ever creates records as
`uncleared` (pending) — it does not clear/approve them for you.

---

## 3. Setup wizard → config.json

With the token in place, run the wizard. It connects to your Wallet account,
**auto-discovers** your accounts, labels, and categories, and writes a
`config.json` for you (starting from `config.example.json`).

```bash
python setup_wizard.py
```

What it does:

- Prompts for / reads your token (it **never prints the token back**).
- Calls the API and lists your **accounts** (with UUIDs), **labels**, and
  **categories**.
- Writes **`config.json`**, pre-filling the `categories` and `labels` maps with
  your real UUIDs, and giving you the account UUIDs to wire into `accounts`.
- Leaves `identity`, `category_rules`, `accounts` mapping, and `email` as
  placeholders for you to edit.
- Prints the next steps.

### What to edit by hand afterward

Open `config.json` and fill in:

| Section | What to set |
| --- | --- |
| **`accounts`** | For each bank-alert account text (e.g. `"Credit Card *1234"`), set `id` to the matching Wallet account UUID the wizard listed. Add `usd_id` if that card has a USD sub-account. Set `payment_type` (`credit_card` / `debit_card` / `cash`). |
| **`default_account_id`** | The UUID to fall back to when an alert's account can't be matched. |
| **`identity`** | `self_names` (your name + any payment alias) so your own transfers are detected as `internal`; `own_accounts` (your account last-4s); `known_payees` (map a destination number to a friendly name). |
| **`category_rules`** | Keyword → category routing. Each `category` must be a name present in `categories`; each optional `label` must be a name present in `labels`. |
| **`labels` keys** | Confirm `autocaptured_label`, `pending_label`, and `usd_label` point at label names that exist in `labels`. |
| **`bank_adapter`** | The adapter module name (section 5). |
| **`email`** | IMAP details (section 4). |

The `__comment__` / `__...__` keys in `config.example.json` explain every field
inline; they're ignored by the loader and you can delete them.

> **Do not commit `config.json`.** It contains your UUIDs, account numbers,
> names, and email. It's git-ignored by default — keep it that way.

---

## 4. Email access (IMAP app password)

The tool reads alert emails over **IMAP, read-only**. Modern providers require an
**app password** (a dedicated 16-ish-character credential), not your normal
login password.

### 4.1 Enable 2FA and create an app password

**Gmail:**

1. Turn on **2-Step Verification** at
   [myaccount.google.com/security](https://myaccount.google.com/security)
   (app passwords require it).
2. Go to **[myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)**,
   create one (e.g. name it "wallet-autocapture"), and copy the 16-character
   value. (Spaces shown for readability are ignored — you can store it with or
   without them.)
3. Enable IMAP: Gmail → **Settings → Forwarding and POP/IMAP → Enable IMAP**.

**Outlook / Microsoft 365:**

- Enable 2FA, then create an app password at
  [account.microsoft.com/security](https://account.microsoft.com/security) →
  *Advanced security options → App passwords*. IMAP host: `outlook.office365.com`.

**iCloud Mail:** generate an app-specific password at
[account.apple.com](https://account.apple.com) → *Sign-In and Security →
App-Specific Passwords*. IMAP host: `imap.mail.me.com`.

**Yahoo / Fastmail / others:** look for "app password" / "app-specific
password" in account security settings; use the provider's documented IMAP host.

### 4.2 Store the secret OUTSIDE the repo

The email password is read in this order:

1. The environment variable named by `email.password_env`
   (default **`WALLET_IMAP_PASSWORD`**), or
2. The file named by `email.password_file` (default **`~/.wallet_imap`**, i.e. in
   your home directory — *not* in the repo).

```bash
# Option A — a file in your home directory (one line)
# macOS / Linux:
printf '%s' 'your app password' > ~/.wallet_imap
chmod 600 ~/.wallet_imap

# Windows PowerShell:
'your app password' | Out-File -NoNewline -Encoding ascii $HOME\.wallet_imap

# Option B — an environment variable
export WALLET_IMAP_PASSWORD='your app password'      # macOS / Linux
$env:WALLET_IMAP_PASSWORD = 'your app password'      # Windows PowerShell (current session)
```

`.gitignore` already excludes `*_imap`, `.wallet_imap`, and `.env`.

### 4.3 Configure the `email` block in config.json

```jsonc
"email": {
  "host": "imap.gmail.com",
  "port": 993,
  "user": "you@gmail.com",
  "password_env": "WALLET_IMAP_PASSWORD",
  "password_file": "~/.wallet_imap",
  "senders": ["yourbank", "otherbank"],   // substrings matched against the From header
  "statement_subject": "statement",        // subject substring that signals a monthly statement
  "days": 3                                 // how many days back to scan
}
```

- **`senders`** are substrings of the sender address — e.g. if alerts come from
  `alerts@notifications.yourbank.com`, `"yourbank"` matches. Keep it specific
  enough to avoid marketing emails.
- The fetcher selects the **All Mail / archive mailbox** by its IMAP `\All`
  attribute, so it works regardless of your mailbox's language; it falls back to
  `INBOX`.

Test it (this prints snippets, writes nothing to Wallet):

```bash
python -m src.email_fetch        # if the module exposes a CLI, or:
python -m src.run_daily --dry    # the dry run exercises fetch + parse end-to-end
```

---

## 5. Bank adapter

A **bank adapter** is the small module that knows your bank's exact alert wording
and amount formatting. It lives in `src/banks/<name>.py` and exposes:

```python
SENDERS = ["yourbank"]            # sender substrings for this bank

def parse(text: str) -> dict | None:
    """Return a NORMALIZED transaction dict, or None if `text` isn't a txn."""
```

The normalized dict is:

```python
{"date": "YYYY-MM-DD", "time": "HH:MM",
 "type": "expense" | "income" | "internal",
 "amount": <positive float>, "merchant": "<name>",
 "account_hint": "<last-4 or text>", "currency": "COP" | "USD",
 "channel": "<optional>"}
```

### If your bank is Bancolombia

It's already included. Set in `config.json`:

```json
"bank_adapter": "bancolombia"
```

### If your bank is something else

1. Copy the template:
   ```bash
   cp src/banks/TEMPLATE.py src/banks/mybank.py
   ```
2. Collect a few **real alert emails** from your bank for each kind of event
   (purchase, transfer sent, transfer received, card payment, etc.). Look at
   their exact wording.
3. Write regexes in `mybank.py` that extract amount, merchant/destination, date,
   time, and account. Mind your bank's **amount format** — e.g. some locales use
   a **comma as the decimal** separator and a **dot for thousands**
   (`55.500,00` = 55 500), and foreign-currency alerts may use yet another
   format. The Bancolombia adapter shows how to handle several formats in one
   parser; copy the *patterns*, not the data.
4. Map your own / internal movements: a transfer to yourself or a credit-card
   payment from your own account should return `type="internal"` so it's skipped
   (it's not new spending). The `identity` config (`self_names`, `own_accounts`)
   helps the parser decide.
5. Point the config at it:
   ```json
   "bank_adapter": "mybank"
   ```
6. **Test against samples.** Put sanitized example alerts in `sample/` (fictional
   amounts/merchants only — never a real exported email) and verify your adapter
   parses them. Many adapters include a small `--test` self-check; run it before
   going live.

> Adapters are intentionally per-user: bank wording differs by country, product,
> and even over time. Keeping the bank-specific regexes isolated in one module
> means the rest of the pipeline stays generic.

---

## 6. Optional: local LLM (Ollama)

This step is **optional**. Skip it entirely and the tool still works — unmatched
merchants just get a `needs-category` label you fix in the app.

The LLM runs **locally** via Ollama; nothing leaves your machine. It (a) suggests
a category for merchants the keyword rules miss and (b) tries to "rescue" alerts
the regex couldn't parse. In all cases the model only *proposes*; the code
validates against your `categories`/`labels` whitelist and rejects anything it
invents.

### Install and pull models

1. Install Ollama from **[ollama.com](https://ollama.com)** and make sure it's
   running (it listens on `http://localhost:11434`).
2. Pull models:
   ```bash
   ollama pull qwen2.5:7b   # classify + rescue (recommended)
   ollama pull all-minilm   # optional embeddings for nearest-merchant memory
   ```

| Model | Role | Size / speed | Notes |
| --- | --- | --- | --- |
| *(none)* | — | — | Default. Keyword rules only; unmatched → `needs-category`. |
| `all-minilm` | embeddings | ~45 MB, instant | "Nearest previous merchant" memory from your own history. |
| `qwen2.5:7b` | classify + rescue | ~4.7 GB, ~3 s/call | **Recommended.** Good accuracy, handles decimal-comma amounts. |
| 13B–27B models | classify + rescue | 8–17 GB, slower | Diminishing returns on short alerts. |

### Enable it in config.json

```json
"ollama": {
  "enabled": true,
  "url": "http://localhost:11434",
  "classify_model": "qwen2.5:7b",
  "rescue_model": "qwen2.5:7b",
  "embed_model": "all-minilm",
  "embed_threshold": 0.66
}
```

If `enabled` is `true` but Ollama isn't reachable, the run logs a notice and
continues without the LLM — it never crashes the pipeline.

---

## 7. First run

Always dry-run first. A dry run fetches and parses real emails and shows you what
it *would* create, but **writes nothing** to Wallet.

```bash
# 1. Dry run — review the output carefully.
python -m src.run_daily --dry

# Optional: widen the lookback window if you want to backfill.
python -m src.run_daily --dry --days=7
```

Check the dry-run table: amounts, merchants, accounts, currencies, and the
dedup status (`new` / `dup-exact` / `dup-split`). Only `new` items get created.

```bash
# 2. For real — creates pending (uncleared) records with the review label.
python -m src.run_daily
```

Then **open the Wallet app**, find the records by the autocaptured/review label,
and **approve, fix, or recategorize** them. They count toward your budget only
once you clear them.

If something looks wrong, you can roll back the auto-created pending records:

```python
# undo() deletes uncleared records carrying the autocaptured label;
# it never touches records you've already reviewed/cleared.
from src.config import load_config, get_token
from src.wallet_api import Wallet
from src import register
cfg = load_config(); w = Wallet(get_token(cfg), cfg["wallet"]["api_base"])
register.undo(w, cfg)
```

---

## 8. Scheduling

Run `src.run_daily` automatically once a day.

### Windows (Task Scheduler)

Use the helper in `scheduling/windows_task.ps1` (registers a daily task that runs
headless). Run it once in PowerShell (no admin needed):

```powershell
powershell -ExecutionPolicy Bypass -File scheduling\windows_task.ps1
```

Tips for a reliable headless run on Windows:

- Use a **real `pythonw.exe`** (not the Microsoft Store shim, which can fail
  under Task Scheduler).
- Run it **only when you're logged on** so the task inherits your environment and
  any synced folders, and you don't have to store a Windows password.
- Make sure `.wallet_token` and your `~/.wallet_imap` (or the env vars) are
  available to the account the task runs as.

### macOS / Linux (cron)

See **`scheduling/cron.md`** for a ready-to-paste crontab line. The gist:

```cron
# Run every day at 07:30. Adjust the paths.
30 7 * * *  cd /path/to/wallet-autocapture && /usr/bin/python3 -m src.run_daily >> logs/cron.log 2>&1
```

Make sure the cron environment can see your token/email secret (export them in a
wrapper script or use the `*_file` options, since cron has a minimal `PATH`/env).

---

## 9. Security & troubleshooting

### Security checklist

- ✅ `config.json`, `.wallet_token`, `*_imap`, `.env`, `logs/`, and data exports
  are all in `.gitignore`. **Verify** before your first `git push`:
  `git status --ignored` should list them as ignored.
- ✅ Use an **app password** for email, never your real password; revoke it from
  the provider if it leaks.
- ✅ The Wallet token is **read/write** — store it like a password; rotate it in
  Settings → API if exposed.
- ✅ Everything is created **`uncleared`** — review in the app before it counts.
- ✅ Sample emails in `sample/` must be **fictional/sanitized**.

### Troubleshooting

**`401 Unauthorized` from the Wallet API**
- Token is missing, wrong, or expired. Confirm `.wallet_token` (or
  `WALLET_API_TOKEN`) holds the *current* token from web.budgetbakers.com →
  Settings → API. No surrounding quotes or trailing newline issues — a single
  clean line. Regenerate if unsure. (Also confirm your account is **Premium**;
  the API is Premium-only.)

**IMAP authentication failed**
- You're almost certainly using your **normal password** instead of an **app
  password**, or **2FA isn't enabled** (app passwords require it), or **IMAP
  isn't enabled** in your mail settings. Recheck section 4. For Gmail
  specifically: 2-Step Verification on, app password created, IMAP enabled.

**Fetch connects but finds 0 alerts**
- Widen the window: `--days=7`. Confirm `email.senders` substrings actually match
  your alert sender's address. Confirm the alerts aren't being filtered into a
  folder the All-Mail/INBOX search doesn't cover.

**Some alerts show as "unparsed"**
- Your bank adapter's regexes don't cover that wording yet. Add a pattern in
  `src/banks/<name>.py` (and a sample case). If you enabled the LLM, the rescue
  step may recover it; otherwise it's skipped and logged for you to handle.

**Ollama enabled but not used / "not responding"**
- Make sure Ollama is installed and running (`http://localhost:11434`), the
  model is pulled (`ollama list`), and `ollama.url` matches. The tool degrades
  gracefully: if Ollama is down, it logs a notice and continues with rules-only
  categorization — your run still completes.

**Duplicates appearing in Wallet**
- Dedup needs a working token to read existing records. If the token is missing,
  the run can't compare and may re-create. Ensure the token is set so
  `dup-exact` / `dup-split` detection runs.
