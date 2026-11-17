"""Regressions from the November 17 console runs; no real REST calls."""
import contextlib
import io
import json
import os
import tempfile
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
import requests
import bot
from execution_checks import TradingHalt, risk_allows


def security(ticker, position=0, weight=1):
    return dict(ticker=ticker, position=position, size=1000, last=100, bid=99.9, ask=100.1,
                is_tradeable=True, is_shortable='F' in ticker, max_trade_size=30,
                limits=[dict(name='COM5', units=weight)])


class Exchange:
    def __init__(self):
        self.rows = [security(t) for t in ('CL-1F', 'CL-2F', 'CL', 'CL-AK', 'CL-NYC', 'HO', 'RB')]
        self.limits = [dict(name='COM5', gross_limit=500, net_limit=100)]
        self.orders = []
        self.calls = []
        self.partial = None
        self.reject = False

    def get(self, endpoint, params=None):
        if endpoint == '/orders':
            return [dict(o) for o in self.orders if o['status'] == 'OPEN']
        if endpoint == '/securities': return [dict(r) for r in self.rows]
        if endpoint == '/limits': return self.limits
        raise AssertionError(endpoint)

    def post(self, endpoint, params=None):
        self.calls.append((endpoint, params))
        if endpoint == '/commands/cancel':
            for order in self.orders: order['status'] = 'CANCELLED'
            return {'success': True}
        if endpoint == '/orders':
            if self.reject: raise requests.HTTPError('400 rejected')
            qty = params['quantity']
            done = qty if self.partial is None else min(qty, self.partial)
            row = next(r for r in self.rows if r['ticker'] == params['ticker'])
            row['position'] += done * (1 if params['action'] == 'BUY' else -1)
            order = dict(params, order_id=len(self.orders)+1, quantity_filled=done,
                         status='TRANSACTED' if done == qty else 'CANCELLED')
            self.orders.append(order)
            return dict(order)
        raise AssertionError(endpoint)


