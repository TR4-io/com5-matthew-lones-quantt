import unittest
from unittest.mock import Mock, patch
import requests
import bot


def response(status='ACTIVE', http=200):
    result = Mock(status_code=http, ok=http == 200, text='test server response')
    result.json.return_value = dict(status=status, period=1, tick=100)
    return result


class StartupTests(unittest.TestCase):
    def setUp(self):
        self.log = Mock()
        self.enterContext(patch.object(bot, 'API_KEY', 'test-key'))
        self.enterContext(patch.object(bot.time, 'sleep'))

    def test_missing_key_fails_before_connecting(self):
        with patch.object(bot, 'API_KEY', ''), patch.object(bot.SESSION, 'get') as get:
            with self.assertRaisesRegex(RuntimeError, 'API key is blank'):
                bot.wait_for_active(self.log)
            get.assert_not_called()

    def test_rejected_key_is_not_reported_as_inactive(self):
        with patch.object(bot.SESSION, 'get', return_value=response(http=401)) as get:
            with self.assertRaisesRegex(RuntimeError, 'rejected access'):
                bot.wait_for_active(self.log)
            get.assert_called_once()

    def test_connection_failures_stop_after_three_attempts(self):
        with patch.object(bot.SESSION, 'get', side_effect=requests.ConnectionError) as get:
            with self.assertRaisesRegex(RuntimeError, 'connection problem'):
                bot.wait_for_active(self.log)
            self.assertEqual(get.call_count, 3)
            self.assertEqual(get.call_args.kwargs['timeout'], (3, 5))

    def test_connected_paused_case_waits_then_starts_when_active(self):
        with patch.object(bot.SESSION, 'get', side_effect=[response('PAUSED'), response()]):
            self.assertEqual(bot.wait_for_active(self.log)['status'], 'ACTIVE')
        self.assertTrue(any('status=PAUSED' in call.args[0] for call in self.log.info.call_args_list))
        self.assertTrue(any('Starting live trading' in call.args[0] for call in self.log.info.call_args_list))

    def test_wrong_endpoint_is_not_silently_retried_forever(self):
        result = response()
        result.json.return_value = []
        with patch.object(bot.SESSION, 'get', return_value=result):
            with self.assertRaisesRegex(RuntimeError, 'no case status'):
                bot.wait_for_active(self.log)

    def test_server_error_reports_body_and_stops_after_three_attempts(self):
        with patch.object(bot.SESSION, 'get', return_value=response(http=500)) as get:
            with self.assertRaisesRegex(RuntimeError, 'failed three times'):
                bot.wait_for_active(self.log)
        self.assertEqual(get.call_count, 3)
        self.assertIn('test server response', self.log.error.call_args.args[0])


if __name__ == '__main__':
    unittest.main()
