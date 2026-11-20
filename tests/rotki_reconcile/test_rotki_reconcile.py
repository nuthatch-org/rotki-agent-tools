import importlib.util
import io
import json
import sys
import tempfile
import threading
import unittest
import urllib.parse
import zipfile
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent / 'fixtures'
spec = importlib.util.spec_from_file_location(
    'rotki_reconcile', ROOT / 'skills/rotki-reconcile/scripts/rotki_reconcile.py',
)
rec = importlib.util.module_from_spec(spec)
sys.modules['rotki_reconcile'] = rec  # dataclasses look the module up by name
spec.loader.exec_module(rec)

TEXT = FIXTURES / 'all_events_text.csv'
FORMULAS = FIXTURES / 'all_events_formulas.csv'
H = {k: '0x' + k * 64 for k in 'abcdef'}
ADDRESS = '0x4444444444444444444444444444444444444444'


def run_json(*argv):
    out = io.StringIO()
    with redirect_stdout(out):
        code = rec.main([*map(str, argv), '--json'])
    return code, json.loads(out.getvalue())


class TextExport(unittest.TestCase):
    def setUp(self):
        _, self.r = run_json(TEXT, '--from', '2024-01-01', '--to', '2024-12-31')

    def test_reads_summary_rows(self):
        self.assertEqual(self.r['export']['events'], 7)
        self.assertEqual(self.r['export']['summary'],
                         {'rotki version': '1.41.2', 'cost_basis_method': 'fifo'})
        self.assertFalse(self.r['export']['formulas'])
        self.assertEqual(self.r['export']['unparsed_dates'], 0)

    def test_zero_price(self):
        self.assertEqual([x['asset'] for x in self.r['zero_price']], ['SPAM'])

    def test_missing_acquisitions(self):
        got = {x['asset']: x['reason'] for x in self.r['missing_acquisitions']}
        self.assertEqual(got['UNI'], 'rotki marked the cost basis incomplete')
        self.assertEqual(got['ETH'], 'acquisitions cover 1 of 1.5')
        self.assertEqual(len(got), 2)  # the full spend, the fee and the fiat spend are fine

    def test_gaps_include_period_edges(self):
        kraken = self.r['gaps']['kraken']['gaps']
        self.assertEqual([g['days'] for g in kraken], [244, 120])
        self.assertEqual([g['days'] for g in self.r['gaps']['ethereum']['gaps']], [256])

    def test_gap_threshold(self):
        _, r = run_json(TEXT, '--gap-days', '300')
        self.assertTrue(all(not g['gaps'] for g in r['gaps'].values()))


class FormulaExport(unittest.TestCase):
    def setUp(self):
        _, self.r = run_json(FORMULAS)

    def test_semicolon_and_formulas_detected(self):
        self.assertEqual(self.r['export']['delimiter'], ';')
        self.assertTrue(self.r['export']['formulas'])

    def test_formula_cost_basis_is_read(self):
        got = {x['asset']: x['reason'] for x in self.r['missing_acquisitions']}
        self.assertEqual(got['ETH'], 'acquisitions cover 1 of 1.5')
        self.assertIn('UNI', got)  # the free column keeps rotki's marker when its PnL is zero

    def test_marker_lost_to_formulas_is_possible(self):
        self.assertEqual([x['asset'] for x in self.r['possible_missing_acquisitions']], ['BTC'])


class Inputs(unittest.TestCase):
    def test_zip_export(self):
        with tempfile.TemporaryDirectory() as d:
            z = Path(d) / 'csv.zip'
            with zipfile.ZipFile(z, 'w') as zf:
                zf.write(TEXT, 'all_events.csv')
            _, r = run_json(z)
        self.assertEqual(r['export']['events'], 7)

    def test_not_a_pnl_export(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'x.csv'
            p.write_text('a,b\n1,2\n')
            with self.assertRaises(SystemExit):
                rec.main([str(p)])

    def test_date_formats(self):
        self.assertEqual(rec.parse_date('15/03/2024 10:00:00 CET', None).month, 3)
        self.assertEqual(rec.parse_date('2024-03-15 10:00:00 UTC', None).day, 15)
        self.assertEqual(rec.parse_date('03/15/2024 10:00:00', '%m/%d/%Y %H:%M:%S %Z').day, 15)
        self.assertIsNone(rec.parse_date('', None))


class CrossCheckFile(unittest.TestCase):
    def test_missing_hash(self):
        _, r = run_json(TEXT, '--onchain-hashes', FIXTURES / 'onchain_hashes.txt')
        cc = r['crosscheck']
        self.assertEqual((cc['onchain'], cc['in_export']), (5, 4))
        self.assertEqual(cc['missing'], [H['e']])


class FakeNest(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):  # noqa: N802
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query))
        if q.get('action') == 'getblocknobytime':
            body = {'status': '1', 'message': 'OK', 'result': '100' if q['closest'] == 'after' else '200'}
        elif q.get('action') == 'txlist':
            assert (q['startblock'], q['endblock']) == ('100', '200'), q
            rows = [{'hash': H[k], 'from': ADDRESS, 'isError': '0'} for k in 'abe']
            rows.append({'hash': H['f'], 'from': '0x' + '9' * 40, 'isError': '1'})  # failed incoming
            body = {'status': '1', 'message': 'OK', 'result': rows if q.get('page') == '1' else []}
        elif q.get('action') == 'tokentx':
            body = {'status': '0', 'message': 'NOTOK', 'result': 'NUTHATCH_INCOMPLETE: history starts at block 150'}
        else:
            body = {'status': '0', 'message': 'No transactions found', 'result': []}
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(data)


class CrossCheckNest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(('127.0.0.1', 0), FakeNest)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def test_nest_crosscheck(self):
        _, r = run_json(TEXT, '--nest', self.url, '--address', ADDRESS,
                        '--from', '2024-01-01', '--to', '2024-12-31', '--actions', 'txlist,tokentx,txlistinternal')
        cc = r['crosscheck']
        self.assertIn('blocks 100-200', cc['source'])
        self.assertEqual(cc['missing'], [H['e']])
        self.assertEqual(cc['onchain'], 3)  # the failed incoming tx is not expected
        self.assertEqual(len(cc['problems']), 1)
        self.assertIn('NUTHATCH_INCOMPLETE', cc['problems'][0])


if __name__ == '__main__':
    unittest.main()
