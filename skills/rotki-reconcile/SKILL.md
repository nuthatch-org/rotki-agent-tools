---
name: rotki-reconcile
description: Check a rotki profit and loss CSV export (all_events.csv or the csv.zip rotki writes) for data gaps before the user files taxes or hands it to an accountant. Flags events valued at a zero or missing price, disposals with missing acquisitions (no cost basis), long quiet stretches that may be uncovered history, and, if the user runs a nuthatch address-history nest, on-chain transactions that never reached the export. Use when the user asks whether their rotki PnL report or tax export is complete, correct or safe to file. Reports gaps only; gives no tax advice and never says what to declare.
---

# rotki-reconcile

Reads only the file the user exported themselves. It does not call rotki's API and does not
fetch history from rotki. The optional cross-check reads the user's own nest, on their machine.

## 1. Get the export

In rotki, the PnL report page has an export button that writes `csv.zip` containing
`all_events.csv` (`rotkehlchen/accounting/export/csv.py`). Ask for its path. Either file works.

Columns, from `ProcessedAccountingEvent.to_exported_dict()`:
`type, notes, location, timestamp, asset_identifier, free_amount, taxable_amount, price,
pnl_taxable, pnl_free, cost_basis_taxable, cost_basis_free, asset, direction, location_path`.
`notes` carries the explorer link with the tx hash for on-chain events. With the summary option on,
totals, the rotki version and the accounting settings follow after two blank rows.

The delimiter is the `csv_export_delimiter` setting and dates follow `date_display_format`
(default `%d/%m/%Y %H:%M:%S %Z`). The script detects `,` `;` tab and `|`, and tries common date
formats; pass `--delimiter` or `--date-format` if it reports unparsed timestamps.

## 2. Run

```bash
python3 -I scripts/rotki_reconcile.py ~/Downloads/csv.zip --from 2025-01-01 --to 2025-12-31
python3 -I scripts/rotki_reconcile.py all_events.csv --gap-days 60 --json
```

`--from`/`--to` are the report period; they let the script flag a location that starts late or
stops early. `--max-rows` limits each list in text output; `--json` has everything.

## 3. Read the results to the user

- **Zero or missing price.** rotki values the event at zero. An acquisition at zero gives a zero
  cost basis to whatever is later sold from it. Spam airdrops belong here; real assets do not.
  Fix in rotki: a manual historical price, or ignore the spam asset, then regenerate the report.
- **Missing acquisitions.** rotki writes "Incomplete cost basis information for spend." when it
  found no earlier acquisition for some of the amount sold (`CostBasisInfo.to_string`). The script
  also sums the matched acquisitions and flags partial cover. Usual causes: an exchange or wallet
  not added to rotki, history before the period not imported, or a transfer between own accounts
  that was not matched.
- **Possible missing acquisitions (formula export).** With "Export formulas in the PnL CSV" on
  (the default), a cost basis cell becomes a formula and the marker can disappear. Ask the user to
  turn it off and export again to confirm.
- **Quiet stretches.** Per location, gaps of `--gap-days` (default 90) or more between events, and
  from the period edges. Not proof of anything: ask whether the account was really idle then. If
  it was not, run `rotki-doctor` on the logs to see whether that range was never covered.
- **Not visible in a CSV.** A spend whose price rotki could not find is left out of the export
  entirely. The report page in rotki lists those under Missing Prices, and missing acquisitions
  under Missing Acquisitions. Ask the user to check those two lists in the app; they are only kept
  for the most recently generated report.

Say what is missing and where. Do not say what is taxable, which method to use, or what to declare.
If the user asks, that is a question for their tax adviser.

## 4. Optional: cross-check against a nuthatch nest

Only if the user already runs a nuthatch nest in address-history (rotki) mode for their accounts.
Its `/api` speaks the Etherscan account shape that rotki's nuthatch indexer reads:

```bash
python3 -I scripts/rotki_reconcile.py all_events.csv --from 2025-01-01 --to 2025-12-31 \
  --nest http://127.0.0.1:8288 --address 0xTheirAccount --actions txlist,tokentx,txlistinternal
```

It resolves the period to blocks with `getblocknobytime` (or takes `--startblock`/`--endblock`),
lists each account's transactions, and reports the hashes not found in the export's notes
(`--location`, default `ethereum`). A nest that refuses a range has not indexed it; the script
reports that and the cross-check is incomplete for it.

With the nest's MCP server or its `/sql` endpoint instead, query the transaction hashes of the
user's addresses over the period yourself, save them to a file (any text; hashes are extracted),
and pass `--onchain-hashes FILE`. Keep that file on the user's machine.

Not every transaction produces a PnL event: approvals, ignored assets and non-taxable actions do
not. Treat each missing hash as something to look at, and a missing transfer or trade as the gap.

## Not this skill

- Why rotki is slow, stuck or missing history in the first place: `rotki-doctor`.
- A transaction rotki decodes wrongly: `rotki-decoder`.
