#!/usr/bin/env python3
"""Print an EVM transaction's call and logs in the shape a rotki decoder needs.

For each log: index, emitting contract, topic0 with its signature (openchain lookup) and the
byte literal rotki's constants use, the indexed topics, and the data split into 32-byte words.
With --rotki, also says where in a rotki checkout each contract address and topic already appear,
which answers "is there a module for this already".

Run it with rotki's own environment so addresses come out checksummed:
    cd <rotki checkout> && uv run python <skill>/scripts/inspect_tx.py --chain scroll 0x<hash> --rotki .

The tx hash is sent to the RPC (a public one unless --rpc is given) and topic hashes to
api.openchain.xyz unless --no-lookup is given. Point --rpc at your own node if that matters.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from decimal import Decimal
from pathlib import Path

PUBLIC_RPCS = {
    'ethereum': (1, 'https://ethereum-rpc.publicnode.com'),
    'optimism': (10, 'https://mainnet.optimism.io'),
    'binance_sc': (56, 'https://bsc-dataseed.binance.org'),
    'gnosis': (100, 'https://rpc.gnosischain.com'),
    'polygon_pos': (137, 'https://polygon-bor-rpc.publicnode.com'),
    'base': (8453, 'https://mainnet.base.org'),
    'arbitrum_one': (42161, 'https://arb1.arbitrum.io/rpc'),
    'scroll': (534352, 'https://rpc.scroll.io'),
}

try:
    from eth_utils import to_checksum_address
except ImportError:  # outside rotki's venv
    def to_checksum_address(value: str) -> str:
        return value.lower()
    CHECKSUMMED = False
else:
    CHECKSUMMED = True


def fmt_native(wei: int) -> str:
    text = format(Decimal(wei).scaleb(-18), 'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


def post_json(url: str, payload: object) -> object:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={'content-type': 'application/json', 'user-agent': 'rotki-decoder-skill'},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())


def get_json(url: str) -> object:
    request = urllib.request.Request(url, headers={'user-agent': 'rotki-decoder-skill'})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())


def rpc(url: str, method: str, params: list) -> dict:
    result = post_json(url, {'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params})
    if not isinstance(result, dict) or result.get('result') is None:
        sys.exit(f'{method} failed: {result}')
    return result['result']


def lookup_signatures(events: set[str], functions: set[str]) -> dict[str, str]:
    if len(events) == 0 and len(functions) == 0:
        return {}
    query = []
    if events:
        query.append('event=' + ','.join(sorted(events)))
    if functions:
        query.append('function=' + ','.join(sorted(functions)))
    try:
        result = get_json('https://api.openchain.xyz/signature-database/v1/lookup?' + '&'.join(query) + '&filter=true')  # noqa: E501
    except OSError as e:
        print(f'signature lookup failed: {e}', file=sys.stderr)
        return {}
    names = {}
    for kind in ('event', 'function'):
        for selector, matches in (result.get('result', {}).get(kind) or {}).items():
            if matches:
                names[selector] = ' | '.join(match['name'] for match in matches)
    return names


def address_from_topic(topic: str) -> str | None:
    """Guess that a word is an address: top 12 bytes zero and more than 16 bytes significant.

    Small integers and booleans also have 12 zero bytes, hence the second condition. A vanity
    address with many leading zeros prints as an int; check the ABI when it matters.
    """
    raw = topic[2:]
    if raw[:24] == '0' * 24 and len(raw[24:].lstrip('0')) > 32:
        return to_checksum_address('0x' + raw[24:])
    return None


def build_index(rotki_root: Path) -> list[tuple[str, str]]:
    files = []
    for directory in ('rotkehlchen/chain', 'rotkehlchen/constants'):
        for path in sorted((rotki_root / directory).rglob('*.py')):
            files.append((str(path.relative_to(rotki_root)), path.read_text(encoding='utf-8')))
    return files


def where(needles: list[str], index: list[tuple[str, str]]) -> list[str]:
    hits = []
    for name, text in index:
        lowered = text.lower()
        if any(needle.lower() in lowered for needle in needles):
            hits.append(name)
    return hits


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)  # noqa: E501
    parser.add_argument('tx_hash')
    parser.add_argument('--chain', default='ethereum', choices=sorted(PUBLIC_RPCS))
    parser.add_argument('--rpc', help='RPC URL to use instead of the public default')
    parser.add_argument('--rotki', type=Path, help='path to a rotki checkout to search')
    parser.add_argument('--no-lookup', action='store_true', help='skip the openchain lookup')
    args = parser.parse_args()

    chain_id, default_rpc = PUBLIC_RPCS[args.chain]
    url = args.rpc or default_rpc
    tx = rpc(url, 'eth_getTransactionByHash', [args.tx_hash])
    receipt = rpc(url, 'eth_getTransactionReceipt', [args.tx_hash])
    block = rpc(url, 'eth_getBlockByNumber', [receipt['blockNumber'], False])

    selector = tx['input'][:10] if len(tx['input']) >= 10 else None
    topics0 = {log['topics'][0] for log in receipt['logs'] if log['topics']}
    names = {} if args.no_lookup else lookup_signatures(topics0, {selector} if selector else set())
    index = build_index(args.rotki) if args.rotki else None

    if not CHECKSUMMED:
        print('note: eth_utils not importable, addresses are lowercase. Run under rotki\'s uv env.\n')  # noqa: E501
    print(f'chain {args.chain} (chain id {chain_id}), block {int(receipt["blockNumber"], 16)}, timestamp {int(block["timestamp"], 16)} (TimestampMS {int(block["timestamp"], 16) * 1000})')  # noqa: E501
    print(f'status {receipt["status"]}, from {to_checksum_address(tx["from"])}, to {to_checksum_address(tx["to"]) if tx["to"] else None}, value {int(tx["value"], 16)} wei')  # noqa: E501
    if selector:
        print(f'selector {selector} {names.get(selector, "?")}')
    fee_wei = int(receipt['gasUsed'], 16) * int(receipt.get('effectiveGasPrice') or tx['gasPrice'], 16)  # noqa: E501
    if (l1_fee := receipt.get('l1Fee')) is not None:
        fee_wei += int(l1_fee, 16)
    print(f'fee {fee_wei} wei = {fmt_native(fee_wei)} native (gas event only if the sender is tracked{"; includes l1Fee" if l1_fee else ""})')  # noqa: E501

    contracts = {}
    for log in receipt['logs']:
        address = to_checksum_address(log['address'])
        contracts.setdefault(address, []).append(int(log['logIndex'], 16))
        print(f'\nlog {int(log["logIndex"], 16)} from {address}')
        if not log['topics']:
            print('  anonymous log')
        for position, topic in enumerate(log['topics']):
            if position == 0:
                literal = repr(bytes.fromhex(topic[2:]))
                print(f'  topic0 {topic} {names.get(topic, "?")}')
                print(f'         {literal}')
            else:
                as_address = address_from_topic(topic)
                suffix = f' (address {as_address})' if as_address else f' (int {int(topic, 16)})'
                print(f'  topic{position} {topic}{suffix}')
        data = log['data'][2:]
        for word in range(len(data) // 64):
            chunk = data[word * 64:(word + 1) * 64]
            as_address = address_from_topic('0x' + chunk)
            suffix = f' address {as_address}' if as_address else f' int {int(chunk, 16)}'
            print(f'  data[{word * 32}:{(word + 1) * 32}] {chunk}{suffix}')

    if index is None:
        return
    print('\nwhere rotki already knows these contracts and topics:')
    for address, log_indices in contracts.items():
        hits = where([address], index)
        print(f'  {address} (logs {log_indices}): {", ".join(hits) if hits else "nowhere"}')
    for topic in sorted(topics0):
        hits = where([repr(bytes.fromhex(topic[2:])), topic], index)
        print(f'  {topic[:18]}... {names.get(topic, "?")}: {", ".join(hits) if hits else "nowhere"}')  # noqa: E501
    print('  (a topic written as a non-repr byte literal will not be found; grep the name too)')


if __name__ == '__main__':
    main()
