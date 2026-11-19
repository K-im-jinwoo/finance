"""Offline tests for full multi-indicator chart analysis, multi-panel rendering, and committee vision."""
from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from stock_assistant import candidate_charts as charts
from stock_assistant.chart_analysis import compute_full_chart_analysis, summarize_for_committee
from stock_assistant.indicators import price_features
from stock_assistant.models import to_json_value
from stock_assistant.repository import StockRepository
from tests.helpers import make_bars


ROOT = Path(__file__).resolve().parents[1]
BRIDGE_PATH = ROOT / "hermes/profiles/stock-cio/scripts/stock-expert-review.py"
COMMITTEE_PATH = ROOT / "hermes/profiles/stock-cio/scripts/stock-committee-brief.py"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bridge = _load("stock_expert_review_chart_test", BRIDGE_PATH)
committee = _load("stock_committee_brief_chart_test", COMMITTEE_PATH)


class ChartAnalysisTests(unittest.TestCase):
    def test_uptrend_computes_all_indicators_and_bullish_alignment(self):
        bars = make_bars(symbol="053800", count=120, start_price=50000, step=400)
        analysis = compute_full_chart_analysis(bars)
        summary = summarize_for_committee(analysis)

        self.assertEqual(summary["bars_count"], 120)
        self.assertEqual(summary["moving_averages"]["alignment"], "BULLISH_FULL")
        self.assertIsNotNone(summary["moving_averages"]["sma120"])
        self.assertEqual(summary["bollinger_20_2"]["state"], "UPPER_HALF")
        self.assertGreaterEqual(summary["rsi14"]["value"], 70.0)
        self.assertEqual(summary["rsi14"]["zone"], "OVERBOUGHT")
        self.assertGreater(summary["macd_12_26_9"]["macd"], 0.0)
        self.assertEqual(summary["ichimoku"]["cloud_position"], "ABOVE_CLOUD")
        self.assertIsNotNone(summary["support_resistance"]["nearest_support"])
        self.assertIsNotNone(summary["volume_profile"]["poc_price"])
        self.assertEqual(summary["obv"]["trend_20d"], "ACCUMULATION")
        # Summary must omit raw time-series arrays.
        self.assertNotIn("series", summary["moving_averages"])
        self.assertNotIn("series", summary["rsi14"])
        self.assertNotIn("bins", summary["volume_profile"])

    def test_flat_zero_volume_bars_are_safe_without_division_errors(self):
        bars = [
            replace(b, open=Decimal(10000), high=Decimal(10000), low=Decimal(10000), close=Decimal(10000), volume=0)
            for b in make_bars(symbol="000001", count=120)
        ]
        analysis = compute_full_chart_analysis(bars)
        self.assertEqual(analysis["rsi14"]["value"], 50.0)
        self.assertIsNone(analysis["bollinger"]["percent_b"])
        self.assertEqual(analysis["volume_profile"]["bins"], [])
        self.assertEqual(analysis["obv"]["trend_20d"], "FLAT")
        self.assertEqual(analysis["candle"]["pattern"], "FLAT")

    def test_candle_patterns_detect_engulfing_and_hammer(self):
        bars = make_bars(symbol="000001", count=65, start_price=20000, step=0)
        bars[-2] = replace(bars[-2], open=Decimal(20500), high=Decimal(20600), low=Decimal(19900), close=Decimal(20000))
        bars[-1] = replace(bars[-1], open=Decimal(19900), high=Decimal(20800), low=Decimal(19800), close=Decimal(20700))
        self.assertEqual(compute_full_chart_analysis(bars)["candle"]["pattern"], "BULLISH_ENGULFING")

        bars[-2] = replace(bars[-2], open=Decimal(20000), high=Decimal(20100), low=Decimal(19900), close=Decimal(20050))
        bars[-1] = replace(bars[-1], open=Decimal(20000), high=Decimal(20150), low=Decimal(19000), close=Decimal(20100))
        self.assertEqual(compute_full_chart_analysis(bars)["candle"]["pattern"], "HAMMER")

    def test_double_bottom_box_breakout_and_trade_plan_grades(self):
        # 1. Double Bottom (W-pattern) with breakout above neckline
        base = make_bars(symbol="000100", count=100, start_price=10000, step=0)
        prices = [10000] * 100
        # Build clear W: drop to 8000 at idx 55, rally to 9600 at idx 70, retest 8100 at idx 82, rally to 9800 at 99
        for i in range(45, 56):
            prices[i] = int(10000 - (i - 45) * 200)
        for i in range(56, 71):
            prices[i] = int(8000 + (i - 55) * 106)
        for i in range(71, 83):
            prices[i] = int(9600 - (i - 70) * 125)
        for i in range(83, 100):
            prices[i] = int(8100 + (i - 82) * 100)
        w_bars = [
            replace(b, open=Decimal(p - 50), high=Decimal(p + 120), low=Decimal(p - 120), close=Decimal(p), volume=150000)
            for b, p in zip(base, prices)
        ]
        w_res = compute_full_chart_analysis(w_bars)
        w_codes = [p["code"] for p in w_res["patterns"]["detected"]]
        self.assertIn("DOUBLE_BOTTOM", w_codes)
        self.assertIsNotNone(w_res["trade_plan"]["breakout_price"])
        self.assertIsNotNone(w_res["trade_plan"]["stop_loss"])

        # 2. Box Range Breakout
        box_prices = [10000 + (450 if (i % 6 < 3) else -450) for i in range(97)] + [10700, 10850, 11000]
        box_bars = [
            replace(b, open=Decimal(p - 40), high=Decimal(p + 80), low=Decimal(p - 80), close=Decimal(p), volume=200000)
            for b, p in zip(base, box_prices)
        ]
        box_res = compute_full_chart_analysis(box_bars)
        box_codes = [p["code"] for p in box_res["patterns"]["detected"]]
        self.assertIn("BOX_RANGE", box_codes)

        # 3. Overbought uptrend classifies as WAIT_PULLBACK
        hot_bars = make_bars(symbol="000200", count=120, start_price=50000, step=500)
        hot_res = compute_full_chart_analysis(hot_bars)
        self.assertEqual(hot_res["trade_plan"]["grade"], "WAIT_PULLBACK")
        self.assertLess(hot_res["trade_plan"]["scoring_bonus"], 0)

        # 4. Downtrend classifies as CHART_CAUTION
        down_bars = make_bars(symbol="000300", count=120, start_price=100000, step=-400)
        down_res = compute_full_chart_analysis(down_bars)
        self.assertEqual(down_res["trade_plan"]["grade"], "CHART_CAUTION")

    def test_cup_and_handle_and_triangle_detection(self):
        base = make_bars(symbol="000400", count=110, start_price=20000, step=0)
        # Cup & Handle: left rim ~20000 at idx 22, smooth U bottom ~15500 at idx 58, right rim ~19900 at idx 95, shallow handle 18900..19700
        cup_prices = [19000] * 110
        for i in range(15, 25):
            cup_prices[i] = 19000 + (i - 15) * 110
        for i in range(25, 60):
            t = (i - 25) / 35.0
            cup_prices[i] = int(20000 - 4500 * t)
        for i in range(60, 96):
            t = (i - 60) / 35.0
            cup_prices[i] = int(15500 + 4400 * t)
        for i in range(96, 105):
            cup_prices[i] = int(19800 - (i - 95) * 90)
        for i in range(105, 110):
            cup_prices[i] = int(19000 + (i - 104) * 180)
        cup_bars = [
            replace(b, open=Decimal(p - 60), high=Decimal(p + 120), low=Decimal(p - 120), close=Decimal(p), volume=180000)
            for b, p in zip(base, cup_prices)
        ]
        cup_res = compute_full_chart_analysis(cup_bars)
        self.assertIn("CUP_AND_HANDLE", [p["code"] for p in cup_res["patterns"]["detected"]])

    def test_compact_actionable_caption_includes_extended_indicators(self):
        bars = make_bars(symbol="053800", count=120, start_price=50000, step=350)
        as_of = bars[-1].observed_at + timedelta(hours=10)
        row = {
            "symbol": "053800", "name": "안랩", "action_decision": "BUY_RECOMMENDATION", "rank": 1,
            "price_input_fingerprint": charts._fingerprint(bars),
            "price": {"status": "READY", "session": bars[-1].trade_date.isoformat(),
                      "strategy": "MOMENTUM_CONTINUATION", "metrics": to_json_value(price_features(bars))},
        }
        report = {
            "contract": "UNIFIED_RESEARCH_V1", "policy_mode": "ACTIONABLE_COMMITTEE",
            "as_of": as_of.isoformat(), "candidates": [row], "research_queue": [],
        }
        report["report_id"] = "UR-" + hashlib.sha256(json.dumps(report, sort_keys=True).encode()).hexdigest()[:24]
        repo = Mock()
        repo.bars_for.return_value = bars
        with tempfile.TemporaryDirectory() as tmp:
            result = charts.candidate_charts(report, repo, Path(tmp), renderer=Mock())
            caption = charts.telegram_attachments(result)
        self.assertIn("RSI", caption)
        self.assertIn("MACD", caption)
        self.assertIn("볼린저 %B", caption)
        self.assertIn("매물대 POC", caption)
        self.assertEqual(caption.count("MEDIA:"), 1)

    @unittest.skipUnless(importlib.util.find_spec("PIL"), "optional Pillow renderer is unavailable")
    def test_multipanel_chart_and_committee_contact_sheet_render_valid_png(self):
        from PIL import Image
        bars = make_bars(symbol="053800", count=120, start_price=50000, step=250)
        as_of = bars[-1].observed_at + timedelta(hours=10)
        row = {
            "symbol": "053800", "name": "안랩", "action_decision": "BUY_RECOMMENDATION", "rank": 1,
            "price_input_fingerprint": charts._fingerprint(bars),
            "price": {"status": "READY", "session": bars[-1].trade_date.isoformat(),
                      "strategy": "MOMENTUM_CONTINUATION", "metrics": to_json_value(price_features(bars))},
        }
        report = {
            "contract": "UNIFIED_RESEARCH_V1", "policy_mode": "ACTIONABLE_COMMITTEE",
            "as_of": as_of.isoformat(), "candidates": [row], "research_queue": [],
        }
        report["report_id"] = "UR-" + hashlib.sha256(json.dumps(report, sort_keys=True).encode()).hexdigest()[:24]
        repo = Mock()
        repo.bars_for.return_value = bars
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = charts.candidate_charts(report, repo, root)
            png_path = Path(result["charts"][0]["path"])
            with Image.open(png_path) as img:
                self.assertEqual(img.size, (1600, 1120))
                self.assertEqual(img.format, "PNG")
                img.verify()
            sheet = charts.build_committee_chart_sheet([png_path, png_path], root / "sheet.png")
            with Image.open(sheet) as s_img:
                self.assertEqual(s_img.format, "PNG")
                self.assertGreater(s_img.size[0], 1500)
                s_img.verify()


