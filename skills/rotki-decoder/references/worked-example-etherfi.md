# Worked example: ether.fi card cashback (rotki/rotki#13695)

Merged 2026-11-19 into `develop`, author cargopete, reviewer LefterisJP. Issue rotki/rotki#10940,
cassettes rotki/test-caching#459. Small enough to read whole, and it drew one review objection of
the most common kind. The code below is quoted from the merged branch
(`pete/etherfi-card-cashback`, head `66fe77e`).

## 1. The problem

Issue #10940 (yabirgb): an account using the ether.fi card gets cashback, rotki shows it as a
plain receive. Reference tx on Scroll:
`0xe9d19395a77c8c32295328a4ea15d1af2fa39d1dc9970e18de1dbae997bc9ede`.

This is the "undecoded" path: rotki's generic transfer decoding already produced
`RECEIVE / NONE` of SCR; nothing marked it as cashback.

## 2. Reading the transaction

`inspect_tx.py --chain scroll 0xe9d1...9ede --rotki <develop checkout>` gives, trimmed:

```
log 10 from 0xd29687c813D741E2F938F4aC377128810E217b1b            (SCR)
  topic0 0xddf252ad... Transfer(address,address,uint256)
  topic1 ... (address 0xef55eC694B0B8273967f28627C5BC26F5deea836)    cashback payer
  topic2 ... (address 0x0520a3bcF7a29EE3FB2DaCC1d50C2D6A6F58Bf1F)    the user's Safe
  data[0:32] ... int 2170484754468739741

log 11 from 0x380B2e96799405be6e3D965f4044099891881acB            ether.fi event emitter
  topic0 0x89d3571a... Cashback(address,uint256,address,address,uint256,uint256,uint256,bool)
         b'\x89\xd3W\x1aI\x8b]=hY\x9f_\x00\xc3\x01o\x96\x04\xaa\xfaw\x01\xc5,\x1b\x04\x10\x9c\xd9\t\xa7\x98'
  topic1 ... (address 0x0520a3bc...)                                 spender
  topic2 ... (address 0x0520a3bc...)                                 recipient
  topic3 ... (int 1)                                                 paid
  data[32:64] ... address 0xd29687c8...                              token
  data[64:96] ... int 2170484754468739741                            amount

where rotki already knows these contracts and topics:
  0x380B2e96799405be6e3D965f4044099891881acB (logs [9, 11, 13]): nowhere
  0x89d3571a498b5d3d... Cashback(...): nowhere
```

Log 13 is a second `Cashback` for a different, untracked recipient, and log 12 its transfer.
The decoder must not touch those.

Conclusions: there is a protocol log (`Cashback`) from a fixed contract that names the
recipient, token and amount of a transfer rotki already decoded. So: `addresses_to_decoders` on
the emitter, match the existing receive, rewrite it. No new event.

Reading the contract, not just this transaction, shows two more facts: `paid` can be false (the
cashback is recorded as pending and paid later by `clearPendingCashback()`, which emits
`PendingCashbackCleared(address,address,uint256,uint256)`), and the emitter has the same address
on Optimism. The first version of the PR missed the pending path; that was the review (section 7).

## 3. Where it goes

Same address on several chains, so a chain-agnostic class in
`rotkehlchen/chain/evm/decoding/etherfi/`, registered in `_add_builtin_decoders` gated on a chain
set, like Frankencoin savings. Not a per-chain module.

## 4. Constants

`rotkehlchen/chain/evm/decoding/etherfi/constants.py`:

```python
CPT_ETHERFI: Final = 'etherfi'
ETHERFI_COUNTERPARTY_DETAILS: Final = CounterpartyDetails(
    identifier=CPT_ETHERFI,
    label='ether.fi',
    image='etherfi.png',
)

# The ether.fi Cash event emitter has the same address on every chain the card has lived on
ETHERFI_CASH_EVENT_EMITTER: Final = string_to_evm_address('0x380B2e96799405be6e3D965f4044099891881acB')  # noqa: E501
ETHERFI_CASH_CHAINS: Final = {ChainID.SCROLL, ChainID.OPTIMISM}

CASHBACK: Final = b'\x89\xd3W\x1aI\x8b]=hY\x9f_\x00\xc3\x01o\x96\x04\xaa\xfaw\x01\xc5,\x1b\x04\x10\x9c\xd9\t\xa7\x98'  # noqa: E501
PENDING_CASHBACK_CLEARED: Final = b'\xb20N=u\xf4fz\xe0\xec\xb88\xe9\xdd\xa6\x97\xe1+\xe22\xdf5JB\n\xbcOvc\x90&\xca'  # noqa: E501
```

