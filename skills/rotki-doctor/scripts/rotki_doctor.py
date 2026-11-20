#!/usr/bin/env python3
"""Scan rotki's own log files for known failure signatures and print a summary.

Stdlib only. Run it isolated: python3 -I rotki_doctor.py [options]

It reads log files (or stdin) and, if --api is given, a handful of small read-only
endpoints (info, a few settings, the names of configured services and nodes). It never
asks rotki for balances, transactions or history, and it redacts addresses, hashes and
keys from the examples it prints unless --no-redact is passed.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import platform
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, Iterator

ROTKI_REF = 'rotki/rotki@ed4db5ed57 (develop, 2026-11-20)'

# File handler format, rotkehlchen/logging.py configure_logging():
#   '[%(asctime)s] %(levelname)s %(name)s %(message)s' with datefmt '%d/%m/%Y %H:%M:%S %Z'
FILE_LINE = re.compile(
    r'^\[(?P<ts>\d{2}/\d{2}/\d{4} \d{2}:\d{2}:\d{2})(?: [^\]]*)?\] '
    r'(?P<level>TRACE|DEBUG|INFO|WARNING|ERROR|CRITICAL) (?P<logger>\S+) (?P<msg>.*)$',
)
# Stdout handler (LOGTARGET=stdout, Docker): '%(asctime)s [core] %(levelname)s %(name)s %(message)s'
STDOUT_LINE = re.compile(
    r'^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})\S* \[core\] '
    r'(?P<level>TRACE|DEBUG|INFO|WARNING|ERROR|CRITICAL) (?P<logger>\S+) (?P<msg>.*)$',
)
LEVELS = ('TRACE', 'DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL')

REDACTIONS = (
    (re.compile(r'0x[0-9a-fA-F]{64}'), '0x<hash>'),
    (re.compile(r'0x[0-9a-fA-F]{40}'), '0x<address>'),
    (re.compile(r'(?i)(apikey|api_key|key|token)=[^&\s\'",]+'), r'\1=<redacted>'),
    (re.compile(r'(https?://[^/\s\'"]+)/[^\s\'",)]*'), r'\1/<path>'),
)


@dataclass
class Signature:
    id: str
    category: str
    title: str
    pattern: re.Pattern
    explain: str
    fix: str
    source: str
    keys: tuple = ()


def sig(id, category, title, pattern, explain, fix, source, keys=()):
    return Signature(id, category, title, re.compile(pattern), explain, fix, source, keys)


NEST_UPSTREAM_PR = 'https://github.com/rotki/rotki/pull/13686'
NEST_FORK = 'https://github.com/nuthatch-org/rotki-nuthatch'
COVERAGE_IDS = ('range_not_covered', 'no_indexers', 'transactions_not_added')


def nest_note(hits: dict) -> str | None:
    """One mention of a nuthatch nest for coverage problems, whatever the number of hits.
    Upstream #13686 is Ethereum-only; the fork offers the nest on every EVM chain."""
    chains = set()
    for sid in COVERAGE_IDS:
        if sid in hits:
            chains.update(c.lower() for c in getattr(hits[sid], 'keys', {}).get('chain', {}))
    if not chains:
        return None
    note = (
        'One option among the above: a self-hosted nuthatch nest can serve your own accounts\' '
        f'history to rotki as an indexer ({", ".join(sorted(chains))} affected here). It is not in '
        f'a rotki release. The unofficial fork {NEST_FORK} offers it on every EVM chain '
        '(enable_nuthatch_indexer)'
    )
    if 'ethereum' in chains:
        note += f'; for Ethereum only, the open upstream PR {NEST_UPSTREAM_PR} adds it too'
    return note + '.'


