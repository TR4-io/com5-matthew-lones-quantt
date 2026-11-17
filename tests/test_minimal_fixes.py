"""Offline checks for the small API/batch corrections retained after rollback."""
import unittest
from unittest.mock import Mock, patch
import bot
import com5_template as template


class MinimalFixTests(unittest.TestCase):
    def test_cancel_all_supplies_required_selector(self):
        engine = bot.ExecutionEngine(Mock(), Mock())
        with patch.object(bot, '_post') as post:
            engine.cancel_all_orders()
            post.assert_called_once_with('/commands/cancel', params={'all': 1})
        with patch.object(template, '_post') as post:
            template.cancel_all()
            post.assert_called_once_with('/commands/cancel', params={'all': 1})

    def test_pipeline_helpers_supply_source_and_contract_quantity(self):
        for ticker, source in [('AK-CS-PIPE', 'CL-AK'), ('CS-NYC-PIPE', 'CL')]:
            with self.subTest(ticker=ticker), patch.object(template, 'lease_asset', return_value={'id': 7}), patch.object(template, '_post') as post:
                template.run_pipeline(ticker)
                post.assert_called_once_with('/leases/7', params={'from1': source, 'quantity1': 10})

    def test_refinery_helper_uses_full_case_batch(self):
        with patch.object(template, 'lease_asset', return_value={'id': 8}), patch.object(template, '_post') as post:
            template.run_refinery(wait=False)
            post.assert_called_once_with('/leases/8', params={'from1': 'CL', 'quantity1': 30})
        self.assertEqual((template.REFINERY_CL_IN, template.REFINERY_RB_OUT, template.REFINERY_HO_OUT), (30, 840000, 420000))

    def test_bot_refinery_requires_and_submits_thirty_contracts(self):
        state, config = Mock(), Mock()
        state.tick = 100
        state.refinery_lease_id = None
        state.get_price.side_effect = lambda ticker: (0, 0, {'CL': 100, 'RB': 3, 'HO': 3}[ticker])
        state.get_position.return_value = 30
        state.available_gross.return_value = 100
        state.strategy_gross = {'crack': 0}
        config.get_transfer_sec.return_value = 45
        config.ticks_per_period = 600
        config.get_contract_size.side_effect = lambda ticker: 1000 if ticker == 'CL' else 42000
        config.get_asset_cost.return_value = 300000
        config.fee_per_contract = 1
        config.crack_min_profit = 1000
        engine = bot.CrackEngine(state, Mock(), Mock(), config)
        with patch.object(bot, '_post', return_value={'id': 9}) as post, patch.object(bot, 'asset_available', return_value=True):
            state.get_position.return_value = 29
            engine.evaluate()
            post.assert_not_called()
            state.get_position.return_value = 30
            engine.evaluate()
            self.assertEqual(post.call_args.args, ('/leases/9',))
            self.assertEqual(post.call_args.kwargs['params'], {'from1': 'CL', 'quantity1': 30})
        self.assertEqual(state.strategy_gross['crack'], 30)


if __name__ == '__main__':
    unittest.main()