Checksummed literal inside `string_to_evm_address`, `Final` everywhere, topics as byte literals,
one comment for the one non-obvious fact. The logo is `frontend/app/public/assets/images/protocols/etherfi.png`.

## 5. Decoder

`rotkehlchen/chain/evm/decoding/etherfi/decoder.py`, whole:

```python
class EtherfiCashCommonDecoder(EvmDecoderInterface):

    def _decode_cash_events(self, context: DecoderContext) -> EvmDecodingOutput:
        if context.tx_log.topics[0] == CASHBACK:
            if int.from_bytes(context.tx_log.topics[3]) == 0:  # not paid out yet
                return DEFAULT_EVM_DECODING_OUTPUT

            recipient = bytes_to_address(context.tx_log.topics[2])
            token_address = bytes_to_address(context.tx_log.data[32:64])
            raw_amount = int.from_bytes(context.tx_log.data[64:96])
        elif context.tx_log.topics[0] == PENDING_CASHBACK_CLEARED:  # deferred cashback paid out
            recipient = bytes_to_address(context.tx_log.topics[1])
            token_address = bytes_to_address(context.tx_log.data[:32])
            raw_amount = int.from_bytes(context.tx_log.data[32:64])
        else:
            return DEFAULT_EVM_DECODING_OUTPUT

        if not self.base.is_tracked(recipient):
            return DEFAULT_EVM_DECODING_OUTPUT

        token = self.base.get_or_create_evm_token(address=token_address)
        amount = asset_normalized_value(amount=raw_amount, asset=token)
        for event in context.decoded_events:
            if (
                event.event_type == HistoryEventType.RECEIVE and
                event.event_subtype == HistoryEventSubType.NONE and
                event.location_label == recipient and
                event.asset == token and
                event.amount == amount
            ):
                event.event_subtype = HistoryEventSubType.CASHBACK
                event.counterparty = CPT_ETHERFI
                event.notes = f'Receive cashback of {amount} {token.symbol} from ether.fi card'
                break

        return DEFAULT_EVM_DECODING_OUTPUT

    def addresses_to_decoders(self) -> dict[ChecksumEvmAddress, tuple[Any, ...]]:
        return {ETHERFI_CASH_EVENT_EMITTER: (self._decode_cash_events,)}

    @staticmethod
    def counterparties() -> tuple[CounterpartyDetails, ...]:
        return (ETHERFI_COUNTERPARTY_DETAILS,)
```

What to notice, against the checklist:

- Scoped to one address (D1), decoded from the log only (D2).
- The recipient comes from the log, not from the transaction: the user is a Safe executing via
  a module (log 8, `ExecTransactionFromModule`), and `tx.from` is some other address,
  `0xdC45DB93c3fC37272f40812bBa9C4Bad91344b46` (D3).
- It rewrites the existing receive and adds nothing (D8); the untracked recipient's cashback in
  logs 12 and 13 is skipped by `is_tracked`.
- `paid == false` is skipped because no transfer goes with it, and the later payout is matched
  on its own event (D4).