# Order matters: the first matching signature claims the line.
SIGNATURES = [
    # --- database and startup -------------------------------------------------------------
    sig('db_upgrade_failed', 'database', 'A database upgrade failed',
        r'Failed at (?P<db>database|global DB) upgrade from version (?P<from_v>\d+) to (?P<to_v>\d+)',
        'rotki tried to upgrade the user or global database schema and the step raised. It '
        'restores the pre-upgrade backup, so the data is not lost, but the account will not open.',
        'Keep the log. Check the backup files next to the database in the data directory, '
        'then report it to rotki with the traceback that follows this line '
        '(https://github.com/rotki/rotki/issues). Do not hand-edit the database.',
        'rotkehlchen/db/upgrade_manager.py:188, rotkehlchen/globaldb/upgrades/manager.py:192',
        ('db', 'from_v', 'to_v')),
    sig('db_newer_than_binary', 'database', 'Database is newer than this rotki',
        r'Your database version is newer than the version expected|but the GlobalDB found in the system is v\d+',
        'The account (or global DB) was last opened by a newer rotki than the one running now, '
        'usually after a downgrade.',
        'Install the latest rotki release again. rotki does not downgrade databases.',
        'rotkehlchen/db/upgrade_manager.py:119, rotkehlchen/globaldb/upgrades/manager.py:113'),
    sig('db_too_old', 'database', 'Database is too old to upgrade in one step',
        r'was last opened by a very old version of rotki',
        'The account was last opened by a rotki so old that this version cannot upgrade it directly.',
        'Open it once with an intermediate rotki version first, as described at '
        'https://docs.rotki.com/usage-guides#upgrading-rotki-after-a-long-time',
        'rotkehlchen/db/upgrade_manager.py:110, rotkehlchen/globaldb/upgrades/manager.py:104'),
    sig('db_wrong_password', 'database', 'Wrong password or unreadable database',
        r'Wrong password or invalid/corrupt database for user',
        'SQLCipher could not open the user database with the given password. Almost always the '
        'password; rarely a damaged file.',
        'Retry the password (it is case sensitive and has no recovery). If it is certainly right, '
        'restore the database from a backup.',
        'rotkehlchen/db/dbhandler.py:633'),
    sig('db_permission', 'database', 'Database file cannot be opened',
        r'Could not open database file: .*Permission errors\?',
        'The process cannot open its own database file, usually ownership or a read-only mount. '
        'In Docker, starling runs the backend as uid 10001 and expects to own /data.',
        'Fix the ownership of the data directory (or the /data volume) so the rotki user can write it.',
        'rotkehlchen/db/dbhandler.py:621'),
    sig('unhandled_exception', 'crash', 'Unhandled exception in an API request',
        r'Unhandled exception when processing endpoint request',
        'An API call hit a bug rotki did not handle. The traceback follows in the log.',
        'Update to the latest rotki. If it persists, report it with the traceback.',
        'rotkehlchen/api/server.py:677'),
    sig('startup_traceback', 'crash', 'Backend crashed with a traceback',
        r'Traceback \(most recent call last\)',
        'A CRITICAL entry carrying a traceback, which is how rotkehlchen/__main__.py records a '
        'backend that failed to start.',
        'Read the last line of the traceback; it usually names the cause. Report it if unclear.',
        'rotkehlchen/__main__.py:43-51'),
    # --- history coverage: outcomes rank above the indexer errors that cause them ----------
    sig('range_not_covered', 'coverage', 'History range no configured indexer covers',
        r'Skipping (?P<chain>\w+) (?:internal |token )?transactions query for \S+ from \d+ to \d+, '
        r'since no configured indexer covers it',
        'None of the indexers in the chain\'s order can serve that time range, so rotki leaves it '
        'unrecorded and will try again later. Until then, history in that range is missing, '
        'and so is anything a PnL report derives from it.',
        'Add an indexer that covers the range to the chain\'s order (for example Etherscan with a '
        'key that serves the chain).',
        'rotkehlchen/chain/evm/transactions.py:757, :1246, :1323', ('chain',)),
    sig('no_indexers', 'coverage', 'No indexer available for a chain',
        r'No indexers are available for (?P<chain>\w+)',
        'Every indexer for this chain has been removed (refused or failed), so transactions for '
        'it cannot be queried at all.',
        'Check the chain\'s indexer order and API keys (Settings: EVM Indexer order, and API Keys).',
        'rotkehlchen/chain/evm/node_inquirer.py:2551', ('chain',)),
    sig('transactions_not_added', 'coverage', 'Transactions query failed, some not added',
        r'while querying (?:internal )?(?P<chain>\w+) transactions from indexers\. (?:Some t|T)ransactions not added to the DB',
        'Every indexer failed for a range, with errors rather than a clean "not covered". The '
        'range will be retried, but until it succeeds those transactions are missing.',
        'Look at the errors quoted in the line. Rate limits: add a key. Outages: retry later.',
        'rotkehlchen/chain/evm/transactions.py:771, :1260', ('chain',)),
    # --- EVM indexers (etherscan, blockscout, routescan) -----------------------------------
    sig('indexer_daily_limit', 'indexer', 'Indexer daily limit reached',
        r'\b(?P<service>\w+) max daily rate limit reached',
        'The Etherscan key has used its daily call allowance. Every query for the rest of the day fails.',
        'Wait for the reset, or use a key with a larger allowance. Putting Blockscout or Routescan '
        'ahead of Etherscan in the chain\'s indexer order spreads the load.',
        'rotkehlchen/externalapis/etherscan.py:296', ('service',)),
    sig('indexer_backoff_exhausted', 'indexer', 'Indexer kept rate limiting after all retries',
        r'Getting (?P<service>\w+) too many requests error even after we incrementally backed off '
        r'while querying (?P<chain>\S+)'
        r'|\b(?P<chain2>\w+) (?P<service2>\w+) API request to .* failed due to backing off longer than the max backoff',
        'rotki backed off and retried until query_retry_limit and still got 429s, so the query was '
        'abandoned. History for that range is missing until it is retried.',
        'Add your own API key for that indexer (Settings > API Keys). A keyless or shared key is '
        'throttled hardest. Raising query_retry_limit only waits longer.',
        'rotkehlchen/externalapis/etherscan_like.py:211, :514', ('service', 'chain')),
    sig('indexer_throttled', 'indexer', 'Indexer rate limiting (backed off, retried)',
        r'Got too many requests error from (?P<chain>\S+) (?P<service>\w+)\. Will backoff'
        r'|Got response: .*(?:Max calls per sec rate|Max rate limit reached|Free API access is temporarily '
        r'unavailable).* from (?P<service2>\w+) while querying chain (?P<chain2>\w+)',
        'The indexer returned "too many requests" and rotki slowed down. In small numbers this is '
        'normal; in large numbers it is why syncing crawls.',
        'Use your own API key for the indexer. With a free Etherscan key, expect this during a '
        'first full sync of many accounts.',
        'rotkehlchen/externalapis/etherscan_like.py:217, rotkehlchen/externalapis/etherscan.py:287',
        ('service', 'chain')),
    sig('indexer_default_key', 'indexer', 'Using rotki\'s shared default indexer key',
        r'Using default (?!Nuthatch\b)(?P<service>\w+) key',
        'No personal key is set for this indexer, so rotki uses its shared default key, which is '
        'rate limited across all users.',
        'Add your own free key under Settings > API Keys for that service.',
        'rotkehlchen/externalapis/etherscan_like.py:377', ('service',)),
    sig('indexer_chain_refused', 'indexer', 'Indexer does not serve this chain with this key',
        r'Indexer (?P<service>\w+) doesnt support (?P<chain>\w+) with the given API key',
        'Etherscan\'s free tier does not cover every chain. rotki drops the indexer for that chain '
        'and tries the next one.',
        'Put Blockscout or Routescan first in that chain\'s indexer order (Settings: EVM Indexer order), '
        'add a Blockscout key where its endpoint needs one, or use a paid Etherscan plan.',
        'rotkehlchen/chain/evm/node_inquirer.py:2571, rotkehlchen/externalapis/etherscan.py:300',
        ('service', 'chain')),
    sig('blockscout_needs_key', 'indexer', 'Blockscout endpoint needs a key',
        r'Blockscout has no API key configured, which its endpoint for (?P<chain>\w+) requires',
        'This chain\'s Blockscout endpoint only accepts keyed requests, so rotki skips it.',
        'Add a Blockscout API key, or rely on another indexer for that chain.',
        'rotkehlchen/externalapis/blockscout.py:249', ('chain',)),
    sig('indexer_fallthrough', 'indexer', 'Indexer failed, rotki tried the next one',
        r'\bFailed to query (?P<service>Etherscan|Blockscout|Routescan|Nuthatch) due to ',
        'One indexer errored and rotki moved to the next in the order. Harmless if the next one '
        'answered; the reason is in the line.',
        'If one indexer fails constantly, move it down the order or fix its key.',
        'rotkehlchen/chain/evm/node_inquirer.py:2579', ('service',)),
    # --- RPC nodes ------------------------------------------------------------------------
    sig('node_wrong_network', 'node', 'RPC node is on the wrong chain',
        r'Connected to (?P<chain>\w+) node .* but it is not on the expected network value',
        'The endpoint answers, but for a different chain id. Usually an RPC URL pasted under the wrong chain.',
        'Fix the endpoint under Settings > RPC Nodes for that chain.',
        'rotkehlchen/chain/mixins/rpc_nodes.py:503', ('chain',)),
    sig('node_connect_failed', 'node', 'Could not connect to an RPC node',
        r'Failed to connect to (?P<chain>\w+) node (?P<node>.+?) at endpoint',
        'rotki could not open a connection to the node at startup or on reconnect. Public nodes '
        'come and go; one failure among several working nodes is harmless.',
        'If it is your own node, check it is up and reachable from rotki (from Docker, localhost '
        'is the container, not the host). Disable dead public nodes under Settings > RPC Nodes.',
        'rotkehlchen/chain/mixins/rpc_nodes.py:464, :468, :522, :564', ('chain', 'node')),
    sig('node_timeout', 'node', 'RPC node timed out',
        r'Timed out while querying (?P<node>\S+) for',
        'A node did not answer within the read timeout and is skipped for the rest of the session.',
        'Raise read_timeout (Settings > General) if your own node is just slow, or replace the node.',
        'rotkehlchen/chain/evm/node_inquirer.py:718', ('node',)),
    sig('node_all_failed', 'node', 'Every RPC node failed for a call',
        r'Failed to query (?P<method>\w+) after trying the following nodes',
        'No node in the call order could answer. Balances or receipts for that call are missing.',
        'Add a reliable node (your own, or a keyed provider) and make sure it is active.',
        'rotkehlchen/chain/evm/node_inquirer.py:756', ('method',)),
    sig('node_query_failed', 'node', 'RPC node errored on a call (next node tried)',
        r'Failed to query (?P<node>.+?) with position on the query list \d+ for (?P<method>\w+) due to',
        'A node returned an error and rotki tried the next. Rate limits from public nodes show up here.',
        'Only a problem if the same node fails constantly; then deactivate it.',
        'rotkehlchen/chain/evm/node_inquirer.py:738', ('node', 'method')),
    sig('no_archive_node', 'node', 'No archive node for historical balances',
        r'No archive node available for (?P<chain>\w+)',
        'A historical balance needs an archive node and none is configured for the chain.',
        'Add an archive-capable RPC for that chain if you need historical balances.',
        'rotkehlchen/chain/evm/node_inquirer.py:501, :594', ('chain',)),
    sig('chain_unsynced', 'node', 'Own node not synced',
        r'Assuming unsynced chain',
        'The configured own node returned output that suggests it is not fully synced.',
        'Let the node finish syncing, or point rotki at a synced one.',
        'rotkehlchen/chain/aggregator.py:1344, rotkehlchen/chain/evm/manager.py:179'),
    sig('beacon_node_failed', 'node', 'Beacon node unreachable',
        r'Failed to connect to (?:given )?beacon node',
        'The configured consensus-layer endpoint could not be reached.',
        'Check the beacon node URL under Settings > RPC Nodes, or clear it.',
        'rotkehlchen/rotkehlchen.py:1628, rotkehlchen/chain/ethereum/modules/eth2/beacon.py:245'),
    # --- price oracles --------------------------------------------------------------------
    sig('oracle_rate_limited', 'prices', 'Price oracle rate limiting',
        r'Got rate limited by (?P<service>coingecko|Defillama|cryptocompare)',
        'The price oracle returned 429. Historical prices for many assets are the usual trigger, '
        'such as the first PnL report over a long period.',
        'Put DefiLlama first among the historical price oracles (rotki suggests this itself, as '
        'it rate limits less), add free CoinGecko and CryptoCompare keys under API Keys, then '
        'regenerate the report.',
        'rotkehlchen/externalapis/coingecko.py:683, rotkehlchen/externalapis/defillama.py:159, '
        'rotkehlchen/externalapis/cryptocompare.py:409', ('service',)),
    sig('oracle_penalized', 'prices', 'Price oracle benched after repeated failures',
        r'Penalizing price oracle (?P<service>\S+) for (?P<seconds>\d+) seconds',
        'After oracle_penalty_threshold_count failures rotki stops asking that oracle for '
        'oracle_penalty_duration seconds (default 1800). Prices asked meanwhile come from the '
        'remaining oracles or not at all.',
        'Fix the cause (usually rate limits, see above) and regenerate the report after the penalty expires.',
        'rotkehlchen/utils/mixins/penalizable_oracle.py:75', ('service',)),
    sig('missing_price', 'prices', 'No historical price found',
        r'Unable to query a historical price for "(?P<asset>[^"]+)" to "(?P<to_asset>[^"]+)"'
        r'|Could not query the historical (?P<to_asset2>\S+) price for (?P<asset2>\S+) at time'
        r'|Could not query price for (?P<asset3>\S+) and time',
        'No oracle had a price for that asset at that time. In a PnL report the event is either '
        'valued at zero or skipped and listed as a missing price.',
        'If oracles were rate limited, fix that first and regenerate. Otherwise add a manual '
        'historical price for the asset (Manage Prices, Historical prices), or mark worthless '
        'spam tokens as ignored.',
        'rotkehlchen/errors/price.py:25, rotkehlchen/history/price.py:597, :80', ('asset',)),
    sig('pnl_event_skipped', 'accounting', 'Event skipped during a PnL report',
        r'Skipping event with id (?P<event_id>\S+)\s+during history processing due to (?P<reason>[^:]+)',
        'The accountant could not process an event and left it out of the report.',
        'The reason is in the line. Missing prices: see above. Otherwise fix or ignore the event '
        'and regenerate the report.',
        'rotkehlchen/accounting/accountant.py:126', ('reason',)),
    # --- exchanges (generic, last) ----------------------------------------------------------
    sig('exchange_api_failed', 'exchange', 'Exchange or service API request failed',
        r'\b(?P<service>[A-Z][\w.]+(?: [A-Z][\w.]+)?) (?:account )?API request .*failed',
        'A request to an exchange or external service failed. Exchange history for the affected '
        'period may be incomplete.',
        'Check the API key and its permissions for that exchange, and the system clock (several '
        'exchanges reject requests with skewed timestamps).',
        'rotkehlchen/exchanges/*.py (e.g. binance.py:413, coinbase.py:333)', ('service',)),
]

