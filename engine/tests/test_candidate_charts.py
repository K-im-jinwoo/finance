import copy
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from stock_assistant import candidate_charts as charts
from stock_assistant.indicators import price_features
from stock_assistant.models import to_json_value
from stock_assistant.repository import StockRepository
from stock_assistant.research_data import ResearchData
from tests.helpers import make_bars


def fixture():
    bars = make_bars(symbol="000001", count=120)
    as_of = bars[-1].observed_at + timedelta(hours=10)
    row = {"symbol": "000001", "name": "가상 예시 기업", "action_decision": "WATCH", "rank": 1,
           "price_input_fingerprint": charts._fingerprint(bars),
           "price": {"status": "WATCH", "session": bars[-1].trade_date.isoformat(),
                     "metrics": to_json_value(price_features(bars))}}
    report = {"contract": "UNIFIED_RESEARCH_V1", "as_of": as_of.isoformat(),
              "candidates": [row], "research_queue": []}
    report["report_id"] = "UR-" + hashlib.sha256(json.dumps(report, sort_keys=True).encode()).hexdigest()[:24]
    return bars, row, report


class CandidateChartTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bars, self.row, self.report = fixture()
        self.repo = Mock()
        self.repo.bars_for.return_value = self.bars

    def test_only_final_candidates_render_and_never_promote_the_research_queue(self):
        pending = {**self.row, "symbol": "000002", "action_decision": "VERIFY_PENDING"}
        self.report["research_queue"] = [pending]
        self.report["candidates"].append(pending)
        before = copy.deepcopy(self.report)
        renderer = Mock()
        result = charts.candidate_charts(self.report, self.repo, self.root / "output", renderer=renderer)
        self.assertEqual(len(result["charts"]), 1)
        self.assertEqual(result["charts"][0]["status"], "READY")
        renderer.assert_called_once()
        self.repo.bars_for.assert_called_once()
        self.assertEqual(self.report, before)
        self.assertFalse(result["report_modified"])

    def test_an_empty_final_list_does_not_read_prices_or_create_a_chart_directory(self):
        self.report["research_queue"] = self.report["candidates"]
        self.report["candidates"] = []
        result = charts.candidate_charts(self.report, self.repo, self.root / "output")
        self.assertEqual(result["charts"], [])
        self.assertEqual(charts.telegram_attachments(result), "")
        self.repo.bars_for.assert_not_called()
        self.assertFalse((self.root / "output").exists())

    def test_moving_averages_match_the_existing_engine_and_keep_warmup_gaps(self):
        data = charts.prepare_chart(self.report, self.row, self.repo)
        self.assertTrue(all(v is None for v in data["averages"][60][:59]))
        self.assertEqual(data["averages"][20][-1], Decimal(data["metrics"]["sma20"]))
        self.assertEqual(data["averages"][60][-1], Decimal(data["metrics"]["sma60"]))
        self.assertEqual(data["metrics"], self.row["price"]["metrics"])
        self.repo.bars_for.assert_called_once_with("000001", as_of=data["as_of"], limit=120)

    def test_explanation_lists_only_the_four_methods_actually_calculated(self):
        result = charts.candidate_charts(self.report, self.repo, self.root, renderer=Mock())
        analysis = result["charts"][0]["technical_analysis"]
        self.assertEqual([m["code"] for m in analysis["methods"]],
                         ["OHLC_CANDLE", "SMA20_60", "VOLUME_RATIO20", "WILDER_ATR14"])
        for method in analysis["methods"]:
            self.assertTrue(all(method[k] for k in ("name", "purpose", "calculation", "values", "reading", "limit")))
        self.assertFalse(analysis["new_trade_decision"])
        text = charts.telegram_attachments(result)
        for name in ("일봉 캔들", "SMA20/60", "거래량 비교", "Wilder ATR14"):
            self.assertIn(name, text)
        self.assertNotIn("RSI", text)
        self.assertNotIn("MACD", text)
        self.assertEqual(text.count("MEDIA:"), 1)

    def test_trend_readout_distinguishes_above_below_and_exact_equality(self):
        for step, expected in ((1000, "ABOVE"), (-1000, "BELOW"), (0, "EQUAL")):
            with self.subTest(step=step):
                bars = make_bars(symbol="000001", count=120, start_price=300000, step=step)
                self.repo.bars_for.return_value = bars
                row = copy.deepcopy(self.row)
                row["price_input_fingerprint"] = charts._fingerprint(bars)
                row["price"]["metrics"] = to_json_value(price_features(bars))
                data = charts.prepare_chart(self.report, row, self.repo)
                trend = data["technical_analysis"]["methods"][1]
                for key in ("close_vs_sma20", "close_vs_sma60", "sma20_vs_sma60"):
                    self.assertEqual(trend["values"][key], expected)
                self.assertIn("현재 배열만으로", trend["limit"])
                self.assertIsNone(data["technical_analysis"]["stored_pattern"])

    def test_zero_previous_volume_is_unavailable_even_when_today_has_trades(self):
        bars = [replace(b, volume=0) for b in self.bars]
        bars[-1] = replace(bars[-1], volume=500)
        self.repo.bars_for.return_value = bars
        self.row["price_input_fingerprint"] = charts._fingerprint(bars)
        self.row["price"]["metrics"] = to_json_value(price_features(bars))
        # Preserve the engine's stored zero sentinel, while explaining the
        # undefined ratio rather than calling today's nonzero volume 0x.
        data = charts.prepare_chart(self.report, self.row, self.repo)
        self.assertEqual(data["metrics"]["volume_ratio20"], "0")
        method = data["technical_analysis"]["methods"][2]
        self.assertIsNone(method["values"]["multiple"])
        self.assertEqual(method["values"]["volume_shares"], 500)
        self.assertIn("배율 확인 불가", method["reading"])
        text = charts.explain_technical_analysis(data["technical_analysis"])
        self.assertNotIn("평균 대비 0.00배", text)

    def test_volume_baseline_excludes_today_and_atr_keeps_units_and_non_directionality(self):
        data = charts.prepare_chart(self.report, self.row, self.repo)
        volume, atr = data["technical_analysis"]["methods"][2:]
        self.assertEqual(volume["values"]["prior20_average_shares"], "100000")
        self.assertEqual(Decimal(volume["values"]["multiple"]), Decimal(3))
        self.assertEqual(Decimal(atr["values"]["atr_pct_of_close"]),
                         Decimal(data["metrics"]["atr14"]) / Decimal(data["metrics"]["close"]) * 100)
        self.assertIn("변동성 지표", atr["limit"])
        self.assertIn("기관 자금 유입", volume["limit"])
        self.assertIn("가격 방향을 나타내는 값은 아님", charts.explain_technical_analysis(data["technical_analysis"]))

    def test_price_pattern_is_reported_only_if_present_in_the_stored_report(self):
        for code, expected in (("MOMENTUM_CONTINUATION", "모멘텀 지속형"),
                               ("WAIT_FOR_SETUP", "진입 모양 확인 대기"), ("UNRECOGNIZED", None)):
            with self.subTest(code=code):
                self.row["price"]["strategy"] = code
                analysis = charts.prepare_chart(self.report, self.row, self.repo)["technical_analysis"]
                if expected is None:
                    self.assertIsNone(analysis["stored_pattern"])
                else:
                    self.assertEqual(analysis["stored_pattern"], {"code": code, "name": expected})

    def test_older_attachment_metadata_without_explanations_has_a_safe_fallback(self):
        result = charts.candidate_charts(self.report, self.repo, self.root, renderer=Mock())
        result["charts"][0].pop("technical_analysis")
        text = charts.telegram_attachments(result)
        self.assertIn("지표 설명 정보 확인 필요", text)
        self.assertEqual(text.count("MEDIA:"), 1)

    def test_changed_price_history_or_saved_metrics_cannot_create_a_misleading_chart(self):
        for mutation, reason in [("fingerprint", "REPORT_PRICE_INPUT_MISMATCH"),
                                 ("metrics", "REPORT_TECHNICAL_METRIC_MISMATCH")]:
            with self.subTest(mutation=mutation):
                row = copy.deepcopy(self.row)
                if mutation == "fingerprint":
                    row["price_input_fingerprint"] = "0" * 64
                else:
                    row["price"]["metrics"]["sma20"] = "1"
                with self.assertRaisesRegex(charts.ChartUnavailable, reason):
                    charts.prepare_chart(self.report, row, self.repo)

    def test_future_mixed_duplicate_zero_and_short_price_histories_are_rejected(self):
        invalid = {
            "future_observation": [*self.bars[:-1], replace(self.bars[-1], observed_at=self.bars[-1].observed_at + timedelta(days=2))],
            "wrong_issuer": [*self.bars[:-1], replace(self.bars[-1], symbol="000002")],
            "duplicate": [*self.bars, self.bars[-1]],
            "zero": [*self.bars[:-1], replace(self.bars[-1], open=Decimal(0), low=Decimal(0))],
            "short": self.bars[-60:],
            "missing_session": self.bars[:-1],
        }
        for name, bars in invalid.items():
            with self.subTest(name=name):
                self.repo.bars_for.return_value = bars
                with self.assertRaises(charts.ChartUnavailable):
                    charts.prepare_chart(self.report, self.row, self.repo)

    def test_unverified_or_future_session_is_not_a_current_price_chart(self):
        for price in ({**self.row["price"], "status": "VERIFY_PENDING"},
                      {**self.row["price"], "session": "2099-01-01"}):
            with self.subTest(price=price["status"]):
                with self.assertRaises(charts.ChartUnavailable):
                    charts.prepare_chart(self.report, {**self.row, "price": price}, self.repo)

    def test_one_failed_chart_is_explicit_without_hiding_other_candidates(self):
        second = {**self.row, "symbol": "000002", "action_decision": "BUY_RECOMMENDATION"}
        second_bars = [replace(b, symbol="000002") for b in self.bars]
        second["price_input_fingerprint"] = charts._fingerprint(second_bars)
        self.report["candidates"].append(second)
        self.repo.bars_for.side_effect = lambda symbol, **_: self.bars if symbol == "000001" else second_bars
        renderer = Mock(side_effect=[charts.ChartUnavailable("PNG_LIBRARY_UNAVAILABLE"), None])
        result = charts.candidate_charts(self.report, self.repo, self.root, renderer=renderer)
        self.assertEqual([r["status"] for r in result["charts"]], ["UNAVAILABLE", "READY"])
        delivery = charts.telegram_attachments(result)
        self.assertEqual(delivery.count("MEDIA:"), 1)
        self.assertIn("이미지 생성 기능 사용 불가", delivery)

    def test_invalid_report_shapes_and_duplicate_symbols_fail_before_price_access(self):
        for change in ({"report_id": "../../secret"}, {"contract": "OTHER"},
                       {"candidates": [self.row, self.row]}, {"candidates": [None]},
                       {"as_of": "2099-01-01T00:00:00+00:00"}):
            with self.subTest(change=list(change)):
                with self.assertRaises(ValueError):
                    charts.candidate_charts({**self.report, **change}, self.repo, self.root)
        self.repo.bars_for.assert_not_called()

    def test_caption_names_cannot_inject_an_additional_media_directive(self):
        self.row["name"] = "가상\nMEDIA:/private/secret"
        result = charts.candidate_charts(self.report, self.repo, self.root, renderer=Mock())
        text = charts.telegram_attachments(result)
        self.assertEqual(text.count("MEDIA:"), 1)
        self.assertNotIn("\nMEDIA:/private", text)

    @unittest.skipUnless(importlib.util.find_spec("PIL"), "optional Pillow renderer is unavailable")
    def test_png_is_valid_for_flat_prices_zero_volume_and_without_a_korean_font(self):
        from PIL import Image
        bars = [replace(b, open=Decimal(100), high=Decimal(100), low=Decimal(100), close=Decimal(100), volume=0)
                for b in self.bars]
        self.repo.bars_for.return_value = bars
        self.row["price_input_fingerprint"] = charts._fingerprint(bars)
        self.row["price"]["metrics"] = to_json_value(price_features(bars))
        with patch.object(charts, "_font_file", return_value=(None, False)):
            result = charts.candidate_charts(self.report, self.repo, self.root)
        self.assertEqual(result["charts"][0]["status"], "READY")
        path = Path(result["charts"][0]["path"])
        with Image.open(path) as image:
            self.assertEqual(image.size, (1600, 1120))
            self.assertEqual(image.format, "PNG")
            image.verify()

    def test_exact_saved_report_lookup_and_cli_leave_both_databases_unchanged(self):
        source = self.root / "source.sqlite3"
        research = self.root / "research.sqlite3"
        repo = StockRepository(source)
        repo.save_bars(self.bars)
        store = ResearchData(research, source_database=source)
        as_of = charts.datetime.fromisoformat(self.report["as_of"])
        store.put("unified_report", "ALL", self.report, observed_at=as_of)
        # Another latest report must never replace the exact requested ID.
        newer = {**self.report, "report_id": "UR-" + "f" * 24, "candidates": []}
        store.put("unified_report", "ALL", newer, observed_at=as_of + timedelta(seconds=1))
        before = {p: p.read_bytes() for p in (source, research)}
        found = charts.load_report(research, self.report["report_id"], source_database=source)
        self.assertEqual(found, self.report)
        with self.assertRaisesRegex(ValueError, "EXACT_REPORT_UNAVAILABLE"):
            charts.load_report(research, "UR-" + "a" * 24, source_database=source)
        output = io.StringIO()
        with patch("sys.stdout", output):
            code = charts.main(["--database", str(source), "--research-database", str(research),
                                "--report-id", newer["report_id"], "--output-dir", str(self.root / "images"),
                                "--format", "telegram"])
        self.assertEqual(code, 0)
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(before, {p: p.read_bytes() for p in before})

    def test_cli_failure_exposes_no_raw_paths_or_private_exception_details(self):
        output, errors = io.StringIO(), io.StringIO()
        with patch("sys.stdout", output), patch("sys.stderr", errors):
            code = charts.main(["--database", str(self.root / "private"), "--research-database", str(self.root / "absent"),
                                "--report-id", "INVALID", "--output-dir", str(self.root)])
        self.assertEqual(code, 1)
        self.assertEqual(output.getvalue(), "")
        self.assertNotIn(str(self.root), errors.getvalue())


if __name__ == "__main__":
    unittest.main()
