import unittest
from stock_assistant.daytrade.risk import RiskState, Phase


class RiskTests(unittest.TestCase):
    def test_time_and_independent_locks_do_not_overwrite_one_another(self):
        r = RiskState()
        r.advance("2026-11-13T09:00:00+09:00", opening_equity="10000000")
        r.calendar_verified = True
        self.assertTrue(r.candidate_entry_allowed)
        r.lock("RATE_LIMIT_DEGRADED", "MARKET_DATA_CHART")
        r.mark("9700000")
        r.lock("DATA_DEGRADED", "QUOTE_STALE")
        locks = r.locks
        for at, phase in [("14:50:00", Phase.ENTRY_CLOSED), ("15:15:00", Phase.TIME_EXITING), ("15:30:00", Phase.CLOSED)]:
            r.advance("2026-11-13T" + at + "+09:00", has_positions=True)
            self.assertEqual(r.phase, phase)
            self.assertEqual(r.locks, locks)
            self.assertFalse(r.candidate_entry_allowed)
            self.assertTrue(r.exit_monitor_required)

    def test_threshold_includes_unrealized_loss_and_recovery_is_not_daily_restart(self):
        r = RiskState()
        r.advance("2026-11-13T10:00:00+09:00", opening_equity="10000000")
        r.mark("9700000.01")
        self.assertNotIn("LOSS_LOCKED", r.locks)
        r.mark("9700000")
        r.mark("10000000")
        self.assertIn("LOSS_LOCKED", r.locks)
        with self.assertRaises(ValueError): r.recover("LOSS_LOCKED", "DAILY_NET_EQUITY_LOSS", verified=True)

    def test_next_day_carry_blocks_entry_and_reasons_clear_independently(self):
        r = RiskState()
        r.advance("2026-11-13T15:30:00+09:00", opening_equity="10000000")
        r.lock("DATA_DEGRADED", "QUOTE_STALE")
        r.lock("DATA_DEGRADED", "RANKING_STALE")
        r.advance("2026-11-16T09:00:00+09:00", has_positions=True)
        self.assertIn("PRIOR_DAY_UNFLATTENED", r.locks)
        r.recover("DATA_DEGRADED", "QUOTE_STALE", verified=True)
        self.assertIn("DATA_DEGRADED", r.locks)
        with self.assertRaises(ValueError): r.recover("PRIOR_DAY_UNFLATTENED", "2026-11-13", verified=True, has_positions=True)

    def test_uncertain_mark_missing_calendar_and_time_reversal_fail_closed(self):
        r = RiskState()
        r.advance("2026-11-13T10:00:00+09:00", opening_equity="10000000")
        self.assertFalse(r.candidate_entry_allowed)
        r.mark(None)
        self.assertIn("DATA_DEGRADED", r.locks)
        with self.assertRaises(ValueError): r.advance("2026-11-13T09:59:59+09:00")
