# Workflow

Paths are relative to the rotki checkout. `<chain>` is the module name (`ethereum`, `scroll`,
...), `<proto>` the protocol module (`etherfi`, `spark`, ...).

## 0. Set up

- Branch from `develop` for a new protocol or a feature, from `bugfixes` for wrong decoding in
  the released version (CONTRIBUTING.md; the compound and paraswap fixes #13651, #13655, #13698
  went to `bugfixes`, new decoders #13660, #13695, #13711 to `develop`).
- Name the branch now and keep it: the test-caching branch must have exactly the same name, or
  CI cannot find the cassettes (docs.rotki.com python-testing guide, "Syncing with the Cassettes
  Repository").
- `uv sync`. Lint and tests run through `uv run`. The docs still mention `pytestgeventwrapper.py`;
  it no longer exists, use `uv run pytest`.

## 1. Say which problem it is

| What rotki shows | Path |
|---|---|
| Plain send/receive, or only the gas fee, for a protocol interaction | new decoder or new module |
| A decoder ran but the type/subtype/amount/attribution is wrong | fix the existing decoder, `:bug:` |
| Decoded fine, but PnL lists it as missing an accounting rule | `accounting-rules.md`; often no code at all |

If the transaction never appears in rotki at all, that is transaction discovery, not decoding,
and out of scope (Lefteris on #13649 and #13694: an address that appears only in a log topic is
never discovered).

## 2. Read the transaction

```sh
uv run python <skill>/scripts/inspect_tx.py --chain <chain> <tx_hash> --rotki .
```

It prints the call selector, the fee, every log with its signature and byte literal, and which
files in `rotkehlchen/chain` and `rotkehlchen/constants` already name each contract or topic. Write down:

- which logs belong to the protocol and which are plain token transfers rotki already decodes;
- which addresses in the logs are the user's (they decide `location_label`);
- the timestamp in ms, each log index (usually the `sequence_index` of its event) and the fee.

For the ABI use Sourcify, as rotki's AGENTS.md says:
`https://repo.sourcify.dev/contracts/full_match/<chainId>/<address>/metadata.json` (`output.abi`),
or the block explorer's verified source. Read the contract, not only this one transaction:
list every event the user flow can emit. Both of cargopete's 2026-11 decoder PRs drew a review that
was exactly a missed sibling path (deferred cashback in #13695, the stable-debt Mint in #13693).

## 3. Find the existing home

- `inspect_tx.py --rotki` already greps addresses and topics. Also grep the protocol name and
  `CPT_` constants: `grep -rn "CPT_<NAME>\|'<name>'" rotkehlchen/chain`.
- An existing module for the protocol means extend it, in its style. A new module goes:
  - one chain: `rotkehlchen/chain/<chain>/modules/<proto>/{__init__.py,constants.py,decoder.py}`,
    class `<Proto>Decoder`. Discovery is automatic: the class name is the module path in
    CamelCase plus `Decoder` (`rotkehlchen/chain/decoding/decoder.py`, `_initialize_decoders`).
  - several chains, different addresses: a common class in
    `rotkehlchen/chain/evm/decoding/<proto>/decoder.py` (`<Proto>CommonDecoder`) and a thin
    subclass per chain under each chain's modules, passing that chain's addresses
    (pattern: `rotkehlchen/chain/base/modules/cctp/decoder.py`).
  - several chains, same address everywhere: a common class registered in
    `_add_builtin_decoders` of `rotkehlchen/chain/evm/decoding/decoder.py`, gated on a set of
    `ChainID`s (pattern: ether.fi cash and Frankencoin savings).
- A new Ethereum decoder or counterparty must be added to the sets in
  `rotkehlchen/tests/unit/decoders/test_main.py` (`test_decoders_initialization`), as #13649 and
  #13662 did.

## 4. Write constants

- Counterparty: `CPT_<PROTO>: Final = '<proto>'` and a `CounterpartyDetails(identifier=..., label=...,
  image='<proto>.png')`. The image goes in `frontend/app/public/assets/images/protocols/`; a missing
  image was a review blocker on #9631.
- Addresses: checksummed literals, `Final`. `string_to_evm_address` does not checksum
  (AGENTS.md "Memories"); `tools/lint_checksum_addresses.py` runs in `make lint`.
- Topics and selectors: byte literals, `Final`, copied from on-chain hex and round-tripped
  (AGENTS.md "Hex / bytes constants policy"). `inspect_tx.py` prints the literal. Reuse a topic
  that already exists (`rotkehlchen/chain/evm/constants.py` has `DEPOSIT_TOPIC`,
  `WITHDRAW_TOPIC_V3`, ...); `tools/find-duplicate-constants` fails lint on a duplicate.
- Assets are compared by identifier string, not new asset constants (AGENTS.md).
- A value used once does not need a constant (Lefteris, #8891); a value used in the decoder
  and the test does (Lefteris, #8905).

## 5. Write the decoder

Start from the smallest sibling that does the same kind of thing and match it. Shape:

```python
class <Proto>Decoder(EvmDecoderInterface):

    def _decode_<thing>(self, context: DecoderContext) -> EvmDecodingOutput:
        if context.tx_log.topics[0] != <TOPIC>:
            return DEFAULT_EVM_DECODING_OUTPUT

        # read the user, token and amount from this log, then rewrite the matching transfer
        for event in context.decoded_events:
            if (<type, subtype, location_label, asset, amount match>):
                event.event_type = ...
                event.event_subtype = ...
                event.counterparty = CPT_<PROTO>
                event.notes = f'...'
                break
        else:
            log.error('Failed to find the <thing> transfer in %s', context.transaction)

        return DEFAULT_EVM_DECODING_OUTPUT

    # -- DecoderInterface methods

    def addresses_to_decoders(self) -> dict[ChecksumEvmAddress, tuple[Any, ...]]:
        return {<CONTRACT>: (self._decode_<thing>,)}

    @staticmethod
    def counterparties() -> tuple[CounterpartyDetails, ...]:
        return (<COUNTERPARTY_DETAILS>,)
```

Rules that shape it (sources in the checklist):

- Scope by `addresses_to_decoders()`; `decoding_rules()` runs for every log of every transaction
  and is a last resort.
- Decode from logs. Do not trust calldata, `transaction.to_address` or `from_address` for who
  did what: Safes, smart wallets and routers make them lie.
- Rewrite the transfer events rotki already made; do not add a second event for the same value.
  Transfers that come later in the transaction than the protocol log go through an `ActionItem`
  built from the current log's data, or a `post_decoding_rules` entry with
  `addresses_to_counterparties` (ether.fi does the first, Ethena the second).
- Event types and subtypes must be a pair in `EVENT_CATEGORY_MAPPINGS`
  (`rotkehlchen/accounting/constants.py`). Prefer a pair that already has a default accounting
  rule (`scripts/rule_lookup.py`).
- Order: `maybe_reshuffle_events` when the protocol's events must read in an order (swap is
  spend, receive, fee, with nothing between). Do not hand-edit `sequence_index`.
- Every search loop: `break` on the match, `for ... else` with `log.error` naming the
  transaction (`str(context.transaction)` includes the chain).
- Notes: name the protocol as a product ("in Rainbow", "from ether.fi card"), use the token
  symbol, no filler.
- If the events touch protocol balances (`DEPOSIT_TO_PROTOCOL`, `WITHDRAW_FROM_PROTOCOL`,
  `GENERATE_DEBT`, `PAYBACK_DEBT`, liquidations), work out what
  `rotkehlchen/tasks/historical_balances.py` will do with each one and test it. Four recent
  reviews (#13649, #13651, #13655, #13690) were about buckets left open or negative.
- If the fix changes how already-decoded transactions should look, existing users need them
  redecoded; that is a data migration step, not a changelog instruction (#13655).

## 6. Write the test

`rotkehlchen/tests/unit/decoders/test_<proto>.py`, one test per user action, each on a real
transaction:

```python
@pytest.mark.vcr(filter_query_parameters=['apikey'])
@pytest.mark.parametrize('<chain>_accounts', [['0x<checksummed user address>']])
def test_<action>(
        <chain>_inquirer: <Chain>Inquirer,
        <chain>_accounts: list[ChecksumEvmAddress],
) -> None:
    tx_hash = deserialize_evm_tx_hash('0x...')  # noqa: E501
    events, _ = get_decoded_events_of_transaction(evm_inquirer=<chain>_inquirer, tx_hash=tx_hash)
    assert events == [EvmEvent(
        tx_ref=tx_hash,
        sequence_index=0,
        timestamp=(timestamp := TimestampMS(...)),
        location=LOCATION_<CHAIN>,
        event_type=HistoryEventType.SPEND,
        event_subtype=HistoryEventSubType.FEE,
        asset=A_ETH,
        amount=FVal('<fee from inspect_tx>'),
        location_label=(user_address := <chain>_accounts[0]),
        counterparty=CPT_GAS,
    ), EvmEvent(
        ...
    )]
```

- The marker is not optional, even before a cassette exists (AGENTS.md "Testing Strategy";
  Lefteris on #13660 and #13662).
- Assert the whole list with `==`, every field, including the gas event when the tracked
  account sent the transaction. Fill the values from the chain (`inspect_tx.py` output, the
  explorer), not by pasting what the decoder printed: a test copied from output proves only that
  the code does what it does.
- Prove the assertion is the right one: the event must be the correct booking, not just "no
  longer wrong" (Lefteris, #13693).
- Tests must fail on the base branch. Say so in the PR, and say how you checked.
- Inline one-use values with the walrus (`amount=FVal(amount := '1.5')`,
  `notes=f'... {amount} ...'`), no setup-variable blocks (AGENTS.md style preferences).
- Do not seed the DB to make a decoder pass; decode the earlier transactions too, in one
  `decode_transaction_hashes` call, if the decoder depends on them (Lefteris, #13651).
- Tests that make no network calls (building `DecoderContext` by hand) need no marker; say so
  when asked (yabirgb, #13698).

## 7. Lint and run what can run

```sh
uv run make format
uv run make lint          # ruff, double-indent, duplicate constants, mypy, pyright, pylint,
                          # checksum and new-logging-f-string checks
uv run pytest rotkehlchen/tests/unit/decoders/test_main.py
```

`make lint` must be green before pushing; pyright's "possibly unbound" and pylint's
`redefined-outer-name` from walrus in `parametrize` were both review blockers (#13649, #13704).
The new-logging check rejects a new f-string in a `log.*` call (`tools/lint_new_logging_fstrings.py`);
use `log.error('... %s', x)` or keyword fields (`log.error('...', transaction=transaction)`), as
`rotkehlchen/chain/evm/decoding/kiln/decoder.py` does.

The decoder tests themselves cannot pass yet: without a cassette, VCR refuses the network.

## 8. Record cassettes (human)

The test-caching repo is cloned by the test suite into rotki's data directory's parent:
`~/Library/Application Support/rotki/test-caching` on macOS, `~/.local/share/rotki/test-caching`
on Linux (`rotkehlchen/tests/conftest.py`, `fixture_vcr_base_dir`). Hand the user:

```sh
cd "<test-caching dir>"
git fetch origin && git checkout -b <same branch name as the rotki PR> origin/<develop|bugfixes>
cd <rotki checkout>
make create-cassette rotkehlchen/tests/unit/decoders/test_<proto>.py
# equivalent: RECORD_CASSETTES=true uv run pytest -m vcr rotkehlchen/tests/unit/decoders/test_<proto>.py
cd "<test-caching dir>" && git status   # new files under cassettes/unit/decoders/test_<proto>/
git add cassettes && git commit -m "Add cassettes for <proto>" && git push <fork> <branch>
```

- Etherscan: the tests ship a test key; `ETHERSCAN_API_KEY` overrides it
  (`rotkehlchen/tests/utils/database.py`). A free key is enough (Lefteris, #13662).
- Responses that were rate limited or 5xx are dropped while recording and listed at the end of
  the run; such a cassette is incomplete, rerun until the summary is clean (`conftest.py`,
  `DISCARDED_RECORDINGS`).
- Outside contributors push to a fork of rotki/test-caching and open a PR to its `develop` (or
  `bugfixes`). After the rotki PR merges, CI merges the cassette branch
  (`.github/workflows/rotki_cassette_merge.yml`; rotkibot comments "rotki/test-caching#N was
  successfully merged").

Then replay offline (`uv run pytest rotkehlchen/tests/unit/decoders/test_<proto>.py`), fix any
mismatch, and only then report the test as passing.

## 9. Changelog

One line, at the top of the list in `docs/changelog.rst`:

```rst
* :feature:`<issue>` <Protocol thing> on <chains> is now decoded in transaction history.
* :bug:`<issue>` <What the user saw before> is now <what they see>.
```

`:feature:` or `:bug:`, the issue number or `-`. Written for users, short, every chain the
decoder is enabled on, nothing it cannot deliver, nothing about internals they will not see.

## 10. Commit

rotki's AGENTS.md: title at most 50 characters, imperative; a body for a non-trivial change
saying what changed and why, ending `Fix #<issue>`; no `Co-Authored-By` for any AI tool. If the
user's own rules say single-line commits, theirs win; cargopete's single-line commits (#13693, #13695) merged fine.
Maintainers ask for a tidy, squashed, rebased branch before merge (#13649, #13693).

## 11. PR body

Title in the maintainers' plain style: "Decode the Spark Savings V2 vaults", "Add Connext NXTP
bridge decoder". Body, short:

```markdown
Closes #<issue>

<One paragraph: what rotki showed and why.>

<One paragraph: which contract and log the decoder matches, what each becomes (type/subtype,
counterparty), what it deliberately leaves alone and why.>

<One paragraph: the tests, which transactions they use, that they fail without the change,
"Cassettes are in rotki/test-caching#<N>.">

## Checklist

- [ ] The PR modified the frontend, and updated the [user guide](https://github.com/rotki/docs/blob/main/usage-guides/index.md) to reflect the changes.
```

Keep the template's checklist as it is (`.github/pull_request_template.md`). A table is fine
when there are several contracts or actions (#13660, #13662). Do not post anything: give the
user the text.

## 12. Hand-off report

List: files changed; commands run with pass/fail counts; what is pending (cassettes, replay,
issue, CLA); review-checklist items that could not be confirmed. AGENTS.md "Pre-handoff
validation" is the bar.
