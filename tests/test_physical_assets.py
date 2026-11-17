"""Physical-only regressions: conversion requests, delivered goods and lease cleanup."""
import contextlib
import io
import unittest
from unittest.mock import patch
import requests
import bot


class PhysicalTests(unittest.TestCase):
    def setUp(self):
        self.config = bot.CaseConfig()
        self.rows = {t: dict(ticker=t, position=0, last=price, bid=price, ask=price)
                     for t, price in [('CL', 100), ('CL-AK', 95), ('CL-NYC', 103), ('RB', 3), ('HO', 3)]}
        self.config.tickers = {t: dict(size=42000 if t in ('RB', 'HO') else 1000, shortable=False) for t in self.rows}
        self.config.assets = {t: dict(capacity=10000, transfer_sec=30, cost=cost, max_leases=100)
                              for t, cost in [('AK-STORAGE', 500), ('CL-STORAGE', 500), ('NYC-STORAGE', 500), ('AK-CS-PIPE', 40000), ('CS-NYC-PIPE', 20000), ('CL-REFINERY', 300000)]}
        self.config.assets['CL-REFINERY']['transfer_sec'] = 45
        self.state = bot.StateManager(self.config)
        self.state.period, self.state.tick = 1, 100
        self.state.update_from_securities(list(self.rows.values()))
        self.leases, self.calls, self.orders = {}, [], {}
        self.next_id = 100
        self.reject_use = False
        self.bad_ack = False
        self.fail_delete = set()
        self.fail_buys = False
        self.pending_position = None
        self.delay_sale = False
        self.execution = bot.ExecutionEngine(self.state, self.config)
        self.addCleanup(self.execution.executor.shutdown)
        self.storage = bot.StorageManager(self.state, self.config)
        self.transport = bot.TransportEngine(self.state, self.execution, self.storage, self.config)
        self.refinery = bot.CrackEngine(self.state, self.execution, self.storage, self.config)
        self.enterContext(patch.object(bot, '_get', side_effect=self.get))
        self.enterContext(patch.object(bot, '_post', side_effect=self.post))
        self.enterContext(patch.object(bot, '_delete', side_effect=self.delete))
        self.enterContext(patch.object(bot.time, 'sleep'))
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))

    def get(self, endpoint, params=None):
        if endpoint == '/securities': return [dict(row) for row in self.rows.values()]
        if endpoint == '/case': return dict(period=self.state.period, tick=self.state.tick)
        if endpoint == '/leases':
            usage = {t: row['position'] for t, row in self.rows.items()}
            result = []
            for lease in self.leases.values():
                row = dict(lease)
                ticker = {'AK-STORAGE': 'CL-AK', 'CL-STORAGE': 'CL', 'NYC-STORAGE': 'CL-NYC'}.get(row['ticker'])
                if ticker:
                    row['containment_usage'] = min(10, usage[ticker])
                    usage[ticker] -= row['containment_usage']
                result.append(row)
            return result
        if endpoint.startswith('/orders/'):
            return self.orders[int(endpoint.split('/')[-1])]
        raise AssertionError(endpoint)

    def post(self, endpoint, params=None):
        self.calls.append(('POST', endpoint, params))
        if endpoint == '/leases':
            self.next_id += 1
            lease = dict(id=self.next_id, ticker=params['ticker'], convert_finish_period=None, convert_finish_tick=None)
            self.leases[lease['id']] = lease
            if params['ticker'] in ('AK-CS-PIPE', 'CS-NYC-PIPE'):
                self.assertIn('from1', params)
                self.assertEqual(params['quantity1'], 10)
                self.assertGreaterEqual(self.rows[params['from1']]['position'], 10)
                self.rows[params['from1']]['position'] -= 10
                lease.update(convert_finish_period=1, convert_finish_tick=130)
            return dict(lease, id=99999 if self.bad_ack and params['ticker'] == 'CL-REFINERY' else lease['id'])
        if endpoint.startswith('/leases/'):
            lease_id = int(endpoint.split('/')[-1])
            self.assertEqual(self.state.refinery_lease_id, lease_id)  # tracked before use
            self.assertIn(lease_id, self.leases)
            if self.reject_use:
                response = requests.Response(); response.status_code = 400; response._content = b'input rejected'
                raise requests.HTTPError('input rejected', response=response)
            self.assertEqual(params, {'from1': 'CL', 'quantity1': 30})
            self.rows['CL']['position'] -= 30
            self.leases[lease_id].update(convert_finish_period=1, convert_finish_tick=145)
            return dict(self.leases[lease_id])
        if endpoint == '/orders':
            if params['action'] == 'BUY' and self.fail_buys:
                raise requests.HTTPError('purchase rejected')
            ticker, qty = params['ticker'], params['quantity']
            delta = qty if params['action'] == 'BUY' else -qty
            if self.delay_sale and params['action'] == 'SELL':
                self.pending_position = (ticker, delta)
            else:
                self.rows[ticker]['position'] += delta
            order = dict(params, order_id=len(self.orders)+1, quantity_filled=qty, status='TRANSACTED')
            self.orders[order['order_id']] = order
            return order
        raise AssertionError(endpoint)

    def delete(self, endpoint):
        self.calls.append(('DELETE', endpoint, None))
        lease_id = int(endpoint.split('/')[-1])
        if lease_id in self.fail_delete: raise requests.HTTPError('release failed')
        self.leases.pop(lease_id, None)
        return {'success': True}

    def start_ak(self):
        self.transport._start_pipeline('AK-CS-PIPE', 'CL-AK', 'AK', 'CL', 'CL', 10, buy_input=True)

    def start_refinery(self):
        self.rows['CL']['position'] = 30
        self.refinery.evaluate()
        return self.state.refinery_lease_id

    def test_pipeline_one_step_request_with_source_and_destination_storage(self):
        self.start_ak()
        pipeline_calls = [p for method, endpoint, p in self.calls if endpoint == '/leases' and p['ticker'] == 'AK-CS-PIPE']
        self.assertEqual(pipeline_calls, [{'ticker': 'AK-CS-PIPE', 'from1': 'CL-AK', 'quantity1': 10}])
        self.assertFalse(any(endpoint.startswith('/leases/') for method, endpoint, p in self.calls))
        self.assertEqual({lease['ticker'] for lease in self.leases.values()}, {'AK-STORAGE', 'CL-STORAGE', 'AK-CS-PIPE'})
        self.assertEqual(self.rows['CL-AK']['position'], 0)

    def test_no_pipeline_lease_when_input_purchase_fails(self):
        self.fail_buys = True
        self.start_ak()
        self.assertFalse(any(lease['ticker'] == 'AK-CS-PIPE' for lease in self.leases.values()))
        self.assertEqual(self.state.active_pipelines, [])
        self.assertEqual(self.leases, {})  # unused empty tanks cleaned

    def test_pipeline_completion_waits_for_delivery_then_sells_and_closes(self):
        self.start_ak()
        self.state.tick = 131
        self.transport.check_pipeline_completions()
        self.assertTrue(self.state.active_pipelines)
        self.rows['CL']['position'] = 10
        self.transport.check_pipeline_completions()
        self.assertEqual(self.rows['CL']['position'], 0)
        self.assertEqual(self.state.active_pipelines, [])
        self.assertEqual(self.leases, {})

    def test_auto_expired_pipeline_is_not_deleted_again(self):
        self.start_ak()
        lease_id = self.state.active_pipelines[0]['id']
        self.leases.pop(lease_id)
        self.rows['CL']['position'] = 10
        self.state.tick = 131
        self.transport.check_pipeline_completions()
        self.assertNotIn(('DELETE', f'/leases/{lease_id}', None), self.calls)
        self.assertEqual(self.state.active_pipelines, [])

    def test_refinery_uses_actual_new_lease_id_and_cleans_rejected_use(self):
        self.bad_ack = True
        self.reject_use = True
        self.start_refinery()
        self.assertTrue(any(endpoint == '/leases/101' for method, endpoint, p in self.calls))
        self.assertEqual(self.state.refinery_lease_id, None)
        self.assertEqual(self.leases, {})
        self.assertEqual(self.rows['CL']['position'], 30)

    def test_refinery_failed_release_remains_tracked_until_confirmed(self):
        lease_id = self.start_refinery()
        self.state.tick = 146
        self.rows['RB']['position'], self.rows['HO']['position'] = 20, 10
        self.fail_delete.add(lease_id)
        self.refinery.check_refinery_completion()
        self.assertEqual(self.state.refinery_lease_id, lease_id)
        self.assertEqual(self.rows['RB']['position'], 0)  # release failure must not block sales
        self.fail_delete.clear()
        self.refinery.check_refinery_completion()
        self.assertIsNone(self.state.refinery_lease_id)
        self.assertEqual(self.rows['RB']['position'], 0)
        self.assertEqual(self.rows['HO']['position'], 0)
        self.assertEqual(self.leases, {})

    def test_refinery_does_not_start_without_full_input_batch(self):
        self.rows['CL']['position'] = 29
        self.refinery.evaluate()
        self.assertEqual(self.calls, [])

    def test_storage_kept_for_incoming_delivery_and_occupied_tanks(self):
        self.start_ak()
        before = set(self.leases)
        self.storage.release_empty_storage()
        self.assertEqual(set(self.leases), before)
        self.state.active_pipelines.clear()
        self.leases = {101: dict(id=101, ticker='CL-STORAGE')}
        self.rows['CL']['position'] = 10
        self.storage.release_empty_storage()
        self.assertIn(101, self.leases)

    def test_delayed_sale_inventory_does_not_duplicate_sell(self):
        self.start_ak()
        self.rows['CL']['position'] = 10
        self.state.tick = 131
        self.delay_sale = True
        self.transport.check_pipeline_completions()
        self.transport.check_pipeline_completions()
        sells = [p for method, endpoint, p in self.calls if endpoint == '/orders' and p['action'] == 'SELL']
        self.assertEqual(len(sells), 1)
        ticker, delta = self.pending_position
        self.rows[ticker]['position'] += delta
        self.transport.check_pipeline_completions()
        self.assertEqual(self.state.active_pipelines, [])

    def test_two_shipments_reserve_capacity_and_close_independently(self):
        self.start_ak()
        self.state.tick += 1
        self.start_ak()
        ids = [job['id'] for job in self.state.active_pipelines]
        self.assertEqual(len(set(ids)), 2)
        self.assertEqual(sum(l['ticker'] == 'CL-STORAGE' for l in self.leases.values()), 2)
        self.state.tick = 131
        self.rows['CL']['position'] = 10
        self.transport.check_pipeline_completions()
        self.assertEqual([job['id'] for job in self.state.active_pipelines], ids[1:])
        self.state.tick += 1
        self.rows['CL']['position'] = 10
        self.transport.check_pipeline_completions()
        self.assertFalse(self.leases)
        self.assertEqual(self.rows['CL']['position'], 0)

    def test_pipeline_and_refinery_can_start_in_either_order(self):
        self.start_ak()
        refinery_id = self.start_refinery()
        self.assertIsNotNone(refinery_id)
        self.start_ak()
        self.assertEqual(len(self.state.active_pipelines), 2)
        self.assertIn(refinery_id, self.leases)

    def test_period_change_retains_conversion_tracking(self):
        self.start_ak()
        self.state._on_period_change(2)
        self.assertEqual(len(self.state.active_pipelines), 1)


if __name__ == '__main__':
    unittest.main()