VERSION_LINE = re.compile(r'Starting rotki (?P<version>\S+)')  # rotkehlchen/api/server.py:924


@dataclass
class Hit:
    count: int = 0
    first: str = ''
    last: str = ''
    levels: dict = field(default_factory=dict)
    keys: dict = field(default_factory=dict)
    example: str = ''


@dataclass
class Record:
    ts: str
    level: str
    logger: str
    msg: str
    source: str


def candidate_log_dirs() -> list[Path]:
    """Where rotki writes logs, by install type. Electron: LogService in
    frontend/app/electron/main/log-service.ts sets <appData>/rotki/logs. Docker: /logs
    (Dockerfile, --logs-dir), commonly bind-mounted from ~/.rotki/logs."""
    home = Path.home()
    dirs = []
    system = platform.system()
    if system == 'Darwin':
        dirs.append(home / 'Library' / 'Application Support' / 'rotki' / 'logs')
    elif system == 'Windows':
        appdata = os.environ.get('APPDATA')
        if appdata:
            dirs.append(Path(appdata) / 'rotki' / 'logs')
    else:
        xdg = os.environ.get('XDG_CONFIG_HOME') or str(home / '.config')
        dirs.append(Path(xdg) / 'rotki' / 'logs')
    dirs.append(home / '.rotki' / 'logs')
    dirs.append(Path('/logs'))
    return dirs


