# wallet-autocapture

Turn the **transaction alert emails your bank already sends you** into **pending
records in your [BudgetBakers Wallet](https://web.budgetbakers.com) account** —
automatically, with deduplication, a mandatory human-review step, and *optional*
local-LLM categorization.

No more typing every coffee and taxi into your budget app. The alert hits your
inbox, this tool reads it, parses the amount/merchant/account, and drops a
record into Wallet marked **uncleared** (pending) with a review label. You glance
at the app, approve what's right, fix what isn't. Your budget stays current with
almost zero manual entry.

> **Privacy first.** Everything personal — your token, account UUIDs, names,
> payees, email password — lives in `config.json`, which is **git-ignored**.
> Nothing personal is ever committed. This repo ships only placeholders.

---

## What it does

- **Reads bank alert emails over IMAP** (read-only) — no forwarding, no scraping
  a web portal.
- **Parses each alert** into a normalized transaction (date, time, amount,
  merchant, account, currency, expense/income/internal) using a per-bank
  *adapter* (regexes for your bank's exact wording).
- **Deduplicates** against what's already in Wallet — exact matches *and* "split"
  matches (one alert that already exists as several smaller records), so re-runs
  are idempotent.
- **Creates records via the official Wallet REST API**, always as
  `uncleared` (pending) + an `autocaptured` label, so **you review before it
  counts**. The tool never auto-approves anything.
- **Routes foreign-currency (USD) purchases** to a USD sub-account and labels
  them, if you configure one.
- **Categorizes** with deterministic keyword rules, and *optionally* a **local
  LLM via Ollama** for the tricky ones. The LLM only ever *proposes* a category
  from your own whitelist; the code validates and rejects anything invented. If
  the LLM is off or unreachable, the item just gets a "needs-category" label —
  nothing breaks.
- **Detects when your monthly statement arrives** so you can reconcile.
- **Degrades gracefully**: missing email credentials, no token, or no Ollama
  each disable just their feature — the rest still runs.

---

## End-to-end flow

```
   ┌────────────┐   IMAP    ┌────────────┐   adapter   ┌──────────────────┐
   │ Bank sends │  (read    │  fetch     │   regexes   │  NORMALIZED txns │
   │ alert email│ ────────▶ │  snippets  │ ──────────▶ │ date/amt/merchant│
   └────────────┘  only)    └────────────┘             └────────┬─────────┘
                                                                 │
                          ┌──────────────────────────────────────┘
                          ▼
                  ┌────────────────┐   compare vs Wallet   ┌──────────────┐
                  │     dedup      │ ◀──────────────────── │ existing recs│
                  │ new/dup/split  │                       │  (API GET)   │
                  └───────┬────────┘                       └──────────────┘
                          │ only "new"
                          ▼
   ┌──────────────────────────────────┐   POST /records   ┌────────────────┐
   │  build Wallet records:           │ ────────────────▶ │   Wallet app   │
   │  account map, USD routing,       │                   │  status =      │
   │  category (rules + opt. LLM),    │                   │  UNCLEARED     │
   │  state=uncleared, +review label  │                   │  +review label │
   └──────────────────────────────────┘                   └───────┬────────┘
                                                                   │
                                                                   ▼
                                                       ┌───────────────────────┐
                                                       │  👤 YOU review in the  │
                                                       │  app: approve / fix /  │
                                                       │  recategorize. Then it │
                                                       │  counts in your budget.│
                                                       └───────────────────────┘
```

In words:

1. **Email** — your bank sends a transaction alert; it lands in your inbox.
2. **Fetch** — `email_fetch` pulls recent alert snippets over IMAP (read-only).
3. **Parse** — your bank `adapter` turns each snippet into a normalized
   transaction; identity rules tag your own transfers as `internal`.
4. **Dedup** — candidates are compared against existing Wallet records; only
   genuinely *new* ones survive.
5. **Wallet (pending)** — new records are POSTed as `uncleared` with the
   review label (and USD routing/labels where applicable).
6. **Human review** — you open Wallet, approve the correct ones, fix or
   recategorize the rest. *This step is the point.* Nothing is auto-approved.
7. **Budget** — once you clear them, they flow into your normal budgeting.

---

## Architecture

Plain Python 3, standard library only (the LLM features talk to Ollama over
HTTP, also via stdlib). Modules under `src/`:

| Module | Responsibility |
| --- | --- |
| `src/config.py` | Load `config.json`; resolve the Wallet token and email secret from env/file. |
| `src/wallet_api.py` | Thin `Wallet` client over the REST API: get/create/patch records, list accounts/labels/categories (handles pagination). |
| `src/email_fetch.py` | IMAP fetch of alert snippets + monthly-statement detection. Locale-independent mailbox selection. |
| `src/banks/<name>.py` | **Bank adapter.** `SENDERS` + `parse(text) -> normalized txn | None`. Encapsulates your bank's wording and amount formats. Ships with `bancolombia`; `TEMPLATE.py` to start your own. |
| `src/banks/__init__.py` | `get_adapter(name)` — dynamic import of the configured adapter. |
| `src/parser.py` | `clean_text(raw_email_bytes)` (MIME → text) and `to_txns(snippets, cfg)` (applies the adapter + identity rules). |
| `src/dedup.py` | `classify_dups(candidates, existing)` → tags each as `new` / `dup-exact` / `dup-split`. |
| `src/classify.py` | Optional LLM categorization: `chat_json(...)` + `classify(merchant, amount, cfg)` with a hard whitelist; returns `None` if the model invents a label. |
| `src/register.py` | `build_records(txns, cfg)` (account/USD/category/label mapping), `commit(...)`, and `undo(...)` (delete uncleared autocaptured records). |
| `src/run_daily.py` | Orchestrates the whole daily run. Flags: `--dry`, `--days=N`. Logs to `logs/` and a `capturas_log.md`. |
| `setup_wizard.py` | Interactive: connects with your token, discovers accounts/labels/categories, writes `config.json` from the example. Never prints your token. |

### Data contracts (what flows between modules)

- **Normalized transaction** (what an adapter returns):
  ```python
  {"date": "YYYY-MM-DD", "time": "HH:MM",
   "type": "expense" | "income" | "internal",
   "amount": <positive float>, "merchant": "<name/destination>",
   "account_hint": "<last-4 digits or text>",
   "currency": "COP" | "USD", "channel": "<optional>"}
  ```
  or `None` if the text is not a transaction.
- **Wallet record** (what gets POSTed):
  ```python
  {"accountId", "amount": {"value": <neg=expense / pos=income>, "currencyCode"?},
   "recordDate": "<ISO 8601 with offset>", "paymentType",
   "recordState", "categoryId"?, "labelIds": [...], "counterParty"}
  ```

---

## Quickstart

```bash
# 1. Get the code
git clone https://github.com/<your-org>/wallet-autocapture.git
cd wallet-autocapture

# 2. (No pip deps for core use — Python 3.8+ stdlib only.)
python --version

# 3. Put your Wallet API token in .wallet_token (one line). See INSTALL.md §2.
#    Generate it at web.budgetbakers.com -> Settings -> API (Premium).

# 4. Run the setup wizard: it discovers your accounts/labels/categories
#    and writes config.json from config.example.json.
python setup_wizard.py

# 5. Edit config.json by hand: identity, category_rules, accounts mapping,
#    and the email section (IMAP host/user/senders + app password). See INSTALL.md §3–4.

# 6. Dry run — see what WOULD be created, write nothing:
python -m src.run_daily --dry

# 7. For real — creates pending (uncleared) records you then review in the app:
python -m src.run_daily
```

Each user **chooses or writes their own bank adapter** (`src/banks/`). The repo
ships a `bancolombia` adapter and a `TEMPLATE.py`; point `config.bank_adapter`
at yours. See **INSTALL.md section 5**.

---

## Models (optional LLM)

The LLM is **optional and local** (runs on your machine via
[Ollama](https://ollama.com); nothing is sent to a third party). It does two
things: (a) suggest a category for merchants the keyword rules miss, and
(b) "rescue" an alert the regex couldn't parse. In **every** case the model only
*proposes* — the code validates the answer against your configured
categories/labels and discards anything off-list. **Default mode is no-LLM.**

| Mode | What you install | Disk / speed | Use it when |
| --- | --- | --- | --- |
| **No LLM** (default) | nothing | — | You're fine with keyword-rule categories; unmatched items get a `needs-category` label for you to fix in the app. Fully functional. |
| **`all-minilm`** (embeddings) | `ollama pull all-minilm` | ~45 MB, instant | You want a "nearest previous merchant" memory to suggest categories from your own history. Cheap, optional. |
| **`qwen2.5:7b`** (recommended) | `ollama pull qwen2.5:7b` | ~4.7 GB, ~3 s/call | Best balance: handles classify **and** parse-rescue well, including Colombian/European decimal-comma amounts. Set as both `classify_model` and `rescue_model`. |
| Larger (e.g. 13B–27B) | `ollama pull <model>` | 8–17 GB, slower | Marginal accuracy gains for harder text; usually not worth it for short bank alerts. |

Enable it by setting `"ollama": { "enabled": true, ... }` in `config.json`. If
Ollama is enabled but not running, the tool prints a notice and continues
without it. See **INSTALL.md section 6** for setup.

---

## Safety model

- Created records are **always** `uncleared` (pending) + the `autocaptured`
  label. Nothing is auto-approved — review is mandatory and is the whole design.
- **`--dry` runs write nothing.** Always dry-run first.
- `register.undo(...)` can bulk-delete the uncleared autocaptured records if a
  run went wrong (it never touches records you've already reviewed/cleared).
- Re-runs are **idempotent** thanks to dedup; running twice won't double-book.

---

## Install

Full step-by-step setup — token, wizard, IMAP app password, bank adapter,
optional models, scheduling, and troubleshooting — is in **[INSTALL.md](INSTALL.md)**.

## License

[MIT](LICENSE) © 2026 wallet-autocapture contributors.
