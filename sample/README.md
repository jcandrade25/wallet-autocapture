# Sample data

Fictitious bank-alert text used to exercise the `bancolombia` adapter end to end.
Everything here is **fake** — invented names, account numbers, payment keys and
amounts. No real personal data lives in this repo (see the privacy rule in the
project root).

## Files

| File | What it is |
| --- | --- |
| `sample_alerts.json` | An array of ~6 alert strings (Spanish, Bancolombia/RappiPay style) covering: COP purchase, USD purchase, transfer to a person, internal self-transfer, credit-card payment, and a QR payment. |
| `expected.csv` | The golden output the adapter should produce from those alerts, one row per alert. Columns: `date,time,type,amount,merchant,account_hint,currency`. |

## The 6 cases

| # | Alert kind | type | amount | currency |
| --- | --- | --- | --- | --- |
| 1 | Card purchase (COP) | `expense` | 42300 | COP |
| 2 | Card purchase (USD) | `expense` | 9.99 | USD |
| 3 | Transfer to a person (Bre-b key) | `expense` | 85000 | COP |
| 4 | Internal transfer received from yourself | `internal` | 150000 | COP |
| 5 | Credit-card payment from own account | `internal` | 310000 | COP |
| 6 | QR payment | `expense` | 18500 | COP |

## Identity the sample is written against

The alerts assume the placeholder identity from `config.example.json`:

- `self_names`: `["YOUR NAME", "your.payment.key"]`
- `own_accounts`: `["1234", "5678"]`

That is why case 4 (the self-transfer) names **`YOUR NAME`** as the sender and
uses the key **`your.payment.key`** — the adapter recognizes it as one of your
own names and classifies the movement as `internal` rather than income. Case 5
moves money *between* `5678` and card `1234`, both of which are your own
accounts, so it is `internal` too. If you change your real `self_names` /
`own_accounts` in `config.json`, edit the sample to match before re-testing.

## How to run

From the repo root:

```bash
python test_adapter.py            # PASS/FAIL per case, exits non-zero on any miss
python test_adapter.py --verbose  # also dumps the full parsed txn for each case
```

`test_adapter.py` loads `sample_alerts.json`, runs every snippet through
`src.parser.to_txns(...)` (the canonical pipeline, which dispatches to the
adapter named in `cfg["bank_adapter"]`), and diffs the result against
`expected.csv`. If only the adapter module exists it falls back to calling
`src.banks.bancolombia.parse(...)` directly; if the pipeline has not been built
yet it prints a `SKIP` instead of a hard failure.

### Comparison rules

- **Strict** on: `date`, `time`, `type`, `amount`, `currency`.
- `amount` is normalized first: USD keeps two decimals (`9.99`), COP is an
  integer (`42300`). The sign lives in the Wallet record layer, not here — a
  NORMALIZED TXN amount is always positive.
- `account_hint` is compared on its **last 4 digits**, so `*5678`, `5678` and
  `Card 5678` all match.
- `merchant` is compared case-insensitively for `expense` / `income` rows. For
  `internal` rows the adapter may choose its own self-transfer wording (e.g.
  "Self transfer"), so only the `internal` classification is asserted.

## Adding a new case

1. Append a fictitious alert string to `sample_alerts.json`.
2. Append the expected row to `expected.csv` (same column order).
3. Re-run `python test_adapter.py`.

Keep new alerts fake. When in doubt, use obviously-invented merchants and
round-number amounts.