class ConsoleFailureTests(unittest.TestCase):
    def setUp(self):
        self.exchange = Exchange()
        self.config = bot.CaseConfig()
        self.config.tickers = {r['ticker']: dict(size=1000, shortable=r['is_shortable']) for r in self.exchange.rows}
        self.state = bot.StateManager(self.config)
        self.state.period, self.state.tick = 1, 390
        self.execution = bot.ExecutionEngine(self.state, self.config)
        self.storage = Mock()
        self.storage.ensure_storage.return_value = True
        self.addCleanup(self.execution.executor.shutdown)
        self.enterContext(patch.object(bot, '_get', side_effect=self.exchange.get))
        self.enterContext(patch.object(bot, '_post', side_effect=self.exchange.post))
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        self.execution.refresh_positions()

    def eia(self):
        bot.EIAEngine(self.state, self.execution, self.storage, self.config).process(
            dict(news_id=3, headline='WEEK 3 CL ACTUAL DRAW 2 MLN BBLS VS FORECAST BUILD 10 MLN BBLS', body=''))

    def test_eia_shared_net_limit_and_no_unnecessary_tanks(self):
        self.exchange.rows[0]['position'] = 10
        self.execution.refresh_positions()
        self.eia()
        trades = [p for endpoint, p in self.exchange.calls if endpoint == '/orders']
        self.assertEqual(sum(p['quantity'] for p in trades), 90)
        self.assertEqual({p['ticker'] for p in trades}, {'CL-1F'})
        self.assertEqual(self.state.strategy_gross['eia'], 90)
        self.assertEqual(sum(e[3] for e in self.state.scheduled_exits), 90)
        self.assertEqual([e[0] for e in self.state.scheduled_exits], [400, 420, 450])
        self.storage.ensure_storage.assert_not_called()

    def test_partial_fill_stops_split_and_counts_actual_quantity(self):
        self.exchange.partial = 7
        orders = self.execution.place_split_orders('CL-1F', 'BUY', 100)
        self.assertEqual(bot.filled_quantity(orders), 7)
        self.assertEqual(len(orders), 1)
        self.assertEqual(self.state.get_position('CL-1F'), 7)

    def test_rejected_split_does_not_send_remaining_chunks(self):
        self.exchange.reject = True
        result = self.execution.place_split_orders('CL-1F', 'BUY', 100)
        self.assertEqual(result, [])
        self.assertEqual(sum(e == '/orders' for e, _ in self.exchange.calls), 1)

    def test_failed_eia_orders_create_no_exit_exposure(self):
        self.exchange.reject = True
        self.eia()
        self.assertEqual(self.state.strategy_gross['eia'], 0)
        self.assertEqual(sum(e[3] for e in self.state.scheduled_exits), 0)

    def test_scheduled_exit_cannot_reverse_position(self):
        self.exchange.rows[0]['position'] = 8
        self.state.scheduled_exits = [(390, 'CL-1F', 'SELL', 50, 'eia')]
        risk = bot.RiskManager(self.state, self.execution, self.storage, self.config)
        risk.process_scheduled_exits()
        self.assertEqual(self.exchange.rows[0]['position'], 0)
        self.assertEqual(self.state.scheduled_exits, [])
        self.assertEqual([p['quantity'] for e, p in self.exchange.calls if e == '/orders'], [8])

    def test_unfilled_exit_remainder_is_rescheduled(self):
        self.exchange.rows[0]['position'] = 8
        self.exchange.partial = 3
        self.state.scheduled_exits = [(390, 'CL-1F', 'SELL', 8, 'eia')]
        risk = bot.RiskManager(self.state, self.execution, self.storage, self.config)
        risk.process_scheduled_exits()
        self.assertEqual(self.state.scheduled_exits, [(391, 'CL-1F', 'SELL', 5, 'eia')])

    def test_open_orders_reserve_shared_limit_capacity(self):
        self.exchange.orders = [dict(ticker='CL-2F', action='BUY', quantity=95,
                                     quantity_filled=0, status='OPEN')]
        self.assertEqual(self.execution.approved_quantity('CL', 'BUY', 30), 5)

    def test_product_weights_and_gross_are_checked(self):
        rows = [security('CL-1F', -90), security('HO', 90, 5)]
        self.assertFalse(risk_allows(rows, self.exchange.limits, [], 'HO', 'BUY', 1))
        self.exchange.limits[0]['net_limit'] = 1000
        self.assertFalse(risk_allows(rows, self.exchange.limits, [], 'HO', 'BUY', 1))

    def test_expired_future_and_nonshortable_inventory(self):
        self.state.period = 2
        self.assertEqual(self.execution.approved_quantity('CL-1F', 'BUY', 10), 0)
        self.assertEqual(self.execution.approved_quantity('CL', 'SELL', 10), 0)

    def test_500_preserves_body_and_is_not_retried(self):
        response = Mock(status_code=500, ok=False, text='{"message":"backend conversion unavailable"}')
        with patch.object(bot.SESSION, 'request', return_value=response) as request:
            with self.assertRaisesRegex(TradingHalt, 'backend conversion unavailable'):
                bot._write('POST', '/leases', {'ticker': 'AK-CS-PIPE'})
        request.assert_called_once()
        self.assertTrue(bot.LAST_WRITE_FAILURE['uncertain'])

    def test_timeout_is_not_retried(self):
        with patch.object(bot.SESSION, 'request', side_effect=requests.Timeout) as request:
            with self.assertRaises(TradingHalt): bot._write('POST', '/orders', {})
        request.assert_called_once()

    def test_unavailable_asset_is_skipped(self):
        with patch.object(bot, '_get', return_value=[dict(ticker='AK-CS-PIPE', is_available=False, available_quantity=0)]):
            self.assertFalse(bot.asset_available('AK-CS-PIPE'))

    def test_delayed_inventory_does_not_resubmit_purchase(self):
        original = self.execution.refresh_positions
        stale = dict(self.state.positions)
        reads = 0
        def refresh():
            nonlocal reads
            reads += 1
            if reads == 2:
                self.state.positions = dict(stale)
                return [dict(r, position=stale[r['ticker']]) for r in self.exchange.rows]
            return original()
        with patch.object(self.execution, 'refresh_positions', side_effect=refresh), patch.object(bot.time, 'sleep'):
            orders = self.execution.place_split_orders('CL-1F', 'BUY', 10)
        self.assertEqual(bot.filled_quantity(orders), 10)
        self.assertEqual(sum(e == '/orders' for e, _ in self.exchange.calls), 1)
        self.assertGreaterEqual(reads, 3)

    def test_missing_fill_inventory_stops_further_entries(self):
        def post(endpoint, params=None):
            result = self.exchange.post(endpoint, params)
            if endpoint == '/orders': self.exchange.rows[0]['position'] = 0
            return result
        with patch.object(bot, '_post', side_effect=post), patch.object(bot.time, 'sleep'):
            with self.assertRaisesRegex(TradingHalt, 'inventory mismatch'):
                self.execution.place_split_orders('CL-1F', 'BUY', 100)
        self.assertEqual(sum(e == '/orders' for e, _ in self.exchange.calls), 1)

    def test_500_stops_main_loop_and_saves_read_only_evidence(self):
        def get(endpoint, params=None):
            if endpoint == '/case': return dict(status='ACTIVE', period=1, tick=270)
            if endpoint in ('/news', '/leases', '/assets'): return []
            return self.exchange.get(endpoint, params)
        response = Mock(status_code=500, ok=False, text='pipeline backend failed')
        transport = Mock()
        transport.evaluate.side_effect = lambda: bot._write('POST', '/leases', {'ticker': 'AK-CS-PIPE'})
        previous = os.getcwd()
        with tempfile.TemporaryDirectory() as directory:
            try:
                os.chdir(directory)
                with patch.object(bot, 'CaseConfig', return_value=self.config), patch.object(self.config, 'discover'), patch.object(bot, '_get', side_effect=get), patch.object(bot, 'TransportEngine', return_value=transport), patch.object(bot, 'BasisEngine'), patch.object(bot, 'CrackEngine'), patch.object(bot, 'RiskManager'), patch.object(bot, 'MarketMaker') as maker, patch.object(bot.SESSION, 'request', return_value=response) as request:
                    bot.main()
                    request.assert_called_once()
                    maker.return_value.update.assert_not_called()
                paths = list(Path(directory).glob('runs/*/failure.json'))
                self.assertEqual(len(paths), 1)
                data = json.loads(paths[0].read_text())
                self.assertEqual(data['last_write_failure']['response'], 'pipeline backend failed')
                self.assertEqual(data['limits'], self.exchange.limits)
                self.assertIn('open_orders', data)
            finally:
                os.chdir(previous)


if __name__ == '__main__':
    unittest.main()
