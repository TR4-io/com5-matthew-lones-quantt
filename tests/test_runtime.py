import contextlib
import io
import unittest
from unittest.mock import Mock, patch
import requests
import bot
from runtime import Client, TradingHalt, risk_allows, conversion_spec


def security(ticker, position=0, weight=1, short=True, size=1000):
    return dict(ticker=ticker, position=position, size=size, type='FUTURE' if short else 'SPOT',
                is_tradeable=True, is_shortable=short, max_trade_size=30, trading_fees=1,
                limits=[dict(name='COM5', units=weight)], bid=74.9, ask=75.1, last=75)


LIMITS = [dict(name='COM5', gross_limit=500, net_limit=100)]


class Exchange:
    """Deterministic REST fixture; fills market orders and leaves limit orders open."""
    def __init__(self, positions=None):
        self.rows = [security('CL', short=False), security('CL-1F'), security('CL-2F')]
        for r in self.rows:
            r['position'] = (positions or {}).get(r['ticker'], 0)
        self.orders = []
        self.leases = []
        self.writes = []
        self.case = dict(status='ACTIVE', period=1, tick=100, ticks_per_period=600, total_periods=2)
        self.oid = 0

    def request(self, method, endpoint, params=None):
        params = params or {}
        if method == 'GET':
            if endpoint == '/securities': return [dict(r) for r in self.rows]
            if endpoint == '/limits': return LIMITS
            if endpoint == '/orders': return [o for o in self.orders if o['status'] == 'OPEN']
            if endpoint == '/leases': return self.leases
            if endpoint == '/case': return self.case
            if endpoint == '/trader': return {'nlv': 12345}
            if endpoint == '/assets': return []
            if endpoint == '/news': return []
            if endpoint.startswith('/orders/'):
                return next(o for o in self.orders if o['order_id'] == int(endpoint.split('/')[-1]))
        self.writes.append((method, endpoint, params))
        if method == 'POST' and endpoint == '/orders':
            self.oid += 1
            market = params['type'] == 'MARKET'
            order = dict(params, order_id=self.oid, quantity_filled=params['quantity'] if market else 0,
                         status='TRANSACTED' if market else 'OPEN')
            self.orders.append(order)
            if market:
                row = next(r for r in self.rows if r['ticker'] == params['ticker'])
                row['position'] += params['quantity'] * (1 if params['action'] == 'BUY' else -1)
            return dict(order)
        if method == 'POST' and endpoint == '/commands/cancel':
            assert params == {'all': 1}
            for o in self.orders:
                if o['status'] == 'OPEN': o['status'] = 'CANCELLED'
            return {'success': True}
        if method == 'DELETE' and endpoint.startswith('/leases/'):
            self.leases = [l for l in self.leases if l['id'] != int(endpoint.split('/')[-1])]
            return {'success': True}
        raise AssertionError((method, endpoint, params))


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.exchange = Exchange()
        self.client_patch = patch.object(bot, 'CLIENT', self.exchange)
        self.client_patch.start()
        self.addCleanup(self.client_patch.stop)
        self.output = contextlib.redirect_stdout(io.StringIO())
        self.output.__enter__()
        self.addCleanup(self.output.__exit__, None, None, None)
        self.config = bot.CaseConfig()
        self.config.physical_enabled = False
        self.config.tickers = {r['ticker']: dict(size=r['size'], shortable=r['is_shortable']) for r in self.exchange.rows}
        self.state = bot.StateManager(self.config)
        self.state.update_from_case(self.exchange.case)
        self.state.update_from_securities(self.exchange.rows)
        self.execution = bot.ExecutionEngine(self.state, self.config)
        self.storage = bot.StorageManager(self.state, self.config)

    def test_read_only_transport_never_sends_write(self):
        session = Mock()
        c = Client('http://localhost:9999/v1', {}, session=session)
        with self.assertRaises(TradingHalt): c.request('POST', '/orders')
        session.request.assert_not_called()

    def test_uncertain_write_is_not_retried(self):
        session = Mock()
        session.request.side_effect = requests.Timeout('lost acknowledgement')
        c = Client('http://localhost:9999/v1', {}, live=True, session=session)
        with self.assertRaises(TradingHalt): c.request('POST', '/orders')
        self.assertTrue(c.uncertain_write)
        self.assertEqual(session.request.call_count, 1)
        self.assertEqual(session.request.call_args.kwargs['timeout'], (3, 5))

    def test_server_error_write_is_not_retried(self):
        session = Mock()
        session.request.return_value.status_code = 503
        c = Client('http://localhost:9999/v1', {}, live=True, session=session)
        with self.assertRaises(TradingHalt): c.request('POST', '/leases')
        self.assertTrue(c.uncertain_write)
        self.assertEqual(session.request.call_count, 1)

    def test_conversion_rejection_preserves_server_reason_without_retry(self):
        session = Mock()
        response = session.request.return_value
        response.status_code = 400
        response.ok = False
        response.text = '{"message": "Insufficient input quantity"}'
        c = Client('http://localhost:9999/v1', {}, live=True, session=session)
        with self.assertRaisesRegex(TradingHalt, 'Insufficient input quantity'):
            c.request('POST', '/leases/16', {'from1': 'CL', 'quantity1': 30})
        self.assertEqual(session.request.call_count, 1)
        self.assertFalse(c.uncertain_write)

    def test_rate_limit_retries_are_bounded(self):
        session = Mock()
        session.request.return_value.status_code = 429
        session.request.return_value.headers = {'Retry-After': '.01'}
        session.request.return_value.json.return_value = {'wait': .01}
        c = Client('http://localhost:9999/v1', {}, live=True, session=session)
        with patch('runtime.time.sleep'), self.assertRaises(TradingHalt): c.request('POST', '/orders')
        self.assertEqual(session.request.call_count, 4)

    def test_global_net_is_not_a_per_ticker_limit(self):
        rows = [security('CL-1F', 80), security('CL-2F')]
        self.assertFalse(risk_allows(rows, LIMITS, [], 'CL-2F', 'BUY', 30))
        self.assertTrue(risk_allows(rows, LIMITS, [], 'CL-2F', 'BUY', 20))

    def test_product_weight_counts_toward_gross(self):
        rows = [security('CL-1F', -90), security('RB', 90, weight=5, short=False)]
        self.assertFalse(risk_allows(rows, LIMITS, [], 'RB', 'BUY', 1))

    def test_open_orders_reserve_risk(self):
        orders = [dict(ticker='CL-2F', action='BUY', quantity=30, quantity_filled=5)]
        self.assertFalse(risk_allows(self.exchange.rows, LIMITS, orders, 'CL-1F', 'BUY', 80))

    def test_exit_from_breached_position_can_reduce_risk(self):
        self.assertTrue(risk_allows([security('CL-1F', 120)], LIMITS, [], 'CL-1F', 'SELL', 10))

    def test_no_short_spot(self):
        self.assertFalse(risk_allows(self.exchange.rows, LIMITS, [], 'CL', 'SELL', 1))

    def test_missing_weights_fail_closed(self):
        rows = [security('CL-1F')]; rows[0]['limits'] = []
        with self.assertRaises(ValueError): risk_allows(rows, LIMITS, [], 'CL-1F', 'BUY', 1)

    def test_split_orders_stop_after_partial_fill(self):
        self.execution.place_order = Mock(return_value=dict(quantity=30, quantity_filled=7))
        orders = self.execution.place_split_orders('CL-1F', 'BUY', 60)
        self.assertEqual(bot.filled(orders), 7)
        self.assertEqual(self.execution.place_order.call_count, 1)

    def test_split_orders_stop_after_rejection(self):
        self.execution.place_order = Mock(return_value=None)
        self.assertEqual(self.execution.place_split_orders('CL-1F', 'BUY', 60), [])
        self.assertEqual(self.execution.place_order.call_count, 1)

    def test_split_orders_honor_per_security_cap_and_refresh(self):
        self.exchange.rows[1]['max_trade_size'] = 4
        self.assertEqual(bot.filled(self.execution.place_split_orders('CL-1F', 'BUY', 10)), 10)
        self.assertEqual([o['quantity'] for o in self.exchange.orders], [4, 4, 2])
        self.assertEqual(self.state.get_position('CL-1F'), 10)

    def test_reduce_only_cannot_open_short(self):
        self.exchange.rows[1]['position'] = 3
        orders = self.execution.place_split_orders('CL-1F', 'SELL', 10, reduce_only=True)
        self.assertEqual(bot.filled(orders), 3)
        self.assertEqual(self.state.get_position('CL-1F'), 0)

    def test_expired_first_future_is_not_traded(self):
        self.state.period = 2
        self.assertIsNone(self.execution.place_order('CL-1F', 'BUY', 1))
        self.assertEqual(self.exchange.writes, [])

    def test_scheduled_exit_does_not_reverse_existing_position(self):
        self.exchange.rows[1]['position'] = 2
        self.state.scheduled_exits = [(99, 'CL-1F', 'SELL', 10, 'eia')]
        bot.RiskManager(self.state, self.execution, self.storage, self.config).process_scheduled_exits()
        self.assertEqual(self.state.get_position('CL-1F'), 0)
        self.assertEqual(self.state.scheduled_exits, [])

    def test_scheduled_exit_runs_only_when_due(self):
        self.exchange.rows[1]['position'] = 5
        self.state.scheduled_exits = [(110, 'CL-1F', 'SELL', 5, 'eia')]
        manager = bot.RiskManager(self.state, self.execution, self.storage, self.config)
        manager.process_scheduled_exits()
        self.assertEqual(self.exchange.writes, [])
        self.state.tick = 110
        manager.process_scheduled_exits()
        self.assertEqual(self.state.get_position('CL-1F'), 0)

    def test_eia_schedules_confirmed_fills_in_period_two(self):
        self.state.period = 2
        self.execution.place_split_orders = Mock(return_value=[dict(quantity_filled=5)])
        engine = bot.EIAEngine(self.state, self.execution, self.storage, self.config)
        engine.process(dict(news_id=1, headline='WEEK 3 CL ACTUAL DRAW 2 MLN BBLS VS FORECAST BUILD 10 MLN BBLS', body=''))
        self.assertEqual(self.execution.place_split_orders.call_count, 1)
        self.assertEqual(self.execution.place_split_orders.call_args.args[0], 'CL-2F')
        self.assertEqual(sum(e[3] for e in self.state.scheduled_exits), 5)
        self.assertEqual([e[0] for e in self.state.scheduled_exits], [110, 130, 160])

    def test_clean_account_refuses_existing_exposure_without_writes(self):
        self.exchange.rows[1]['position'] = 1
        with self.assertRaises(TradingHalt): bot.ensure_clean_account()
        self.assertEqual(self.exchange.writes, [])

    def test_shutdown_cancels_flattens_and_reconciles(self):
        self.exchange.rows[1]['position'] = 8
        self.exchange.orders = [dict(order_id=1, ticker='CL-2F', action='BUY', quantity=2, quantity_filled=0, status='OPEN')]
        transport = bot.TransportEngine(self.state, self.execution, self.storage, self.config)
        crack = bot.CrackEngine(self.state, self.execution, self.storage, self.config)
        result = bot.shutdown(self.state, self.execution, self.storage, transport, crack)
        self.assertTrue(result['flat'])
        self.assertIn(('POST', '/commands/cancel', {'all': 1}), self.exchange.writes)

    def test_shutdown_failure_does_not_claim_flat(self):
        self.execution.cancel_all_orders = Mock(side_effect=TradingHalt('uncertain cancellation'))
        self.exchange.rows[1]['position'] = 5
        transport = bot.TransportEngine(self.state, self.execution, self.storage, self.config)
        crack = bot.CrackEngine(self.state, self.execution, self.storage, self.config)
        result = bot.shutdown(self.state, self.execution, self.storage, transport, crack)
        self.assertFalse(result['flat'])
        self.assertEqual(result['positions']['CL-1F'], 5)
        self.assertTrue(result['errors'])

    def test_default_cli_is_read_only(self):
        with patch.object(bot, 'API_KEY', 'test'), patch.object(bot, 'Client', return_value=self.exchange):
            bot.main(['--futures-only'])
        self.assertEqual(self.exchange.writes, [])

    def test_asset_spec_uses_contract_quantities(self):
        asset = dict(ticker='CL-REFINERY', convert_from=[dict(ticker='CL', quantity=30)],
                     convert_to=[dict(ticker='RB', quantity=20), dict(ticker='HO', quantity=10)], ticks_per_conversion=45)
        self.assertEqual(conversion_spec(asset, 'CL', {'RB': 20, 'HO': 10}), {'from1': 'CL', 'quantity1': 30})
        asset['convert_from'][0]['quantity'] = 30000
        with self.assertRaises(ValueError): conversion_spec(asset, 'CL', {'RB': 20, 'HO': 10})

    def test_pipeline_spec(self):
        asset = dict(ticker='AK-CS-PIPE', convert_from=[dict(ticker='CL-AK', quantity=10)],
                     convert_to=[dict(ticker='CL', quantity=10)], ticks_per_conversion=30)
        self.assertEqual(conversion_spec(asset, 'CL-AK', {'CL': 10}), {'from1': 'CL-AK', 'quantity1': 10})

    def test_period_change_retains_conversion_tracking(self):
        self.state.active_pipelines = [23]
        self.state.refinery_lease_id = 42
        self.state.update_from_case(dict(period=2, tick=1, status='ACTIVE'))
        self.assertEqual(self.state.active_pipelines, [23])
        self.assertEqual(self.state.refinery_lease_id, 42)

    def test_storage_not_released_during_conversion(self):
        self.exchange.leases = [dict(id=10, ticker='CL-STORAGE', containment_usage=0)]
        self.state.active_pipelines = [23]
        self.storage.release_empty_storage()
        self.assertEqual(self.exchange.writes, [])

    def test_completion_needs_arrived_inventory_not_just_time(self):
        engine = bot.TransportEngine(self.state, self.execution, self.storage, self.config)
        engine.jobs = [dict(id=23, ticker='AK-CS-PIPE', due=50, confirmed=True, released=False, outputs={'CL': 10})]
        self.state.active_pipelines = [23]
        engine.complete()
        self.assertEqual(self.exchange.writes, [])
        self.assertEqual(len(engine.jobs), 1)

    def test_physical_conversion_full_cycle(self):
        self.exchange.rows += [security('RB', short=False, size=42000), security('HO', short=False, size=42000)]
        self.exchange.rows[0]['position'] = 30
        self.config.tickers.update({t: dict(size=42000, shortable=False) for t in ('RB', 'HO')})
        assets = [dict(ticker='CL-STORAGE', lease_price=500, containment=dict(ticker='CL', quantity=10), total_quantity=10),
                  dict(ticker='CL-REFINERY', lease_price=300000, total_quantity=1, ticks_per_conversion=45,
                       convert_from=[dict(ticker='CL', quantity=30)],
                       convert_to=[dict(ticker='RB', quantity=20), dict(ticker='HO', quantity=10)])]
        self.config.load_assets(assets)
        original = self.exchange.request
        converted = []
        def request(method, endpoint, params=None):
            if method == 'GET' and endpoint == '/assets': return assets
            if method == 'POST' and endpoint == '/leases':
                lease = dict(id=len(self.exchange.leases)+1, ticker=params['ticker'], containment_usage=0)
                self.exchange.leases.append(lease)
                return dict(lease)
            if method == 'POST' and endpoint.startswith('/leases/'):
                converted.append(params)
                self.exchange.rows[0]['position'] = 0
                return {'success': True}
            return original(method, endpoint, params)
        self.exchange.request = request
        engine = bot.CrackEngine(self.state, self.execution, self.storage, self.config)
        self.assertTrue(engine.start_conversion('CL-REFINERY', 'CL', {'RB': 20, 'HO': 10}))
        self.assertEqual(converted, [{'from1': 'CL', 'quantity1': 30}])
        self.assertEqual(len(engine.jobs), 1)
        self.state.tick = 146
        for row in self.exchange.rows:
            if row['ticker'] == 'RB': row['position'] = 20
            if row['ticker'] == 'HO': row['position'] = 10
        engine.complete()
        self.assertEqual(engine.jobs, [])
        self.assertFalse(any(r['position'] for r in self.exchange.rows))
        self.assertEqual(self.exchange.leases, [])
        self.assertEqual(self.state.conversion_outputs, set())

    def test_ctrl_c_live_loop_runs_cleanup_and_saves_summary(self):
        import json
        import os
        import tempfile
        from pathlib import Path
        original = self.exchange.request
        case_calls = 0
        def request(method, endpoint, params=None):
            nonlocal case_calls
            if method == 'GET' and endpoint == '/case':
                case_calls += 1
                if case_calls == 5: raise KeyboardInterrupt()
            if method == 'GET' and endpoint == '/news' and case_calls >= 4:
                return [dict(news_id=1, headline='WEEK 3 CL ACTUAL DRAW 2 MLN BBLS VS FORECAST BUILD 10 MLN BBLS', body='')]
            return original(method, endpoint, params)
        self.exchange.request = request
        previous = os.getcwd()
        with tempfile.TemporaryDirectory() as folder:
            try:
                os.chdir(folder)
                self.assertEqual(bot.run_live(self.config), 0)
                summary = next(Path('runs').glob('*/summary.json'))
                self.assertFalse(any(json.loads(summary.read_text())['final_positions'].values()))
                events = next(Path('runs').glob('*/events.jsonl')).read_text()
                self.assertIn('"flat": true', events)
                self.assertTrue(any(w[1] == '/orders' for w in self.exchange.writes))
            finally:
                os.chdir(previous)

    def test_uncertain_write_prevents_clean_shutdown_claim(self):
        self.exchange.uncertain_write = True
        transport = bot.TransportEngine(self.state, self.execution, self.storage, self.config)
        crack = bot.CrackEngine(self.state, self.execution, self.storage, self.config)
        report = bot.shutdown(self.state, self.execution, self.storage, transport, crack)
        self.assertFalse(report['flat'])
        self.assertTrue(report['uncertain_write'])

    def test_partial_scheduled_exit_keeps_unfilled_remainder(self):
        self.exchange.rows[1]['position'] = 10
        self.state.scheduled_exits = [(90, 'CL-1F', 'SELL', 10, 'eia')]
        self.execution.place_split_orders = Mock(return_value=[dict(quantity_filled=3)])
        bot.RiskManager(self.state, self.execution, self.storage, self.config).process_scheduled_exits()
        self.assertEqual(self.state.scheduled_exits, [(101, 'CL-1F', 'SELL', 7, 'eia')])

    def test_outstanding_conversion_outputs_are_not_flattened_early(self):
        self.exchange.rows[0]['position'] = 10
        self.state.conversion_outputs = {'CL'}
        manager = bot.RiskManager(self.state, self.execution, self.storage, self.config)
        manager._emergency_flatten()
        self.assertFalse(self.exchange.writes)

    def test_basis_second_leg_matches_fills_and_unwinds_excess(self):
        self.execution.place_split_orders = Mock(side_effect=[
            [dict(quantity_filled=7)], [dict(quantity_filled=3)], [dict(quantity_filled=4)]])
        basis = bot.BasisEngine(self.state, self.execution, self.storage, self.config)
        basis._trade_pair('CL', 'BUY', 'CL-1F', 'SELL', 10)
        calls = self.execution.place_split_orders.call_args_list
        self.assertEqual(calls[1].args, ('CL-1F', 'SELL', 7))
        self.assertEqual(calls[2].args, ('CL', 'SELL', 4))
        self.assertEqual(self.state.strategy_gross['basis'], 6)

    def test_blocked_spot_entry_does_not_lease_storage(self):
        self.config.physical_enabled = True
        self.storage.ensure_storage = Mock(side_effect=AssertionError('Should not lease for rejected order'))
        engine = bot.EIAEngine(self.state, self.execution, self.storage, self.config)
        engine.process(dict(news_id=1, headline='WEEK 3 CL ACTUAL DRAW 2 MLN BBLS VS FORECAST BUILD 10 MLN BBLS', body=''))
        self.storage.ensure_storage.assert_not_called()
        self.assertEqual(sum(r['position'] for r in self.exchange.rows), 100)

    def test_market_order_timeout_cancel_is_reconciled(self):
        initial = dict(order_id=8, quantity=10, quantity_filled=0, status='OPEN')
        terminal = dict(initial, quantity_filled=4, status='CANCELLED')
        with patch.object(bot, '_get', side_effect=[initial]*6+[terminal]), patch.object(bot, '_delete') as cancel, patch.object(bot.time, 'sleep'):
            result = self.execution.finish_order(initial)
        self.assertEqual(result['quantity_filled'], 4)
        cancel.assert_called_once_with('/orders/8')

    def test_delayed_inventory_after_fill_waits_without_duplicate_purchase(self):
        original = self.exchange.request
        stale = [dict(r) for r in self.exchange.rows]
        remaining = 0
        def request(method, endpoint, params=None):
            nonlocal remaining
            if method == 'GET' and endpoint == '/securities' and remaining:
                remaining -= 1
                return [dict(r) for r in stale]
            result = original(method, endpoint, params)
            if method == 'POST' and endpoint == '/orders': remaining = 3
            return result
        self.exchange.request = request
        with patch.object(bot.time, 'sleep'):
            orders = self.execution.place_split_orders('CL', 'BUY', 30)
        self.assertEqual(bot.filled(orders), 30)
        self.assertEqual(self.state.get_position('CL'), 30)
        self.assertEqual(sum(w[1] == '/orders' for w in self.exchange.writes), 1)
        self.assertIsNone(self.execution.position_mismatch)

    def test_persistent_inventory_mismatch_halts_without_resubmitting(self):
        order = dict(order_id=9, ticker='CL', action='BUY', quantity_filled=30)
        with patch.object(bot.time, 'sleep'), self.assertRaisesRegex(TradingHalt, 'expected 30.0, observed 0.0'):
            self.execution.confirm_inventory(order, 0)
        self.assertEqual(self.execution.position_mismatch['order_id'], 9)
        self.assertEqual(self.exchange.writes, [])


if __name__ == '__main__': unittest.main()
