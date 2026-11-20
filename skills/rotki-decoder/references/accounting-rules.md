# Missing accounting rules

An event "missing an accounting rule" is decoded but PnL skips it. Three cases, cheapest first.

## How rotki picks a rule

`AccountingRulesManager.get_event_settings` (`rotkehlchen/accounting/rules.py`) looks up, in order:
an event-specific rule the user made; a rule for (type, subtype, counterparty); for on-chain
events, a rule for (type, subtype) with no counterparty. The default rules are not in rotki/rotki.
They live in rotki/data as `updates/accounting_rules/v<N>.json`, gated per rotki version by
`updates/info.json`, and reach users through the data updater (`rotkehlchen/db/updates.py`,
`update_accounting_rules`). Tests download the same files (`rotkehlchen/tests/fixtures/accounting.py`).
Callbacks for multi-event logic come from per-module `accountant.py` files.

```sh
python3 <skill>/scripts/rule_lookup.py "<type>" "<subtype>" [counterparty]
```

## Case 1: the decoder chose an uncovered pair

The usual fix is in the decoder, not in the rules: use the type/subtype pair rotki already
treats the right way. Ethena (#13662) and Lido CSM (#13649) both shipped with "No new rules are
needed", because they used `deposit / deposit for wrapped`, `withdrawal / redeem wrapped`,
`deposit / deposit to protocol` and so on, which have generic rules. Say so in the PR body under
an "Accounting" heading.

## Case 2: a genuinely new kind of event

A rule goes to rotki/data, not rotki/rotki. Shape (from rotki/data#276, Giveth donations):

```json
{
  "event_type": "spend",
  "event_subtype": "donate",
  "counterparty": "giveth",
  "taxable": true,
  "count_entire_amount_spend": false,
  "count_cost_basis_pnl": true,
  "accounting_treatment": null
}
```

`accounting_treatment` is `null` or a `TxAccountingTreatment`; the files spell them `"SWAP"` and
`"basis transfer"` (v1 to v4 and v10), and the deserializer accepts both.
`counterparty: null` makes it the generic rule for that pair.

Where it goes:

1. `rule_lookup.py --list-latest` shows the newest file and the version limits.
2. If the newest file's `min_version` is a rotki version not yet released, append the rule to
   that file (rotki/data#286 added to v12 that way).
3. If it is already released, add `v<N+1>.json` and a `limits` entry with `min_version` set to
   the release that will carry the decoder (rotki/data#276). Maintainers bump `latest` in
   `info.json` at release time (rotki/data#291); leave it unless they ask.
   As of 2026-11-20 the newest file is v12 with `min_version` 1.44.0, and 1.44.0 shipped on
   2026-08-21, so a new rule needs a v13.
4. Link the rotki PR from the rotki/data PR body ("Needed for rotki/rotki#..." as in #286).

Taxability is a judgement for the user and the maintainers. Copy the treatment of the closest
existing rule and say which one in the PR.

## Case 3: rotki is changing underneath

Lefteris, closing rotki/rotki#13696 (2026-11): accounting and PnL are being rewritten; the
`AccountingPot` callbacks and `TxAccountingTreatment` consumption are going away, though the new
system keeps accounting rules. Do not build new machinery on the old engine. Rules in rotki/data
and correct decoder types remain the safe contribution.
