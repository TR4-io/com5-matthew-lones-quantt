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
            with self.subTest(ticker=ticker), patch.object(template, '_post', return_value={'id': 7}) as post:
                self.assertEqual(template.run_pipeline(ticker), 7)
                post.assert_called_once_with('/leases', params={'ticker': ticker, 'from1': source, 'quantity1': 10})

    def test_refinery_helper_uses_full_case_batch(self):
        with patch.object(template, 'lease_asset', return_value={'id': 8}), patch.object(template, '_post') as post:
            template.run_refinery(wait=False)
            post.assert_called_once_with('/leases/8', params={'from1': 'CL', 'quantity1': 30})
        self.assertEqual((template.REFINERY_CL_IN, template.REFINERY_RB_OUT, template.REFINERY_HO_OUT), (30, 840000, 420000))



if __name__ == '__main__':
    unittest.main()
