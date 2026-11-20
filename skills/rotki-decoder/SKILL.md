---
name: rotki-decoder
description: Draft an upstream rotki/rotki contribution for an EVM transaction that rotki leaves undecoded (plain send/receive, or nothing), decodes wrongly, or decodes into an event with no accounting rule. Given a tx hash and chain, produce a decoder (or a fix, or a rotki/data accounting rule) plus a VCR decoder test, changelog line and PR body in rotki's own idiom, checked against the review objections rotki maintainers actually raise. Use when the user names a rotki transaction, protocol or "missing accounting rule" and wants it fixed in rotki itself. Not for Solana decoders, exchange importers or local-only workarounds.
---

# rotki-decoder

Turns "rotki shows this transaction as a plain transfer" into a PR the rotki team can merge
without rewriting it. Everything here comes from rotki's own docs and from review comments on
merged decoder PRs; each rule in the checklist links its source.

## Inputs

- A tx hash and its chain (rotki's chain module name: `ethereum`, `optimism`, `arbitrum_one`,
  `base`, `polygon_pos`, `gnosis`, `scroll`, `binance_sc`, ...).
- A rotki checkout, synced with `uv sync`, on a branch off `develop` (new protocol or feature)
  or `bugfixes` (wrong decoding in the current release). Never write into someone else's worktree.
- A rotki issue number. CONTRIBUTING.md requires an issue before a PR. If there is none, draft one
  for the user to file; do not file it yourself.

## What it produces

1. Decoder code (`decoder.py`, `constants.py`, counterparty, registration) or a fix to an
   existing decoder, or, for a missing accounting rule only, a rotki/data rule entry.
2. A decoder test with `@pytest.mark.vcr`, asserting the complete expected event list.
3. One changelog line in `docs/changelog.rst`.
4. A PR body in the shape maintainers merge, and the test-caching PR text.

## What needs a human

- **Recording cassettes.** It needs network, an Etherscan key (or the test default) and a run on
  the user's machine. rotki's AGENTS.md makes the human the owner of recording unless they
  explicitly hand it over. Hand over the exact `make create-cassette ...` command, then replay
  the test offline and fix mismatches before calling it done.
- **Pushing to rotki/test-caching** (a fork PR for outside contributors), opening the rotki PR,
  signing the CLA, filing the issue.
- **Judgement on accounting.** Whether something is taxable is the user's and the maintainers'
  call, not the skill's.

Report anything unrun as pending. A test that was never replayed against its cassette is not a
passing test.

## How to use

1. Read `references/workflow.md` and follow it step by step.
2. Before handing anything over, walk `references/review-checklist.md` against the diff.
3. When unsure what "good" looks like, read `references/worked-example-etherfi.md`, built from a
   real merged PR (rotki/rotki#13695).
4. For a missing accounting rule, `references/accounting-rules.md`.

Scripts (stdlib only; run `inspect_tx.py` under rotki's `uv run` so addresses come out checksummed):

- `scripts/inspect_tx.py --chain <chain> <tx_hash> --rotki <checkout>`: call, fee, every log with
  topic signature and the byte literal rotki uses, and where rotki already knows each contract.
- `scripts/rule_lookup.py <type> <subtype> [counterparty]`: which default accounting rule in
  rotki/data covers an event.

Both make network calls (an RPC, api.openchain.xyz, raw.githubusercontent.com). Pass `--rpc`
with the user's own node if they would rather not show a public RPC their transaction.
