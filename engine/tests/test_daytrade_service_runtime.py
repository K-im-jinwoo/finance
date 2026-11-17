"""Service scope and shutdown checks using injected transports only."""
import os
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

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