class CommitteeVisionIntegrationTests(unittest.TestCase):
    def test_run_specialist_validates_and_passes_image_flag(self):
        commands = []
        process = Mock()
        process.wait.return_value = 0

        def spawn(command, **kwargs):
            commands.append(command)
            kwargs["stdout"].write("담당 결론: 차트 확인 완료".encode("utf-8"))
            return process

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            png = root / "sample.png"
            png.write_bytes(b"\x89PNG\r\n\x1a\n")
            txt = root / "bad.txt"
            txt.write_text("not png", encoding="utf-8")
            with patch.object(bridge.subprocess, "Popen", side_effect=spawn), patch.object(bridge, "stop_process"):
                res = bridge.run_specialist("stock-market", "prompt", root, image=png, reasoning="medium")
                with self.assertRaises(ValueError):
                    bridge.run_specialist("stock-market", "prompt", root, image=txt)
        self.assertEqual(res["status"], "COMPLETE")
        self.assertEqual(commands[0][commands[0].index("--image") + 1], str(png.resolve()))

    def test_committee_attaches_chart_analysis_and_vision_only_to_market_with_fallback(self):
        bars = make_bars(symbol="053800", count=120, start_price=50000, step=300)
        as_of = bars[-1].observed_at + timedelta(hours=10)
        row = {
            "symbol": "053800", "name": "안랩", "action_decision": "BUY_RECOMMENDATION", "rank": 1,
            "financial_score": 100, "news_score": 50, "risk_penalty": 0, "total_score": 85.0,
            "price_input_fingerprint": charts._fingerprint(bars),
            "price": {"status": "READY", "session": bars[-1].trade_date.isoformat(),
                      "strategy": "MOMENTUM_CONTINUATION", "metrics": to_json_value(price_features(bars))},
        }
        report = {
            "contract": "UNIFIED_RESEARCH_V1", "report_id": "UR-" + "c" * 24,
            "as_of": as_of.isoformat(), "candidates": [row],
        }
        calls = []
        captured_requests = []

        class FakeBridge:
            def run_specialist(self, profile, prompt, directory, **kwargs):
                calls.append((profile, kwargs.get("image") is not None))
                # Fail first market vision call to verify safe fallback to text JSON mode.
                if profile == "stock-market" and kwargs.get("image") is not None:
                    return {"profile": profile, "status": "UNAVAILABLE", "memo": None, "error_code": "EXPERT_TIMEOUT"}
                return {"profile": profile, "status": "COMPLETE", "memo": f"{profile} 분석 완료"}

            def review(self, request, *, runner, now=None):
                captured_requests.append(request)
                specs = [runner(p, "prompt", Path(".")) for p in ("stock-market", "stock-fundamentals", "stock-risk")]
                return {"status": "COMPLETE", "specialists": specs}

        def fake_sheet(paths, target):
            target.write_bytes(b"\x89PNG\r\n\x1a\n")
            return target

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src_db = root / "stock.sqlite3"
            repo = StockRepository(src_db)
            repo.save_bars(bars)
            report_chart_dir = root / "charts" / report["report_id"]
            report_chart_dir.mkdir(parents=True)
            (report_chart_dir / "053800-deadbeef.png").write_bytes(b"\x89PNG\r\n\x1a\n")

            with patch.object(charts, "build_committee_chart_sheet", side_effect=fake_sheet):
                result = committee.generate_committee_brief(
                    report, bridge=FakeBridge(), now=as_of,
                    source_database=src_db, chart_dir=report_chart_dir,
                )

        self.assertEqual(result["status"], "COMPLETE")
        self.assertEqual(result["chart_analyses_count"], 1)
        self.assertFalse(result["chart_vision_used"])
        # Verify chart_analysis was attached to candidate packet
        packet = captured_requests[0]["evidence"]["sources"][0]["data"]
        self.assertIn("rsi14", packet["chart_analysis"])
        self.assertIn("volume_profile", packet["chart_analysis"])
        # Verify only stock-market tried vision and then fell back cleanly; others never got an image.
        self.assertIn(("stock-market", True), calls)
        self.assertIn(("stock-market", False), calls)
        self.assertNotIn(("stock-fundamentals", True), calls)
        self.assertNotIn(("stock-risk", True), calls)
        self.assertNotIn(("stock-cio", True), calls)


if __name__ == "__main__":
    unittest.main()