def find_log_files(paths: list[str]) -> list[Path]:
    found = []
    targets = [Path(p).expanduser() for p in paths] if paths else candidate_log_dirs()
    for target in targets:
        if target.is_file():
            found.append(target)
        elif target.is_dir():
            # rotkehlchen.log plus RotatingFileHandler backups (.1, .2 ...); develop builds
            # prefix a date. The electron and colibri logs have other formats and are skipped.
            for f in sorted(target.iterdir()):
                if re.search(r'rotkehlchen\.log(\.\d+)?(\.gz)?$', f.name):
                    found.append(f)
    return found


def open_text(path: Path):
    if path.suffix == '.gz':
        return gzip.open(path, 'rt', encoding='utf-8', errors='replace')
    return open(path, encoding='utf-8', errors='replace')


def iter_records(lines: Iterable[str], source: str) -> Iterator[Record]:
    current = None
    for raw in lines:
        line = raw.rstrip('\n')
        m = FILE_LINE.match(line) or STDOUT_LINE.match(line)
        if m:
            if current is not None:
                yield current
            current = Record(m['ts'], m['level'], m['logger'], m['msg'], source)
        elif current is not None and line.strip():
            current.msg += '\n' + line
    if current is not None:
        yield current


def parse_ts(ts: str):
    for fmt in ('%d/%m/%Y %H:%M:%S', '%Y-%m-%dT%H:%M:%S'):
        try:
            return datetime.strptime(ts, fmt)
        except ValueError:
            continue
    return None


