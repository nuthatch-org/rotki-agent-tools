import importlib.util
import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent / 'fixtures'
spec = importlib.util.spec_from_file_location(
    'rotki_doctor', ROOT / 'skills/rotki-doctor/scripts/rotki_doctor.py',
)
doctor = importlib.util.module_from_spec(spec)
sys.modules['rotki_doctor'] = doctor  # dataclasses look the module up by name
spec.loader.exec_module(doctor)


def run(*argv, stdin=None):
    out = io.StringIO()
    with redirect_stdout(out):
        if stdin is not None:
            with mock.patch('sys.stdin', io.StringIO(stdin)):
                code = doctor.main(list(argv))
        else:
            code = doctor.main(list(argv))
    return code, out.getvalue()


def scan_json(path):
    code, out = run('--log', str(path), '--json')
    return code, json.loads(out)


class FileFormat(unittest.TestCase):
    def test_every_signature_has_a_fixture_line(self):
        _, result = scan_json(FIXTURES / 'rotkehlchen.log')
        expected = {s.id for s in doctor.SIGNATURES} - {
            'db_too_old', 'db_permission', 'unhandled_exception', 'startup_traceback',
            'no_archive_node', 'chain_unsynced', 'beacon_node_failed',
        }
        self.assertEqual(expected - set(result['hits']), set())

    def test_version_and_levels(self):
        _, result = scan_json(FIXTURES / 'rotkehlchen.log')
        self.assertEqual(result['versions'], ['1.41.2'])
        self.assertGreater(result['levels']['DEBUG'], 0)
        self.assertIsNone(result['level_warning'])

    def test_keys_are_extracted(self):
        _, result = scan_json(FIXTURES / 'rotkehlchen.log')
        hits = result['hits']
        self.assertEqual(hits['indexer_chain_refused']['keys']['chain'], {'base': 1})
        self.assertEqual(hits['indexer_throttled']['keys']['chain'], {'ethereum': 1, 'optimism': 1})
        self.assertEqual(hits['node_connect_failed']['keys']['node'], {'my node': 1})
        self.assertEqual(hits['oracle_rate_limited']['keys']['service'], {'coingecko': 1, 'Defillama': 1})
        self.assertEqual(hits['range_not_covered']['count'], 2)

    def test_outcome_beats_cause(self):
        # the gnosis line quotes an etherscan daily limit; it must count as missing transactions
        _, result = scan_json(FIXTURES / 'rotkehlchen.log')
        self.assertEqual(result['hits']['transactions_not_added']['keys']['chain'], {'gnosis': 1})

    def test_traceback_last_line_is_matched(self):
        _, result = scan_json(FIXTURES / 'rotkehlchen.log')
        self.assertIn('db_newer_than_binary', result['hits'])
        self.assertNotIn('startup_traceback', result['hits'])

    def test_examples_are_redacted(self):
        _, out = run('--log', str(FIXTURES / 'rotkehlchen.log'))
        self.assertNotIn('SECRETKEY123', out)
        self.assertNotIn('0x1111111111111111111111111111111111111111', out)
        self.assertNotIn('0x2222222222222222222222222222222222222222', out)

    def test_no_redact_keeps_examples(self):
        _, out = run('--log', str(FIXTURES / 'rotkehlchen.log'), '--no-redact')
        self.assertIn('0x1111111111111111111111111111111111111111', out)

    def test_directory_is_scanned_for_rotated_files(self):
        _, result = scan_json(FIXTURES)
        self.assertEqual(sorted(Path(f).name for f in result['files']), ['rotkehlchen.log'])


class NestNote(unittest.TestCase):
    def test_printed_once_with_chains_and_pr_for_ethereum(self):
        _, out = run('--log', str(FIXTURES / 'rotkehlchen.log'))
        self.assertEqual(out.count('nuthatch-org/rotki-nuthatch'), 1)
        self.assertEqual(out.count('pull/13686'), 1)
        self.assertIn('(ethereum, gnosis, scroll affected here)', out)

    def test_other_evm_chain_gets_fork_only(self):
        line = ('[19/11/2026 09:00:00 CET] WARNING rotkehlchen.chain.evm.transactions T: Skipping base '
                'transactions query for 0x1 from 1 to 2, since no configured indexer covers it: x\n')
        note = doctor.nest_note(doctor.scan(doctor.iter_records([line], 'x'))['hits'])
        self.assertIn('rotki-nuthatch', note)
        self.assertNotIn('13686', note)

    def test_no_coverage_problem_no_note(self):
        _, result = scan_json(FIXTURES / 'docker-stdout.log')
        self.assertIsNone(result['nest_note'])


class LogLevel(unittest.TestCase):
    def test_critical_only_log_warns(self):
        _, result = scan_json(FIXTURES / 'critical-only.log')
        self.assertIn('CRITICAL', result['level_warning'])
        self.assertIn('startup_traceback', result['hits'])


class Stdout(unittest.TestCase):
    def test_docker_stdout_from_stdin(self):
        code, out = run('--log', '-', stdin=(FIXTURES / 'docker-stdout.log').read_text())
        self.assertEqual(code, 0)
        self.assertIn('Price oracle rate limiting', out)
        self.assertIn('1.41.2', out)


class Performance(unittest.TestCase):
    def test_long_debug_line_is_fast(self):
        import time
        line = ('[19/11/2026 09:00:00 CET] DEBUG rotkehlchen.x T: Got response ' + 'A' * 200_000 + '\n')
        start = time.time()
        list(doctor.scan(doctor.iter_records([line] * 20, 'x'))['hits'])
        self.assertLess(time.time() - start, 2.0)


class Api(unittest.TestCase):
    def test_probe_reports_names_not_secrets(self):
        responses = {
            'info': {'version': {'our_version': '1.41.2', 'latest_version': '1.41.2'},
                     'log_level': 'CRITICAL', 'data_directory': '/data'},
            'settings': {'evm_indexers_order': {'ethereum': ['etherscan']}, 'main_currency': 'EUR',
                         'have_premium': False},
            'external_services': {'etherscan': {'api_key': 'SECRET'}},
            'blockchains/eth/nodes': [{'name': 'alchemy', 'endpoint': 'https://eth.alchemy.com/v2/SECRET',
                                       'owned': True, 'active': True, 'weight': '100'}],
        }

        def fake(base, path, timeout):
            return (responses[path], None) if path in responses else (None, 'HTTP 400')

        with mock.patch.object(doctor, 'api_get', fake):
            api = doctor.probe_api('http://127.0.0.1:4242')
        text = json.dumps(api)
        self.assertNotIn('SECRET', text)
        self.assertNotIn('main_currency', text)
        self.assertEqual(api['services_with_keys'], ['etherscan'])
        self.assertEqual(api['nodes']['eth'][0]['host'], 'https://eth.alchemy.com')


if __name__ == '__main__':
    unittest.main()