- `RECEIVE / CASHBACK` is an existing pair (the Gnosis Pay cashback, rotki/rotki#8539) with a
  generic accounting rule since rotki/data v4: `rule_lookup.py receive cashback etherfi` reports
  "covered". No rotki/data PR needed.
- The loop breaks on the match but has no `for ... else` error log. Review did not ask for one
  here; it has been asked for elsewhere (D12), so adding it costs nothing.

Registration, in `rotkehlchen/chain/evm/decoding/decoder.py` `_add_builtin_decoders`:

```python
        if self.evm_inquirer.chain_id in ETHERFI_CASH_CHAINS:
            self._add_single_decoder(class_name='EtherfiCash', decoder_class=EtherfiCashCommonDecoder, rules=rules)  # noqa: E501
```

It is not an Ethereum module, so `test_main.py` did not change.

## 6. Tests

`rotkehlchen/tests/unit/decoders/test_etherfi.py`, three tests: the issue's Scroll tx, an
Optimism tx from a later issue (#10988), and a Scroll tx that pays a pending WETH cashback next
to a normal one. The first:

```python
CASHBACK_PAYER = string_to_evm_address('0xef55eC694B0B8273967f28627C5BC26F5deea836')


@pytest.mark.vcr(filter_query_parameters=['apikey'])
@pytest.mark.parametrize('scroll_accounts', [['0x0520a3bcF7a29EE3FB2DaCC1d50C2D6A6F58Bf1F']])
def test_card_cashback_scroll(
        scroll_inquirer: ScrollInquirer,
        scroll_accounts: list[ChecksumEvmAddress],
) -> None:
    tx_hash = deserialize_evm_tx_hash('0xe9d19395a77c8c32295328a4ea15d1af2fa39d1dc9970e18de1dbae997bc9ede')  # noqa: E501
    events, _ = get_decoded_events_of_transaction(evm_inquirer=scroll_inquirer, tx_hash=tx_hash)
    assert events == [EvmEvent(
        tx_ref=tx_hash,
        sequence_index=10,
        timestamp=TimestampMS(1762884881000),
        location=LOCATION_SCROLL,
        event_type=HistoryEventType.RECEIVE,
        event_subtype=HistoryEventSubType.CASHBACK,
        asset=Asset('eip155:534352/erc20:0xd29687c813D741E2F938F4aC377128810E217b1b'),
        amount=FVal(amount := '2.170484754468739741'),
        location_label=scroll_accounts[0],
        notes=f'Receive cashback of {amount} SCR from ether.fi card',
        counterparty=CPT_ETHERFI,
        address=CASHBACK_PAYER,
    )]
```

Every value is checkable against section 2: `sequence_index` 10 is the transfer's log index,
the timestamp is the block's times 1000, the amount is `2170484754468739741` at 18 decimals, the
address is the payer. There is no gas event because the tracked Safe did not send the
transaction. The whole list is asserted, so a stray event from log 12 would fail it (T1).

## 7. The review, and the fix

Lefteris, [inline](https://github.com/rotki/rotki/pull/13695#discussion_r4602446924):

> This misses deferred cashback payouts. When a `Cashback` event has `paid == false`, ether.fi
> records it as pending; once funds become available, `clearPendingCashback()` transfers the
> tokens and the same emitter emits `PendingCashbackCleared(address,address,uint256,uint256)`.
> [...] Please add a handler for `PendingCashbackCleared`, matching the indexed recipient with
> the token and amount from the log data, plus a regression test for this payout path

Reply: "added PendingCashbackCleared, matched on the indexed recipient and the token and amount
from the data. test uses a scroll tx that clears a pending WETH cashback next to a normal one".
That is the `elif` branch above and `test_pending_cashback_cleared_scroll`. Then "LGTM".

The lesson is D4: read the contract's other events before opening the PR. A `bool paid` in the
event signature was the clue.

## 8. Changelog

```rst
* :feature:`10940` ether.fi card cashback on Scroll and Optimism, including cashback paid out after a delay, is now decoded as cashback in transaction history.
```

One line, at the top, issue number, both chains, what the user sees.

## 9. PR body (as merged)

```markdown
Closes #10940

ether.fi card cashback arrived as a plain receive from the cashback payer, with nothing marking it as cashback.

The ether.fi Cash event emitter logs a `Cashback` event next to each payout, with the recipient, token and amount. A new chain-agnostic decoder, registered like the Frankencoin savings one for Scroll and Optimism (the emitter has the same address on both), matches that event to the tracked recipient's transfer and makes it a receive/cashback with an `etherfi` counterparty. Cashback logged as not yet paid is skipped, since no transfer goes with it, and its later payout through `clearPendingCashback` is matched on `PendingCashbackCleared` instead. The counterparty image is the ether.fi logo from its CoinGecko listing.

`test_card_cashback_scroll` uses the transaction from the issue and `test_card_cashback_optimism` the one from #10988. `test_pending_cashback_cleared_scroll` covers a deferred payout. All fail without the decoder. Cassettes are in rotki/test-caching#459. The decoder suite passes.

## Checklist

- [ ] The PR modified the frontend, and updated the [user guide](https://github.com/rotki/docs/blob/main/usage-guides/index.md) to reflect the changes.
```

Three paragraphs: what was wrong, what the decoder matches and deliberately skips, what the
tests use and where the cassettes are.

## 10. Cassettes

rotki/test-caching#459, "Add cassettes for ether.fi card cashback", from
`cargopete:pete/etherfi-card-cashback` (the same branch name as the rotki PR) into `develop`,
adding `cassettes/unit/decoders/test_etherfi/<test name>[<param id>].yaml` for each test.
rotkibot merged it when the rotki PR merged.
