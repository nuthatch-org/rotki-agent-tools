#!/usr/bin/env python3
"""Say which rotki default accounting rule covers an event type/subtype/counterparty.

Reads every updates/accounting_rules/vN.json in rotki/data (develop by default) and the limits in
updates/info.json. rotki resolves a counterparty-specific rule first and falls back to the
counterparty-less one (rotkehlchen/accounting/rules.py, get_event_settings), so both are shown.

    python3 rule_lookup.py "receive" "cashback" etherfi
    python3 rule_lookup.py "deposit" "deposit for wrapped"
    python3 rule_lookup.py --list-latest

Type and subtype are the serialized names: lowercase words, as HistoryEventSubType.serialize()
gives them (DEPOSIT_FOR_WRAPPED -> "deposit for wrapped").
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

RAW = 'https://raw.githubusercontent.com/rotki/data/{branch}/updates/{path}'


def fetch(branch: str, path: str) -> dict | None:
    request = urllib.request.Request(RAW.format(branch=branch, path=path), headers={'user-agent': 'rotki-decoder-skill'})  # noqa: E501
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)  # noqa: E501
    parser.add_argument('event_type', nargs='?')
    parser.add_argument('event_subtype', nargs='?')
    parser.add_argument('counterparty', nargs='?')
    parser.add_argument('--branch', default='develop')
    parser.add_argument('--list-latest', action='store_true', help='print the version limits and the newest file')  # noqa: E501
    args = parser.parse_args()

    info = fetch(args.branch, 'info.json') or {}
    limits = info.get('accounting_rules', {})
    rules: list[tuple[int, dict]] = []
    version = 1
    while (data := fetch(args.branch, f'accounting_rules/v{version}.json')) is not None:
        rules.extend((version, rule) for rule in data.get('accounting_rules', []))
        version += 1
    newest = version - 1

    print(f'info.json latest={limits.get("latest")}, files present up to v{newest}, limits={json.dumps(limits.get("limits", {}))}')  # noqa: E501
    if args.list_latest:
        for file_version, rule in rules:
            if file_version == newest:
                print(f'v{file_version}: {json.dumps(rule)}')
        return
    if args.event_type is None or args.event_subtype is None:
        sys.exit('give an event type and subtype, or --list-latest')

    matches = [
        (file_version, rule) for file_version, rule in rules
        if rule['event_type'] == args.event_type and rule['event_subtype'] == args.event_subtype
    ]
    specific = [m for m in matches if args.counterparty is not None and m[1]['counterparty'] == args.counterparty]  # noqa: E501
    generic = [m for m in matches if m[1]['counterparty'] is None]
    others = [m for m in matches if m not in specific and m not in generic]
    for label, group in (('counterparty-specific', specific), ('generic (counterparty null)', generic), ('other counterparties', others)):  # noqa: E501
        print(f'\n{label}:')
        for file_version, rule in group:
            print(f'  v{file_version}: {json.dumps(rule)}')
        if not group:
            print('  none')

    if specific or generic:
        print('\ncovered: rotki will apply the first group that is not empty, in the order above')
    else:
        print('\nnot covered: this event would show as missing an accounting rule')


if __name__ == '__main__':
    main()
