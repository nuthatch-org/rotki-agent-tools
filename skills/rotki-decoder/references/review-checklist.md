# Review checklist

Walk this against the diff before handing over. Each rule is something a rotki maintainer asked
for in review, or wrote into rotki's own docs. `AGENTS.md` means `AGENTS.md` at the root of
rotki/rotki (develop, November 2026); `python-testing` means
docs.rotki.com/contribution-guides/python-testing. Reviewers: LefterisJP (Lefteris), yabirgb
(Yabir), nicholasyoder, prettyirrelevant.

Across the roughly 35 decoder PRs read for this (2024 to November 2026), the objections that
recur most are: missing VCR cassettes; events that leave historical balances wrong; a sibling
code path of the protocol not handled; attribution taken from the transaction sender; changelog
too long or promising too much; lint left red.

## Process

| # | Rule | Source |
|---|---|---|
| P1 | An issue exists and the PR body starts `Closes #N`. | rotki CONTRIBUTING.md; `.github/pull_request_template.md` |
| P2 | Branch from `develop` for features, `bugfixes` for a fix to released behaviour. | CONTRIBUTING.md |
| P3 | No AI slop, no drive-by rewording, no `Co-Authored-By` for an AI tool. | CONTRIBUTING.md; AGENTS.md "Committing", "Opening PRs" |
| P4 | `uv run make lint` green before pushing. Lefteris will not review a red head. | [#13649 comment](https://github.com/rotki/rotki/pull/13649#discussion_r4553543256) (pyright possibly-unbound), [#13704](https://github.com/rotki/rotki/pull/13704#discussion_r4617492398) (pylint redefined-outer-name from walrus in `parametrize`) |
| P5 | Rebased, squashed into tidy commits, no stray files (editor backups, scratch scripts). | [#13649 review](https://github.com/rotki/rotki/pull/13649) ("squash it all in one commit and generally tidy it up"), [#13649 comment](https://github.com/rotki/rotki/pull/13649#discussion_r4553549422) (a `-E` backup file) |
| P6 | Resolve a review comment only after doing it, or ask. | Lefteris, [#9631](https://github.com/rotki/rotki/pull/9631#discussion_r1999523404), [#8891](https://github.com/rotki/rotki/pull/8891#discussion_r1840881758) |
| P7 | Every new test that makes HTTP/RPC calls has `@pytest.mark.vcr(filter_query_parameters=['apikey'])`, its cassettes are in a rotki/test-caching PR on a branch with the same name as the rotki branch, and the PR links it. | AGENTS.md "Testing Strategy" and review guideline 9; python-testing "Syncing with the Cassettes Repository"; Lefteris on [#13660](https://github.com/rotki/rotki/pull/13660#discussion_r4543020838), [#13662](https://github.com/rotki/rotki/pull/13662#discussion_r4553713455), [#13662 again](https://github.com/rotki/rotki/pull/13662#discussion_r4560880762), [#13698](https://github.com/rotki/rotki/pull/13698#discussion_r4600381031) |
| P8 | A test that passed live is not evidence; replay against the cassette. | AGENTS.md review guideline 9, "Pre-handoff validation" |

## Decoder design

| # | Rule | Source |
|---|---|---|
| D1 | Scope with `addresses_to_decoders()`; `decoding_rules()` only when no address can scope it. | AGENTS.md "Decoder scope policy" |
| D2 | Decode from logs, not calldata. | Yabir, [#9631](https://github.com/rotki/rotki/pull/9631#discussion_r1995903745) ("as lefteris requested please don't rely on input data") |
| D3 | Do not infer the actor from `to_address`/`from_address`: a Safe or smart wallet is the `to`, the first event of a Safe tx is the Safe. Use the addresses the log names. | Yabir, [#9631](https://github.com/rotki/rotki/pull/9631#discussion_r1995976009), [#9631](https://github.com/rotki/rotki/pull/9631#discussion_r1995982788), [#9631](https://github.com/rotki/rotki/pull/9631#discussion_r1996018698); Lefteris, [#13690](https://github.com/rotki/rotki/pull/13690#discussion_r4597191123), [#13690](https://github.com/rotki/rotki/pull/13690#discussion_r4601757168) |
| D4 | Handle every path the protocol's contract can take for this user action, not only the one in the issue's transaction. | Lefteris, [#13695](https://github.com/rotki/rotki/pull/13695#discussion_r4602446924) (deferred cashback), [#13693](https://github.com/rotki/rotki/pull/13693#discussion_r4601547463) (stable-debt `Mint`), [#13704](https://github.com/rotki/rotki/pull/13704#discussion_r4616662785) (gas funding) |
| D5 | Do not depend on global DB metadata being populated (protocol field, periodic updates); identify from the logs. | Lefteris, [#13693](https://github.com/rotki/rotki/pull/13693#discussion_r4601540233) |
| D6 | Do not use current chain state (`latest`) as a fact about the past (e.g. `ownerOf` now for an old operation). | Lefteris, [#13690](https://github.com/rotki/rotki/pull/13690#discussion_r4600392904) |
| D7 | Do not query the DB for events of the same decoding batch: writes are buffered every 25 transactions and redecodes delete first. | Lefteris, [#13651](https://github.com/rotki/rotki/pull/13651#discussion_r4536814067) |
| D8 | Rewrite the transfer rotki already decoded; never leave it and add a second event for the same value. Synthesize an event only when its wallet effect is cancelled inside the same transaction. | Lefteris, [#13690](https://github.com/rotki/rotki/pull/13690#discussion_r4600403225), [#13690](https://github.com/rotki/rotki/pull/13690#discussion_r4603032742) |
| D9 | `ActionItem` fields come from the current `tx_log`; scan `all_logs` only to correlate distinct logs. Do not re-iterate logs the decoder loop already walks. | AGENTS.md "ActionItem matching rule"; Yabir, [#7999](https://github.com/rotki/rotki/pull/7999#discussion_r1619160490) |
| D10 | Leave approval events alone; they are used for infinite-approval analytics. | Yabir, [#9631](https://github.com/rotki/rotki/pull/9631#discussion_r1995972147) |
| D11 | Order with `maybe_reshuffle_events`; do not set `sequence_index` by hand. A swap reads spend, receive, fee, with nothing in between. | Yabir, [#9631](https://github.com/rotki/rotki/pull/9631#discussion_r1995914837), [#9631](https://github.com/rotki/rotki/pull/9631#discussion_r1996010418), [#9631](https://github.com/rotki/rotki/pull/9631#discussion_r1999058182) |
| D12 | Search loops `break` on the match and use `for ... else` to `log.error` when nothing matched. | Lefteris, [#7999](https://github.com/rotki/rotki/pull/7999#discussion_r1620419840); Yabir, [#9631](https://github.com/rotki/rotki/pull/9631#discussion_r1995945236) |
| D13 | Every unexpected state is logged as an error, with enough to find the transaction: the transaction (its `str` carries the chain), not a bare hash. | Lefteris, [#7999](https://github.com/rotki/rotki/pull/7999#discussion_r1620552568); Yabir, [#9631](https://github.com/rotki/rotki/pull/9631#discussion_r1995917230); prettyirrelevant, [#8905](https://github.com/rotki/rotki/pull/8905#discussion_r1848732791) |
| D14 | New log calls use lazy arguments or keyword fields, not f-strings (lint fails on new ones). | `tools/lint_new_logging_fstrings.py`, run by `make lint` |
| D15 | Never raise exceptions for control flow; return `None` and log. | Lefteris, [#8891](https://github.com/rotki/rotki/pull/8891#discussion_r1840668386) |
| D16 | A chain-agnostic class keys its data by chain as well as address. Read the chain and native token from `self.node_inquirer` / `evm_inquirer`, do not pass them in. | Lefteris, [#8905](https://github.com/rotki/rotki/pull/8905#discussion_r1848889000); Yabir, [#8036](https://github.com/rotki/rotki/pull/8036#discussion_r1629139220), [#8284](https://github.com/rotki/rotki/pull/8284#discussion_r1682694524) |
| D17 | A base class meant to be subclassed per chain inherits `ABC` and marks its hooks `@abstractmethod`. | Lefteris, [#8891](https://github.com/rotki/rotki/pull/8891#discussion_r1840699052) |
| D18 | A function that can raise documents it (`May raise:` in the docstring), even if the caller handles it. | Lefteris, [#8036](https://github.com/rotki/rotki/pull/8036#discussion_r1630305608) |
| D19 | Do not edit `rotkehlchen/data/eth_abi.json`. Small call ABIs for a module live in its `constants.py` as `Final[ABI]` (Ethena, merged November 2026). | Yabir, [#9631](https://github.com/rotki/rotki/pull/9631#discussion_r1995895925); `rotkehlchen/chain/ethereum/modules/ethena/constants.py` |
| D20 | Airdrop claim decoders also flip `has_decoder` in rotki/data's airdrop index. | AGENTS.md "Adding EVM protocol decoders" |

## Balances and accounting consequences

| # | Rule | Source |
|---|---|---|
| B1 | Protocol movements (`DEPOSIT_TO_PROTOCOL`, `WITHDRAW_FROM_PROTOCOL`, `GENERATE_DEBT`, `PAYBACK_DEBT`, liquidation losses) must open and close the same protocol bucket in the same asset. Model a conversion (ETH to stETH, native to wrapped) as its own events. | Lefteris, [#13649](https://github.com/rotki/rotki/pull/13649#discussion_r4547568013), [#13651](https://github.com/rotki/rotki/pull/13651#discussion_r4535838337), [#13651](https://github.com/rotki/rotki/pull/13651#discussion_r4535841446) |
| B2 | If a fix changes how already-decoded transactions should look, reset them for redecode in the unreleased data migration; do not tell users to redecode. | Lefteris, [#13655](https://github.com/rotki/rotki/pull/13655#discussion_r4536936702) |
| B3 | A third party paying into or out of someone's position moves value between accounts first; do not book it on the payer as their own position. | Lefteris, [#13690](https://github.com/rotki/rotki/pull/13690#discussion_r4600403225), [#13690](https://github.com/rotki/rotki/pull/13690#discussion_r4601751112) |
| B4 | Ask for, or add, a historical-balances regression test when B1 to B3 apply. | the same comments |
| B5 | The accounting engine is being rewritten (late 2026). Prefer an event type/subtype pair that already has a default rule; do not build new machinery on `AccountingPot` callbacks. | Lefteris closing [#13696](https://github.com/rotki/rotki/pull/13696) |

## Constants and style

| # | Rule | Source |
|---|---|---|
| S1 | Address literals checksummed. `string_to_evm_address` does not checksum. | AGENTS.md "Memories", "Testing Strategy"; `tools/lint_checksum_addresses.py` |
| S2 | Topics and selectors are byte literals (`Final`), verified by round trip; no `bytes.fromhex` in the final constant. Reuse existing ones. | AGENTS.md "Hex / bytes constants policy"; `tools/find-duplicate-constants` |
| S3 | Backend constants are `Final`. | AGENTS.md "Development Tips" 6 |
| S4 | No constant for a value used once; a constant for a value repeated (decoder and test). | Lefteris, [#8891](https://github.com/rotki/rotki/pull/8891#discussion_r1840641906), [#8905](https://github.com/rotki/rotki/pull/8905#discussion_r1848899601) |
| S5 | No temporary variable for something used once (`tx_log = context.tx_log`); inline with the walrus in tests. | Yabir, [#9631](https://github.com/rotki/rotki/pull/9631#discussion_r1995944518); AGENTS.md "Rotki Backend Style Preferences" |
| S6 | Docstrings one line where possible, explaining what the code cannot say; no copy-pasted docstrings; no comment that restates the function name. | Yabir, [#9631](https://github.com/rotki/rotki/pull/9631#discussion_r1999011883), [#9631](https://github.com/rotki/rotki/pull/9631#discussion_r1999039506) |
| S7 | `elif` for exclusive branches; no blank first line in a file; keyword arguments for all or none. | Yabir, [#9631](https://github.com/rotki/rotki/pull/9631#discussion_r1996016842), [#8036](https://github.com/rotki/rotki/pull/8036#discussion_r1629748114), [#8036](https://github.com/rotki/rotki/pull/8036#discussion_r1629749855) |
| S8 | Narrow exceptions (`RemoteError`, `DeserializationError`), never `except Exception`. | AGENTS.md "Rotki Backend Style Preferences" 1 |

## User-facing text

| # | Rule | Source |
|---|---|---|
| U1 | Notes name the protocol as users know it ("Rainbow", not "Rainbow swaps"), use the token symbol, stay consistent in number, and carry no meaningless detail ("for 0 ETH"). | Lefteris, [#9631](https://github.com/rotki/rotki/pull/9631#discussion_r1999525646), [#8891](https://github.com/rotki/rotki/pull/8891#discussion_r1838599304), [#8891](https://github.com/rotki/rotki/pull/8891#discussion_r1840683452) |
| U2 | Counterparty has a label and an image in `frontend/app/public/assets/images/protocols/`. | Lefteris, [#9631](https://github.com/rotki/rotki/pull/9631#discussion_r1999521588) |
| U3 | Changelog: one short line at the top, `:feature:`/`:bug:` and the issue number. | Lefteris, [#13649](https://github.com/rotki/rotki/pull/13649#discussion_r4547648872), [#9631 review](https://github.com/rotki/rotki/pull/9631) ("Also add a changelog entry") |
| U4 | Changelog promises only what the decoder does (e.g. only transactions the tracked address is part of are found). | Lefteris, [#13694](https://github.com/rotki/rotki/pull/13694#discussion_r4602196160) |
| U5 | Changelog names every chain the decoder is enabled on. | Lefteris, [#13704](https://github.com/rotki/rotki/pull/13704#discussion_r4616666422) |
| U6 | Changelog describes what users will see on that branch; on `bugfixes` historical balances are behind `ROTKI_ACCOUNTING_UPDATE`, so do not mention them. | Lefteris, [#13651](https://github.com/rotki/rotki/pull/13651#discussion_r4537236353), [#13655](https://github.com/rotki/rotki/pull/13655#discussion_r4537403237) |

## Tests

| # | Rule | Source |
|---|---|---|
| T1 | One test per user action, on a real transaction, asserting the complete event list with `==`. | every merged decoder test, e.g. `rotkehlchen/tests/unit/decoders/test_ethena.py` |
| T2 | The assertion proves the correct booking, not only that the old wrong one is gone. | Lefteris, [#13693](https://github.com/rotki/rotki/pull/13693#discussion_r4601540233) |
| T3 | Each test fails on the base branch; the PR says so. Checking by breaking the code on purpose is welcome. | #13693 and #13695 bodies (cargopete), #13649 and #13662 bodies (officialasishkumar), all merged |
| T4 | Do not seed the DB to make a decoder pass; decode the prior transactions in the same call. | Lefteris, [#13651](https://github.com/rotki/rotki/pull/13651#discussion_r4536817127) |
| T5 | Assert `event_type` as well as `event_subtype`; a test that passes either way guards nothing. | Lefteris, [#13698](https://github.com/rotki/rotki/pull/13698#discussion_r4600376511) |
| T6 | No walrus inside `@pytest.mark.parametrize` (it leaks module globals); use named `Final` module constants. | Lefteris, [#13704](https://github.com/rotki/rotki/pull/13704#discussion_r4617492398) |
| T7 | New Ethereum decoders and counterparties are listed in `test_main.py`. | #13649, #13662 (both touched `test_main.py`) |
| T8 | Tests that build `DecoderContext` in memory and make no calls need no VCR marker. | yabirgb, [#13698](https://github.com/rotki/rotki/pull/13698#discussion_r4601114008) |
