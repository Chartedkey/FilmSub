from decimal import Decimal
import io
import json
from pathlib import Path
import queue
import tempfile
from types import SimpleNamespace
import unittest
import urllib.error
from unittest.mock import Mock, patch

from filmsub.balance import BalanceError, fetch_balance, parse_balance
from filmsub.config import Settings, load_settings, save_settings
from filmsub.gui import App


class BalanceApiTests(unittest.TestCase):
    def test_deepseek_uses_total_and_keeps_currencies(self):
        result = parse_balance('DeepSeek', {'balance_infos': [
            {'currency': 'USD', 'total_balance': '7.314', 'topped_up_balance': '6.00'},
            {'currency': 'CNY', 'total_balance': '0.00'}]})
        self.assertEqual(result.amounts[0][1], Decimal('7.314'))
        self.assertEqual(result.label(), '7.31 USD / 0.00 CNY')

    def test_openrouter_subtracts_usage_exactly(self):
        result = parse_balance('OpenRouter', {'data': {'total_credits': '30.00', 'total_usage': '4.25'}})
        self.assertEqual(result.label(), '25.75 USD')
        self.assertEqual(parse_balance('OpenRouter', {'data': {'total_credits': 0, 'total_usage': 0}}).label(), '0.00 USD')

    def test_bad_data_is_not_a_zero_balance(self):
        for payload in ({}, {'data': {}}, {'data': {'total_credits': True, 'total_usage': 0}},
                        {'data': {'total_credits': 'NaN', 'total_usage': 0}},
                        {'data': {'total_credits': 'Infinity', 'total_usage': 0}}, []):
            with self.subTest(payload=payload), self.assertRaises(BalanceError):
                parse_balance('OpenRouter', payload)
        for rows in ([], None, [{'currency': 'XXX', 'total_balance': '1'}]):
            with self.assertRaises(BalanceError):
                parse_balance('DeepSeek', {'balance_infos': rows})

    def test_request_is_get_and_goes_to_the_matching_service(self):
        examples = [
            ('DeepSeek', 'ds-test-key', 'https://api.deepseek.com/user/balance',
             {'balance_infos': [{'currency': 'USD', 'total_balance': '7.31'}]}),
            ('OpenRouter', 'or-management-test-key', 'https://openrouter.ai/api/v1/credits',
             {'data': {'total_credits': 20, 'total_usage': 7.7}})]
        for provider, key, url, payload in examples:
            opener = Mock()
            opener.open.return_value.__enter__ = Mock(return_value=io.BytesIO(json.dumps(payload).encode()))
            opener.open.return_value.__exit__ = Mock(return_value=False)
            with patch('filmsub.balance.urllib.request.build_opener', return_value=opener):
                fetch_balance(provider, key, timeout=3)
            request = opener.open.call_args.args[0]
            self.assertEqual(request.full_url, url)
            self.assertEqual(request.get_method(), 'GET')
            self.assertEqual(request.get_header('Authorization'), f'Bearer {key}')
            self.assertIsNone(request.data)
            self.assertEqual(opener.open.call_args.kwargs['timeout'], 3)

    def test_http_errors_never_expose_server_body_or_key(self):
        for code, fragment in ((401, '401'), (403, 'management key'), (429, '429'), (500, '500')):
            error = urllib.error.HTTPError('https://openrouter.ai', code, 'private-key-value', {},
                                           io.BytesIO(b'private-key-value'))
            opener = Mock()
            opener.open.side_effect = error
            with patch('filmsub.balance.urllib.request.build_opener', return_value=opener):
                with self.assertRaises(BalanceError) as caught:
                    fetch_balance('OpenRouter', 'private-key-value')
            self.assertIn(fragment, str(caught.exception))
            self.assertNotIn('private-key-value', str(caught.exception))

    def test_timeout_and_network_error_are_non_fatal(self):
        for error in (TimeoutError('private-key-value'), urllib.error.URLError('private-key-value')):
            opener = Mock()
            opener.open.side_effect = error
            with patch('filmsub.balance.urllib.request.build_opener', return_value=opener), self.assertRaises(BalanceError) as caught:
                fetch_balance('DeepSeek', 'private-key-value')
            self.assertNotIn('private-key-value', str(caught.exception))

    def test_missing_key_makes_no_request(self):
        with patch('filmsub.balance.urllib.request.build_opener') as build:
            with self.assertRaises(BalanceError):
                fetch_balance('DeepSeek', ' ')
        build.assert_not_called()

    def test_management_key_is_separate_and_old_settings_load(self):
        with tempfile.TemporaryDirectory() as tmp, patch('filmsub.config.app_dir', return_value=tmp):
            path = Path(tmp) / 'settings.json'
            path.write_text(json.dumps({'keys': {'DeepSeek': 'ds', 'OpenRouter': 'translation'}}))
            settings = load_settings()
            self.assertEqual(settings.openrouter_management_key, '')
            settings.openrouter_management_key = 'management'
            save_settings(settings)
            restored = load_settings()
            self.assertEqual(restored.openrouter_management_key, 'management')
            self.assertEqual(restored.keys['OpenRouter'], 'translation')