def redact(text: str) -> str:
    for pattern, repl in REDACTIONS:
        text = pattern.sub(repl, text)
    return text


def first_group(m: re.Match, name: str):
    for suffix in ('', '2', '3'):
        value = m.groupdict().get(name + suffix)
        if value:
            return value
    return None


def node_label(raw: str) -> str:
    m = re.search(r"name='([^']+)'", raw)
    return m.group(1) if m else raw.split(' ')[0]


def scan(records: Iterable[Record], since=None, do_redact=True) -> dict:
    hits: dict[str, Hit] = {}
    levels = dict.fromkeys(LEVELS, 0)
    versions = set()
    first_ts = last_ts = None
    total = 0
    for rec in records:
        when = parse_ts(rec.ts)
        if since is not None and when is not None and when < since:
            continue
        total += 1
        levels[rec.level] = levels.get(rec.level, 0) + 1
        if when is not None:
            first_ts = when if first_ts is None or when < first_ts else first_ts
            last_ts = when if last_ts is None or when > last_ts else last_ts
        if (vm := VERSION_LINE.search(rec.msg)) and rec.logger.startswith('rotkehlchen.api'):
            versions.add(vm['version'])
        body = rec.msg.strip().split('\n')
        # head[:4000] bounds the cost of API responses dumped into DEBUG lines; a traceback's
        # last line names the exception, so it is tried as well
        texts = [body[0][:4000]] + ([body[-1][:4000]] if len(body) > 1 else [])
        head = body[0]
        for s in SIGNATURES:
            if s.id == 'startup_traceback':
                # __main__.py logs the whole traceback as one CRITICAL record
                if rec.level != 'CRITICAL':
                    continue
                m = s.pattern.search(rec.msg)
            else:
                m = next((x for x in (s.pattern.search(t) for t in texts) if x), None)
            if not m:
                continue
            hit = hits.setdefault(s.id, Hit())
            hit.count += 1
            hit.first = hit.first or rec.ts
            hit.last = rec.ts
            hit.levels[rec.level] = hit.levels.get(rec.level, 0) + 1
            for key in s.keys:
                value = first_group(m, key)
                if value is None:
                    continue
                if key == 'node':
                    value = node_label(value)
                if do_redact:
                    value = redact(value)
                bucket = hit.keys.setdefault(key, {})
                bucket[value] = bucket.get(value, 0) + 1
            if not hit.example:
                example = rec.msg.strip().splitlines()[-1] if s.id == 'startup_traceback' else head
                example = example[:400]
                hit.example = redact(example) if do_redact else example
            break
    return {
        'records': total,
        'levels': levels,
        'versions': sorted(versions),
        'first_ts': first_ts.isoformat(sep=' ') if first_ts else None,
        'last_ts': last_ts.isoformat(sep=' ') if last_ts else None,
        'hits': hits,
    }


