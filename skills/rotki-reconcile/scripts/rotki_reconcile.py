#!/usr/bin/env python3
"""Check a rotki profit and loss CSV export for data gaps before it is used for a tax return.

Stdlib only. Run it isolated: python3 -I rotki_reconcile.py all_events.csv [options]

It reads the file the user exported themselves (all_events.csv, or the csv.zip rotki
writes). It reports missing prices, missing acquisitions and quiet stretches that may be
uncovered history, and, if given on-chain transaction hashes or a local nuthatch nest,
the transactions that do not appear in the export. It does not say what to declare.

Export format, from rotki/rotki@ed4db5ed57:
  rotkehlchen/accounting/export/csv.py         CSVExporter, FILENAME_ALL_CSV, summary rows
  rotkehlchen/accounting/structures/processed_event.py  to_exported_dict(): the columns
  rotkehlchen/accounting/cost_basis/base.py    CostBasisInfo.to_string(): the
                                               'Incomplete cost basis information' marker
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

INCOMPLETE_MARKER = 'Incomplete cost basis information for spend.'
REQUIRED = ('type', 'timestamp', 'price', 'free_amount', 'taxable_amount')
TX_HASH = re.compile(r'0x[0-9a-fA-F]{64}')
FORMULA_TERM = re.compile(r'([0-9][0-9.eE+-]*)\*H\d+')
TEXT_TERM = re.compile(r'(\S+) / \S+\s+acquired at')
# ISO 4217 codes rotki treats as fiat main currencies; a fiat spend needs no acquisition.
FIAT = set(
    'USD EUR GBP JPY CNY CAD AUD NZD CHF SEK NOK DKK PLN CZK HUF RON BGN HRK TRY RUB UAH INR '
    'KRW SGD HKD TWD THB IDR MYR PHP VND BRL MXN ARS CLP COP PEN ZAR NGN KES EGP ILS AED SAR '
    'QAR KWD BHD ISK'.split(),
)
DATE_FORMATS = (
    '%d/%m/%Y %H:%M:%S', '%m/%d/%Y %H:%M:%S', '%Y-%m-%d %H:%M:%S', '%Y/%m/%d %H:%M:%S',
    '%d.%m.%Y %H:%M:%S', '%d-%m-%Y %H:%M:%S', '%d/%m/%Y', '%Y-%m-%d',
)


@dataclass
class Row:
    line: int
    data: dict
    when: datetime | None = None


@dataclass
class Export:
    rows: list
    summary: dict = field(default_factory=dict)
    delimiter: str = ','
    formulas: bool = False
    unparsed_dates: int = 0


def read_text(path: Path) -> str:
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as zf:
            names = [n for n in zf.namelist() if n.endswith('all_events.csv')] or \
                [n for n in zf.namelist() if n.endswith('.csv')]
            if not names:
                raise SystemExit(f'{path}: no CSV inside the zip')
            return zf.read(names[0]).decode('utf-8-sig')
    return path.read_text(encoding='utf-8-sig')


def detect_delimiter(header: str, given: str | None) -> str:
    if given:
        return given
    for d in (',', ';', '\t', '|'):
        cols = next(csv.reader([header], delimiter=d))
        if 'type' in cols and 'timestamp' in cols:
            return d
    raise SystemExit('Could not find the rotki PnL header (type, timestamp, ...). Is this all_events.csv?')


def parse_date(value: str, fmt: str | None) -> datetime | None:
    value = value.strip()
    if not value:
        return None
    if value.isdigit():
        return datetime.fromtimestamp(int(value), tz=timezone.utc).replace(tzinfo=None)
    formats = ([fmt.replace(' %Z', '').replace('%Z', '').strip()] if fmt else []) + list(DATE_FORMATS)
    candidates = [value]
    parts = value.rsplit(' ', 1)
    if len(parts) == 2 and re.fullmatch(r'[A-Za-z+\-0-9:]+', parts[1]) and not parts[1][0].isdigit():
        candidates.append(parts[0])  # strip the %Z zone name rotki appends by default
    for candidate in candidates:
        for f in formats:
            try:
                return datetime.strptime(candidate, f)
            except ValueError:
                continue
    return None


def load(path: Path, delimiter: str | None = None, date_format: str | None = None) -> Export:
    text = read_text(path)
    header = text.split('\n', 1)[0]
    delim = detect_delimiter(header, delimiter)
    reader = csv.DictReader(io.StringIO(text), delimiter=delim)
    missing = [c for c in REQUIRED if c not in (reader.fieldnames or [])]
    if missing:
        raise SystemExit(f'Not a rotki PnL export: missing columns {missing}')
    export = Export(rows=[], delimiter=delim)
    in_summary = False
    for i, data in enumerate(reader, start=2):
        if not any((v or '').strip() for v in data.values()):
            in_summary = True  # _maybe_add_summary separates the summary with blank rows
            continue
        if in_summary or not (data.get('type') or '').strip():
            label = (data.get('free_amount') or '').strip()
            if label:
                export.summary[label] = (data.get('taxable_amount') or '').strip()
            continue
        if any((v or '').startswith('=') for v in data.values()):
            export.formulas = True
        row = Row(line=i, data=data, when=parse_date(data.get('timestamp') or '', date_format))
        if row.when is None:
            export.unparsed_dates += 1
        export.rows.append(row)
    return export


def dec(value: str | None) -> Decimal | None:
    try:
        return Decimal((value or '').strip())
    except InvalidOperation:
        return None


def amount(row: Row) -> Decimal:
    return (dec(row.data.get('free_amount')) or Decimal(0)) + (dec(row.data.get('taxable_amount')) or Decimal(0))


def is_fiat(row: Row) -> bool:
    for key in ('asset_identifier', 'asset'):
        if (row.data.get(key) or '').strip().upper() in FIAT:
            return True
    return False


def describe(row: Row) -> dict:
    d = row.data
    return {
        'line': row.line,
        'timestamp': d.get('timestamp', ''),
        'location': d.get('location', ''),
        'type': d.get('type', ''),
        'asset': d.get('asset') or d.get('asset_identifier', ''),
        'amount': str(amount(row)),
        'direction': d.get('direction', ''),
        'tx': (TX_HASH.findall(d.get('notes') or '') or [''])[0],
    }


def matched_amount(cell: str) -> Decimal | None:
    """Amount of acquisitions a cost basis cell matched, or None if the cell says nothing
    (ACB exports a bare number)."""
    cell = (cell or '').strip()
    terms = FORMULA_TERM.findall(cell) if cell.startswith('=') else TEXT_TERM.findall(cell)
    if not terms:
        return None
    total = Decimal(0)
    for t in terms:
        total += dec(t) or Decimal(0)
    return total


def check_prices(export: Export) -> list:
    out = []
    for row in export.rows:
        price = dec(row.data.get('price'))
        if (price is None or price == 0) and amount(row) != 0:
            item = describe(row)
            item['price'] = row.data.get('price', '')
            out.append(item)
    return out


def check_acquisitions(export: Export) -> tuple[list, list]:
    """Returns (certain, possible). Certain: rotki wrote its incomplete marker, or the
    matched acquisitions sum to less than the amount spent. Possible: a formula export,
    where rotki replaces the marker with a formula and an unmatched spend shows an
    empty cost basis."""
    certain, possible = [], []
    for row in export.rows:
        d = row.data
        taxable_cb, free_cb = d.get('cost_basis_taxable') or '', d.get('cost_basis_free') or ''
        if INCOMPLETE_MARKER in taxable_cb or INCOMPLETE_MARKER in free_cb:
            certain.append({**describe(row), 'reason': 'rotki marked the cost basis incomplete'})
            continue
        if (d.get('direction') or '') != 'out' or is_fiat(row):
            continue
        matched = [m for m in (matched_amount(taxable_cb), matched_amount(free_cb)) if m is not None]
        spent = amount(row)
        if matched:
            got = sum(matched, Decimal(0))
            if spent > 0 and got < spent * Decimal('0.999999'):
                certain.append({
                    **describe(row),
                    'reason': f'acquisitions cover {got} of {spent}',
                })
            continue
        pnl_formula = any((d.get(k) or '').startswith('=IF(') for k in ('pnl_taxable', 'pnl_free'))
        if export.formulas and pnl_formula and not taxable_cb.strip() and not free_cb.strip():
            possible.append({**describe(row), 'reason': 'spend with PnL but no matched acquisition'})
    return certain, possible


def check_gaps(export: Export, gap_days: float, start: datetime | None, end: datetime | None) -> dict:
    by_location: dict[str, list] = {}
    for row in export.rows:
        if row.when is not None:
            by_location.setdefault(row.data.get('location') or '?', []).append(row.when)
    result = {}
    for location, times in sorted(by_location.items()):
        times.sort()
        gaps = []
        bounds = ([start] if start else []) + times + ([end] if end else [])
        for a, b in zip(bounds, bounds[1:]):
            days = (b - a).total_seconds() / 86400
            if days >= gap_days:
                gaps.append({'from': a.isoformat(sep=' '), 'to': b.isoformat(sep=' '), 'days': round(days)})
        result[location] = {
            'events': len(times),
            'first': times[0].isoformat(sep=' '),
            'last': times[-1].isoformat(sep=' '),
            'gaps': gaps,
        }
    return result


def export_hashes(export: Export, locations: list | None) -> set:
    hashes = set()
    for row in export.rows:
        if locations and (row.data.get('location') or '') not in locations:
            continue
        hashes.update(h.lower() for h in TX_HASH.findall(row.data.get('notes') or ''))
    return hashes


def hashes_from_file(path: Path) -> set:
    return {h.lower() for h in TX_HASH.findall(path.read_text(encoding='utf-8', errors='replace'))}


def nest_get(base: str, params: dict, timeout: float) -> dict:
    url = base.rstrip('/') + '/api?' + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - the user's own nest
        return json.loads(resp.read().decode('utf-8'))


def nest_block(base: str, when: datetime, closest: str, timeout: float) -> int:
    ts = int(when.replace(tzinfo=timezone.utc).timestamp())
    body = nest_get(base, {'module': 'block', 'action': 'getblocknobytime', 'timestamp': ts,
                           'closest': closest}, timeout)
    if str(body.get('status')) != '1':
        raise RuntimeError(f"getblocknobytime refused: {body.get('message')} {body.get('result')}")
    return int(body['result'])


def nest_hashes(base: str, addresses: list, startblock: int, endblock: int, actions: list,
                timeout: float = 30.0, page_size: int = 10000) -> tuple[set, list]:
    """Etherscan-shaped account queries against a local nest (the shape rotki's nuthatch
    indexer reads). A refusal means the nest has not covered the range: reported, not hidden."""
    found, problems = set(), []
    for address in addresses:
        for action in actions:
            page = 1
            while True:
                params = {'module': 'account', 'action': action, 'address': address,
                          'startblock': startblock, 'endblock': endblock, 'page': page,
                          'offset': page_size, 'sort': 'asc'}
                try:
                    body = nest_get(base, params, timeout)
                except (urllib.error.URLError, OSError, ValueError) as e:
                    problems.append(f'{action} {address}: {e}')
                    break
                result = body.get('result')
                if str(body.get('status')) != '1':
                    if body.get('message') in ('No transactions found', 'No records found') or result == []:
                        break
                    problems.append(f"{action} {address}: {body.get('message')} {result}")
                    break
                before = len(found)
                for tx in result or []:
                    h = (tx.get('hash') or '').lower()
                    if not h:
                        continue
                    if tx.get('isError') == '1' and (tx.get('from') or '').lower() != address.lower():
                        continue  # a failed incoming tx moved nothing; a failed outgoing one still paid gas
                    found.add(h)
                if len(result or []) < page_size or len(found) == before:
                    break
                page += 1
    return found, problems


def render(report: dict, max_rows: int) -> str:
    out = ['rotki-reconcile: data gaps in a rotki PnL export. Not tax advice.']
    meta = report['export']
    out.append(f"{meta['file']}: {meta['events']} events, delimiter {meta['delimiter']!r}, "
               f"formulas {'on' if meta['formulas'] else 'off'}")
    for k, v in meta['summary'].items():
        out.append(f'  {k}: {v}')
    if meta['unparsed_dates']:
        out.append(f"  {meta['unparsed_dates']} timestamps could not be parsed; pass --date-format "
                   'with your date_display_format setting.')

    def rows(title, items, note):
        out.append('')
        out.append(f'{title}: {len(items)}')
        if items and note:
            out.append(f'  {note}')
        for it in items[:max_rows]:
            extra = f"  ({it['reason']})" if 'reason' in it else ''
            tx = f" tx {it['tx']}" if it.get('tx') else ''
            out.append(f"  line {it['line']}: {it['timestamp']} {it['location']} {it['type']} "
                       f"{it['amount']} {it['asset']} {it.get('direction', '')}{tx}{extra}")
        if len(items) > max_rows:
            out.append(f'  ... {len(items) - max_rows} more (use --json for all)')

    rows('Events with a zero or missing price', report['zero_price'],
         'rotki values these at zero. An acquisition at zero gives a zero cost basis to whatever '
         'is later sold from it. Worthless spam tokens are expected here; real assets are not.')
    rows('Spends without full cost basis (missing acquisitions)', report['missing_acquisitions'],
         'rotki found no (or not enough) earlier acquisition of the asset, so the gain is '
         'computed against a missing cost. Usually an untracked account, exchange or older history.')
    if report['possible_missing_acquisitions'] or meta['formulas']:
        rows('Possible missing acquisitions (formula export)', report['possible_missing_acquisitions'],
             'A formula export can drop rotki\'s "incomplete" marker. Turn off "Export formulas in '
             'the PnL CSV" (account settings, CSV export) and export again to confirm these.')
    out.append('')
    out.append(f"Quiet stretches of {report['gap_days']}+ days, per location:")
    for location, g in report['gaps'].items():
        out.append(f"  {location}: {g['events']} events, {g['first']} .. {g['last']}")
        for gap in g['gaps'][:max_rows]:
            out.append(f"    no events {gap['from']} -> {gap['to']} ({gap['days']} days)")
    out.append('  A quiet stretch is not proof of missing history, only where to look: compare it '
               'with what the account actually did then.')
    cc = report.get('crosscheck')
    if cc:
        out.append('')
        out.append(f"On-chain cross-check ({cc['source']}): {cc['onchain']} on-chain transactions, "
                   f"{cc['in_export']} found in the export, {len(cc['missing'])} missing.")
        for h in cc['missing'][:max_rows]:
            out.append(f'  missing: {h}')
        if len(cc['missing']) > max_rows:
            out.append(f"  ... {len(cc['missing']) - max_rows} more (use --json for all)")
        if cc['missing']:
            out.append('  Not every transaction yields a PnL event (approvals, ignored or non-taxable '
                       'actions), so check each one; a missing transfer or trade is the real gap.')
        for p in cc['problems']:
            out.append(f'  nest: {p}')
        if cc['problems']:
            out.append('  The nest did not answer for everything above, so the cross-check is incomplete.')
    out.append('')
    out.append('Not visible in a CSV: spends rotki skipped for a missing price are left out of the '
               'export entirely. The report page in rotki lists them under missing prices.')
    return '\n'.join(out)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description='Check a rotki PnL CSV export for data gaps.')
    p.add_argument('csv', type=Path, help='all_events.csv or the csv.zip rotki exported')
    p.add_argument('--delimiter', help="csv_export_delimiter, if not ',' ';' tab or '|'")
    p.add_argument('--date-format', help="rotki's date_display_format, default '%%d/%%m/%%Y %%H:%%M:%%S %%Z'")
    p.add_argument('--from', dest='start', help='report period start, YYYY-MM-DD')
    p.add_argument('--to', dest='end', help='report period end, YYYY-MM-DD')
    p.add_argument('--gap-days', type=float, default=90, help='flag quiet stretches this long (default 90)')
    p.add_argument('--onchain-hashes', type=Path,
                   help='file containing the on-chain tx hashes to expect (any text; hashes are extracted)')
    p.add_argument('--nest', help='local nuthatch nest base URL, e.g. http://127.0.0.1:8288')
    p.add_argument('--address', action='append', default=[], help='account to cross-check; repeatable')
    p.add_argument('--startblock', type=int)
    p.add_argument('--endblock', type=int)
    p.add_argument('--actions', default='txlist', help='nest actions, comma separated (txlist,tokentx,txlistinternal)')
    p.add_argument('--location', action='append', default=[],
                   help='export locations to cross-check against (default: ethereum)')
    p.add_argument('--max-rows', type=int, default=20)
    p.add_argument('--json', action='store_true')
    args = p.parse_args(argv)

    export = load(args.csv, args.delimiter, args.date_format)
    start = parse_date(args.start, None) if args.start else None
    end = parse_date(args.end, None) if args.end else None
    certain, possible = check_acquisitions(export)
    report = {
        'export': {
            'file': str(args.csv), 'events': len(export.rows), 'delimiter': export.delimiter,
            'formulas': export.formulas, 'summary': export.summary,
            'unparsed_dates': export.unparsed_dates,
        },
        'zero_price': check_prices(export),
        'missing_acquisitions': certain,
        'possible_missing_acquisitions': possible,
        'gap_days': args.gap_days,
        'gaps': check_gaps(export, args.gap_days, start, end),
    }

    onchain, problems, source = None, [], None
    if args.onchain_hashes:
        onchain, source = hashes_from_file(args.onchain_hashes), str(args.onchain_hashes)
    elif args.nest:
        if not args.address:
            raise SystemExit('--nest needs at least one --address')
        startblock, endblock = args.startblock, args.endblock
        try:
            if startblock is None:
                startblock = nest_block(args.nest, start, 'after', 30) if start else 0
            if endblock is None:
                endblock = nest_block(args.nest, end.replace(hour=23, minute=59, second=59), 'before', 30) \
                    if end else 99_999_999
        except (RuntimeError, urllib.error.URLError, OSError, ValueError) as e:
            raise SystemExit(f'Could not resolve blocks from the nest: {e}. Pass --startblock/--endblock.')
        onchain, problems = nest_hashes(args.nest, args.address, startblock, endblock,
                                        [a.strip() for a in args.actions.split(',') if a.strip()])
        source = f'nest {args.nest}, blocks {startblock}-{endblock}'
    if onchain is not None:
        in_export = export_hashes(export, args.location or ['ethereum'])
        report['crosscheck'] = {
            'source': source,
            'onchain': len(onchain),
            'in_export': len(onchain & in_export),
            'missing': sorted(onchain - in_export),
            'problems': problems,
        }

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(render(report, args.max_rows))
    return 0


if __name__ == '__main__':
    sys.exit(main())
