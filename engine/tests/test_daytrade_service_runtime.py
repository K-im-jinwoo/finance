"""Service scope and shutdown checks using injected transports only."""
import json
import os
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import MagicMock, patch

from stock_assistant.daytrade.service_runtime import ReadOnlyTransport, TelegramSender
from stock_assistant.daytrade.daemon import StickyAdapter


class RuntimeTests(unittest.TestCase):
    def test_candles_accounts_orders_and_other_paths_rejected_before_network(self):
        transport = ReadOnlyTransport(lambda: 'secret', threading.Event())
        for path in ('/api/v1/candles', '/api/v1/orders', '/api/v1/prices', '/oauth2/token'):
            with self.assertRaisesRegex(ValueError, 'PUBLIC_READ_SCOPE_REQUIRED'):
                transport(path, {})

    def test_failed_telegram_transport_returns_safe_metadata(self):
        class Opener:
            def open(self, *_args, **_kwargs): raise RuntimeError('secret-token')
        self.assertEqual(TelegramSender('secret-token', '123', opener=Opener())('paper fill'),
                         {'status': 'FAILED', 'http_status': None})

    def test_telegram_routes_each_axis_and_falls_back_with_its_header(self):
        class Opener:
            def open(self, request, **_kwargs):
                body = json.loads(request.data)
                bodies.append(body)
                response = MagicMock(status=200)
                response.__enter__.return_value = response
                response.read.return_value = json.dumps({
                    'ok': True, 'result': {'message_id': 123, 'chat': {'id': int(body['chat_id'])}},
                }).encode()
                return response

        bodies = []
        for axis, header in {'DAYTRADE': '[단타]', 'SWING': '[스윙]', 'LONGTERM': '[장기]'}.items():
            with self.subTest(axis=axis):
                route = {f'TELEGRAM_{axis}_CHAT_ID': ' -100123 ', f'TELEGRAM_{axis}_MESSAGE_THREAD_ID': '42'}
                for env, expected_chat, expected_thread, text in (
                    (route, '-100123', 42, '본문'),
                    ({f'TELEGRAM_{axis}_CHAT_ID': ' '}, '123', None, header + '\n본문'),
                ):
                    sender = TelegramSender('fixture', '123', bot_channel=axis, env=env, opener=Opener())
                    self.assertEqual(sender(text)['status'], 'SENT')
                    self.assertEqual(bodies[-1]['text'], header + '\n본문')
                    self.assertEqual(bodies[-1]['chat_id'], expected_chat)
                    self.assertEqual(bodies[-1].get('message_thread_id'), expected_thread)

    def test_longterm_cron_target_keeps_native_delivery_and_topic(self):
        from stock_assistant.telegram_routing import longterm_cron_target
        self.assertEqual(longterm_cron_target({}), 'telegram')
        self.assertEqual(longterm_cron_target({'TELEGRAM_LONGTERM_CHAT_ID': '-100123'}), 'telegram:-100123')
        self.assertEqual(longterm_cron_target({'TELEGRAM_HOME_CHANNEL': '-100456',
                                              'TELEGRAM_LONGTERM_MESSAGE_THREAD_ID': '42'}), 'telegram:-100456:42')
        with self.assertRaisesRegex(ValueError, '^TELEGRAM_MESSAGE_THREAD_ID_INVALID$'):
            longterm_cron_target({'TELEGRAM_LONGTERM_CHAT_ID': '-100123',
                                  'TELEGRAM_LONGTERM_MESSAGE_THREAD_ID': '42,telegram:456'})

    def test_sticky_subscription_pool_caps_eight_and_preserves_held_names(self):
        import random
        adapter = StickyAdapter(lambda: None, rng=random.Random(1))
        adapter.replace({f'trade:kr:{i:06d}' for i in range(12)}, now=1)
        self.assertEqual(len(adapter.desired), 16)
        adapter.held = ['999999']
        adapter.replace({'trade:kr:888888'}, now=2)
        self.assertIn('orderbook:kr:999999', adapter.desired)
        self.assertEqual(len(adapter.desired), 16)

    def test_systemd_assets_timer_and_restart_modes(self):
        root = Path(__file__).resolve().parents[1]/'deploy/daytrade'
        self.assertIn('Restart=on-failure', (root/'daytrade-paper.service').read_text())
        timer = (root/'daytrade-paper.timer').read_text()
        self.assertIn('08:55:00 Asia/Seoul', timer)
        self.assertIn('Persistent=true', timer)
        self.assertIn('Restart=always', (root/'daytrade-dashboard.service').read_text())


if __name__ == '__main__': unittest.main()