class BalanceUiTests(unittest.TestCase):
    def app(self, ds='ds-test', management='management-test'):
        app = SimpleNamespace(s=Settings(keys={'DeepSeek': ds, 'OpenRouter': 'translation-only'}),
                              v_provider=Mock(), v_key=Mock(), v_rkey=Mock(), v_management_key=Mock(),
                              balance_labels={'DeepSeek': Mock(), 'OpenRouter': Mock()}, balance_btn=Mock(),
                              _balance_busy=False, _balance_pending=False, _balance_request_id=0,
                              _balance_credentials=None, collect_settings=Mock(), q=queue.Queue())
        app.v_provider.get.return_value = 'DeepSeek'
        app.v_key.get.return_value = ds
        app.v_rkey.get.return_value = 'translation-only'
        app.v_management_key.get.return_value = management
        app._balance_keys = lambda: App._balance_keys(app)
        app._fetch_balance = Mock()
        app.refresh_balances = lambda save=True: App.refresh_balances(app, save)
        return app

    def test_regular_openrouter_key_never_used_for_credits(self):
        app = self.app(ds='', management='')
        with patch('filmsub.gui.threading.Thread') as thread:
            app.refresh_balances(save=False)
        thread.assert_not_called()
        self.assertIn('management key', app.balance_labels['OpenRouter'].configure.call_args.kwargs['text'])
        self.assertFalse(app._balance_busy)

    def test_requests_run_in_background_and_overlap_is_coalesced(self):
        app = self.app()
        with patch('filmsub.gui.threading.Thread') as thread:
            app.refresh_balances(save=False)
            app.refresh_balances(save=False)
        self.assertEqual(thread.call_count, 2)
        self.assertTrue(app._balance_pending)
        self.assertEqual(thread.call_args_list[1].kwargs['args'][2], 'management-test')
        self.assertNotIn('translation-only', str(thread.call_args_list))

    def test_response_for_old_credentials_is_discarded(self):
        app = self.app()
        with patch('filmsub.gui.threading.Thread'):
            app.refresh_balances(save=False)
        app.v_management_key.get.return_value = 'new-management'
        app.balance_labels['OpenRouter'].reset_mock()
        App._show_balance(app, 1, 'OpenRouter', '999.00 USD')
        app.balance_labels['OpenRouter'].configure.assert_not_called()

    def test_stale_request_id_is_ignored(self):
        app = self.app()
        app._balance_request_id = 2
        App._show_balance(app, 1, 'DeepSeek', '999.00 USD')
        app.balance_labels['DeepSeek'].configure.assert_not_called()

    def test_failure_goes_to_queue_and_does_not_stop_translator(self):
        app = self.app()
        with patch('filmsub.gui.fetch_balance', side_effect=BalanceError('Нет связи')):
            App._fetch_balance(app, 1, 'DeepSeek', 'ds-test')
        self.assertEqual(app.q.get_nowait(), ('balance', 1, 'DeepSeek', 'баланс недоступен — Нет связи'))


if __name__ == '__main__':
    unittest.main()