SETTINGS_KEYS = (
    'have_premium', 'evm_indexers_order', 'default_evm_indexer_order', 'historical_price_oracles',
    'current_price_oracles', 'query_retry_limit', 'connect_timeout', 'read_timeout',
    'oracle_penalty_threshold_count', 'oracle_penalty_duration', 'suppress_missing_key_msg_services',
)
NODE_CHAINS = ('eth', 'optimism', 'arbitrum_one', 'base', 'polygon_pos', 'gnosis', 'binance_sc')


def api_get(base: str, path: str, timeout: float):
    url = base.rstrip('/') + '/api/1/' + path
    req = urllib.request.Request(url, headers={'Accept': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - local API
            return json.loads(resp.read().decode('utf-8')).get('result'), None
    except urllib.error.HTTPError as e:
        try:
            message = json.loads(e.read().decode('utf-8')).get('message', '')
        except (ValueError, OSError):
            message = ''
        return None, f'HTTP {e.code} {message}'.strip()
    except (urllib.error.URLError, OSError, ValueError) as e:
        return None, str(e)


def host_only(endpoint: str) -> str:
    if not endpoint:
        return ''
    parsed = urllib.parse.urlparse(endpoint)
    return f'{parsed.scheme}://{parsed.hostname}' if parsed.hostname else '<set>'


def probe_api(base: str, timeout: float = 5.0) -> dict:
    """Small read-only calls only. No balances, no history, no key values."""
    out: dict = {'base': base, 'errors': {}}
    info, err = api_get(base, 'info', timeout)
    if err:
        out['errors']['info'] = err
        return out
    out['version'] = (info or {}).get('version', {}).get('our_version')
    out['latest_version'] = (info or {}).get('version', {}).get('latest_version')
    out['log_level'] = (info or {}).get('log_level')
    out['data_directory'] = (info or {}).get('data_directory')
    settings, err = api_get(base, 'settings', timeout)
    if err:
        out['errors']['settings'] = err + ' (settings need a logged-in account)'
    else:
        out['settings'] = {k: settings.get(k) for k in SETTINGS_KEYS if k in (settings or {})}
    services, err = api_get(base, 'external_services', timeout)
    if err:
        out['errors']['external_services'] = err
    else:
        out['services_with_keys'] = sorted((services or {}).keys())  # names only, never values
    nodes = {}
    for chain in NODE_CHAINS:
        result, err = api_get(base, f'blockchains/{chain}/nodes', timeout)
        if err:
            continue
        nodes[chain] = [
            {
                'name': n.get('name'),
                'host': host_only(n.get('endpoint', '')),
                'owned': n.get('owned'),
                'active': n.get('active'),
                'weight': n.get('weight'),
            }
            for n in (result or [])
        ]
    out['nodes'] = nodes
    return out


def level_warning(result: dict) -> str | None:
    levels = result['levels']
    if result['records'] == 0:
        return None
    if levels.get('DEBUG', 0) == 0 and levels.get('INFO', 0) == 0:
        return (
            'Only WARNING and above were logged. Packaged rotki and the Docker image default to '
            'CRITICAL, which hides most of what this scan looks for. Raise the log level to DEBUG '
            '(login screen > advanced settings, or LOGLEVEL=debug in Docker), reproduce the '
            'problem, and scan again.'
        )
    return None


def render_text(files: list[str], result: dict, api: dict | None) -> str:
    lines = [f'rotki-doctor: signatures from {ROTKI_REF}']
    lines.append(f'Read {len(files)} file(s): ' + (', '.join(files) if files else 'none'))
    if result['records'] == 0:
        lines.append('No rotki log records found. Check the path, or that logging is on.')
    else:
        lines.append(
            f"{result['records']} records, {result['first_ts']} to {result['last_ts']}. "
            'Levels: ' + ', '.join(f'{k} {v}' for k, v in result['levels'].items() if v),
        )
    if result['versions']:
        lines.append('rotki version(s) seen starting: ' + ', '.join(result['versions']))
    if warn := level_warning(result):
        lines.append('NOTE: ' + warn)
    if api is not None:
        lines.append('')
        lines.append(f"API {api['base']}:")
        for key in ('version', 'latest_version', 'log_level', 'data_directory'):
            if api.get(key) is not None:
                lines.append(f'  {key}: {api[key]}')
        if 'settings' in api:
            for k, v in api['settings'].items():
                lines.append(f'  {k}: {json.dumps(v)}')
        if 'services_with_keys' in api:
            lines.append('  services with a key set: ' + (', '.join(api['services_with_keys']) or 'none'))
        for chain, nodes in api.get('nodes', {}).items():
            active = [f"{n['name']}{' (own)' if n['owned'] else ''}" for n in nodes if n['active']]
            lines.append(f'  {chain} active nodes: ' + (', '.join(active) or 'none'))
        for k, v in api['errors'].items():
            lines.append(f'  {k}: unavailable ({v})')
    hits = result['hits']
    lines.append('')
    if not hits:
        lines.append('No known failure signatures matched.')
        return '\n'.join(lines)
    by_id = {s.id: s for s in SIGNATURES}
    order = sorted(hits, key=lambda i: (SIGNATURES.index(by_id[i]), -hits[i].count))
    lines.append(f'{len(hits)} signature(s) matched, most serious first:')
    for sid in order:
        s, h = by_id[sid], hits[sid]
        lines.append('')
        lines.append(f'[{s.category}] {s.title}  x{h.count}  ({h.first} .. {h.last})')
        for key, values in h.keys.items():
            top = sorted(values.items(), key=lambda kv: -kv[1])[:6]
            lines.append(f'  {key}: ' + ', '.join(f'{v} ({n})' for v, n in top))
        lines.append(f'  what: {s.explain}')
        lines.append(f'  fix:  {s.fix}')
        lines.append(f'  source: {s.source}')
        lines.append(f'  e.g.: {h.example.splitlines()[0]}')
    if note := nest_note(hits):
        lines.append('')
        lines.append(note)
    return '\n'.join(lines)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description='Scan rotki logs for known failure signatures.')
    p.add_argument('--log', action='append', default=[],
                   help='log file or directory; repeatable; "-" reads stdin (docker logs)')
    p.add_argument('--since-days', type=float, help='only records from the last N days of the log')
    p.add_argument('--api', help='rotki API base, e.g. http://127.0.0.1:4242 (optional)')
    p.add_argument('--json', action='store_true', help='machine-readable output')
    p.add_argument('--no-redact', action='store_true', help='keep addresses, hashes and URLs in examples')
    p.add_argument('--list-dirs', action='store_true', help='print where logs are looked for and exit')
    args = p.parse_args(argv)

    if args.list_dirs:
        for d in candidate_log_dirs():
            print(f"{d}{'  (exists)' if d.exists() else ''}")
        return 0

    sources: list[tuple[str, Iterable[str] | None]] = []
    files: list[str] = []
    paths = [x for x in args.log if x != '-']
    if '-' in args.log:
        sources.append(('<stdin>', sys.stdin.readlines()))
        files.append('<stdin>')
    if paths or not args.log:
        for f in find_log_files(paths):
            files.append(str(f))
            sources.append((str(f), None))

    def all_records():
        for name, stream in sources:
            if stream is not None:
                yield from iter_records(stream, name)
            else:
                with open_text(Path(name)) as fh:
                    yield from iter_records(fh, name)

    since = None
    if args.since_days is not None:
        # relative to the newest record, so an old log still yields its last N days
        newest = None
        for rec in all_records():
            t = parse_ts(rec.ts)
            if t and (newest is None or t > newest):
                newest = t
        since = (newest or datetime.now()) - timedelta(days=args.since_days)

    result = scan(all_records(), since=since, do_redact=not args.no_redact)
    api = probe_api(args.api) if args.api else None

    if args.json:
        payload = dict(result)
        payload['files'] = files
        payload['hits'] = {
            k: {**vars(v), 'category': next(s.category for s in SIGNATURES if s.id == k)}
            for k, v in result['hits'].items()
        }
        payload['level_warning'] = level_warning(result)
        payload['nest_note'] = nest_note(result['hits'])
        payload['api'] = api
        print(json.dumps(payload, indent=2, default=str))
    else:
        print(render_text(files, result, api))
    return 0 if files else 2


if __name__ == '__main__':
    sys.exit(main())
