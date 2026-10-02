import json
import sys
import unittest
from datetime import datetime
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'vendor/stock-assistant/src')]
from stock_assistant.validation import assert_point_in_time, parse_krx_ohlcv_rows

class ObservedSampleTests(unittest.TestCase):
    def setUp(self):
        self.path=ROOT/'datasets/observed-provider-sample.json'
        if not self.path.exists():
            self.skipTest('bounded provider sample not present')
        self.data=json.loads(self.path.read_text(encoding='utf-8-sig'))

    def test_public_price_sample_uses_real_observation_time(self):
        observed=datetime.fromisoformat(self.data['observed_at'])
        rows=self.data['price_rows']
        self.assertEqual(len(rows),9)
        bars=parse_krx_ohlcv_rows(rows,observed_at=observed)
        self.assertEqual(len(bars),9)
        self.assertTrue(all(b.observed_at==observed for b in bars))
        self.assertEqual(len({(b.symbol,b.trade_date) for b in bars}),9)
        self.assertFalse(self.data['three_year_certified'])

    def test_historical_trade_date_is_not_historical_availability(self):
        observed=datetime.fromisoformat(self.data['observed_at'])
        with self.assertRaises(ValueError):
            assert_point_in_time(datetime.fromisoformat('2023-11-01T20:00:00+09:00'),observed_at=observed,label='retrieved historical price')
        self.assertFalse(self.data['dart']['historical_version_certified'])

