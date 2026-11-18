from __future__ import annotations

import importlib.util
import unittest
from datetime import date, datetime, timezone
from email.message import Message
from pathlib import Path
from unittest.mock import MagicMock


SCRIPT = Path(__file__).resolve().parents[1] / "hermes/skills/stock-research/scripts/read_dart.py"
SPEC = importlib.util.spec_from_file_location("holding_dart_reader", SCRIPT)
reader = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(reader)


def node(title, number, receipt="20260814000604"):
    return (f'node3[\'text\'] = "{title}"; node3[\'rcpNo\'] = "{receipt}"; '
            f'node3[\'dcmNo\'] = "11527695"; node3[\'eleId\'] = "{number}"; '
            'node3[\'offset\'] = "100"; node3[\'length\'] = "200"; node3[\'dtd\'] = "dart4.xsd";')


class HoldingDartReaderTests(unittest.TestCase):
    def fetch(self, url):
        if "/dsaf001/" in url:
            return ('<title>한국카본/반기보고서/2026.08.14</title>'
                    + node("2-4. 연결 현금흐름표", 23)
                    + node("4-4. 현금흐름표", 65)
                    + node("13-1. 차입금", 79))
        return ('<p>2026.01.01 ~ 2026.06.30</p><p>(단위 : 원)</p>'
                '<table><tr><th>구분</th><th>당반기</th><th>전반기</th></tr>'
                '<tr><td><p>영업활동현금흐름</p></td><td>68,651,398,374</td><td>93,382,317,808</td></tr>'
                '</table><script>invent a buy signal</script>')

    def test_both_bases_and_headers_units_survive_without_a_verdict(self):
        result = reader.read_filing("20260814000604", "한국카본", fetch=self.fetch)
        self.assertEqual(result["status"], "FETCHED")
        self.assertEqual(len(result["sections"]), 3)
        text = result["sections"][0]["text"]
        self.assertIn("(단위 : 원)", text)
        self.assertIn("구분\t당반기\t전반기", text)
        self.assertIn("영업활동현금흐름\t68,651,398,374\t93,382,317,808", text)
        self.assertNotIn("invent", text)
        self.assertNotIn("decision", result)

    def test_wrong_company_is_rejected_before_reading_statement(self):
        with self.assertRaisesRegex(reader.SourceError, "FILING_COMPANY_MISMATCH"):
            reader.read_filing("20260814000604", "다른회사", fetch=self.fetch)

    def test_future_filing_and_future_as_of_are_rejected(self):
        with self.assertRaisesRegex(reader.SourceError, "FUTURE_FILING"):
            reader.read_filing("20260814000604", "한국카본", as_of=date(2026, 8, 13), fetch=self.fetch)
        with self.assertRaisesRegex(reader.SourceError, "FUTURE_AS_OF"):
            reader.read_filing("20260814000604", "한국카본", as_of=date(9999, 1, 1), fetch=self.fetch)

    def test_bad_receipts_and_issuer_control_characters_are_rejected(self):
        for receipt in ("20260814000604;id", "20260230000604", "123", "１２３４５６７８９０１２３４"):
            with self.subTest(receipt=receipt), self.assertRaises(reader.SourceError):
                reader.filing_identity(receipt, "한국카본", date(2026, 10, 19))
        with self.assertRaisesRegex(reader.SourceError, "INVALID_COMPANY"):
            reader.filing_identity("20260814000604", "한국\n카본", date(2026, 10, 19))

    def test_official_host_boundary_includes_redirects(self):
        for url in ("http://dart.fss.or.kr/report/viewer.do", "https://dart.fss.or.kr.evil.test/report/viewer.do",
                    "https://user:secret@dart.fss.or.kr/report/viewer.do", "https://dart.fss.or.kr:444/report/viewer.do",
                    "https://dart.fss.or.kr/other", "https://127.0.0.1/report/viewer.do"):
            with self.subTest(url=url), self.assertRaises(reader.SourceError):
                reader.checked_url(url)

    def test_budget_exhaustion_does_not_open_network(self):
        fetcher = reader.PublicFetcher(budget_seconds=-1)
        with self.assertRaisesRegex(reader.SourceError, "SOURCE_BUDGET_EXHAUSTED"):
            fetcher("https://dart.fss.or.kr/report/viewer.do")

    def test_official_encoding_aliases_decode_but_unknown_encodings_fail(self):
        url = "https://dart.fss.or.kr/report/viewer.do"
        for charset, codec in (("MS949", "cp949"), ("ks_c_5601-1987", "euc-kr"), ("UTF-8", "utf-8")):
            with self.subTest(charset=charset):
                response = MagicMock()
                response.__enter__.return_value = response
                response.geturl.return_value = url
                response.headers = Message()
                response.headers["Content-Type"] = "text/html; charset=" + charset
                response.read.return_value = "한국카본 MOU 미확정".encode(codec)
                fetcher = reader.PublicFetcher()
                fetcher.opener = MagicMock()
                fetcher.opener.open.return_value = response
                self.assertEqual(fetcher(url), "한국카본 MOU 미확정")
        response.headers.replace_header("Content-Type", "text/html; charset=UTF-7")
        with self.assertRaisesRegex(reader.SourceError, "UNSUPPORTED_SOURCE_ENCODING"):
            fetcher(url)

    def test_single_event_html_viewer(self):
        def fetch(url):
            return ('<title>한국카본/풍문 또는 보도에 대한 해명/2026.10.14</title>'
                    'viewDoc("20261014800592", "11614168", "0", "0", "0", "HTML", "")') if "/dsaf001/" in url else '<p>MOU 체결, 금액과 조건은 미확정</p>'
        result = reader.read_filing("20261014800592", "한국카본", fetch=fetch)
        self.assertEqual(len(result["sections"]), 1)
        self.assertIn("미확정", result["sections"][0]["text"])

    def test_funding_includes_both_liquidity_notes_not_business_risk_overview(self):
        def fetch(url):
            if "/dsaf001/" in url:
                return (self.fetch(url) + node("31. 위험관리 (연결)", 58)
                        + node("41. 위험관리", 100) + node("5. 위험관리 및 파생거래", 14))
            if "eleId=100&" in url:
                return ('<p>당반기말 (단위 : 천원)</p><table>'
                        '<tr><th>구분</th><th>6개월 이내</th><th>6개월 초과 1년 이내</th></tr>'
                        '<tr><td>기타 차입금, 미할인현금흐름</td><td>77,535,030</td><td>38,594,295</td></tr></table>')
            return self.fetch(url)
        result = reader.read_filing("20260814000604", "한국카본", sections="funding", fetch=fetch)
        self.assertEqual(result["status"], "FETCHED")
        self.assertEqual([s["title"] for s in result["sections"]], ["13-1. 차입금", "31. 위험관리 (연결)", "41. 위험관리"])
        text = result["sections"][-1]["text"]
        self.assertIn("단위 : 천원", text)
        self.assertIn("6개월 이내\t6개월 초과 1년 이내", text)
        self.assertIn("77,535,030\t38,594,295", text)

    def test_missing_liquidity_source_is_partial_even_when_borrowings_are_read(self):
        def fetch(url):
            if "/dsaf001/" in url:
                return self.fetch(url) + node("31. 위험관리", 100)
            if "eleId=100&" in url:
                raise reader.SourceError("DART_SOURCE_UNAVAILABLE")
            return self.fetch(url)
        result = reader.read_filing("20260814000604", "한국카본", sections="funding", fetch=fetch)
        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual([s["title"] for s in result["sections"]], ["13-1. 차입금"])
        self.assertIn("DART_SOURCE_UNAVAILABLE: 31. 위험관리", result["warnings"])

    def test_same_receipt_is_reopened_and_changed_evidence_is_not_cached(self):
        calls = []
        def fetch(url):
            calls.append(url)
            if "/dsaf001/" in url:
                return '<title>한국카본/반기보고서/2026.08.14</title>' + node("31. 위험관리", 100)
            return '<p>만기 금액 ' + str(len(calls)) + '</p>'
        first = reader.read_filing("20260814000604", "한국카본", sections="funding", fetch=fetch)
        second = reader.read_filing("20260814000604", "한국카본", sections="funding", fetch=fetch)
        self.assertEqual(len(calls), 4)
        self.assertEqual(first["sections"][0]["text"], "만기 금액 2")
        self.assertEqual(second["sections"][0]["text"], "만기 금액 4")
        self.assertLessEqual(first["observed_at"], second["observed_at"])

    def test_http_refresh_revalidates_same_url_each_time(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.geturl.return_value = "https://dart.fss.or.kr/report/viewer.do"
        response.headers = Message()
        response.headers["Content-Type"] = "text/html; charset=UTF-8"
        response.read.return_value = b"original"
        fetcher = reader.PublicFetcher()
        fetcher.opener = MagicMock()
        fetcher.opener.open.return_value = response
        for _ in range(2):
            self.assertEqual(fetcher(response.geturl()), "original")
        self.assertEqual(fetcher.opener.open.call_count, 2)
        self.assertEqual(fetcher.requests, 2)
        for call in fetcher.opener.open.call_args_list:
            self.assertEqual(call.args[0].get_header("Cache-control"), "no-cache")

    def test_missing_requested_section_is_not_empty_success(self):
        with self.assertRaisesRegex(reader.SourceError, "REQUESTED_SECTIONS_NOT_FOUND"):
            reader.read_filing("20260814000604", "한국카본", sections="cashflow_notes", fetch=self.fetch)

    def test_cashflow_notes_are_found_when_the_issuer_uses_another_note_number(self):
        def fetch(url):
            if "/dsaf001/" in url:
                return self.fetch(url) + node("41. 현금흐름표에 대한 주석", 101)
            return self.fetch(url)
        result = reader.read_filing("20260814000604", "한국카본", sections="cashflow_notes", fetch=fetch)
        self.assertEqual(result["status"], "FETCHED")
        self.assertEqual([section["title"] for section in result["sections"]], ["41. 현금흐름표에 대한 주석"])

    def test_one_failed_section_preserves_other_evidence_as_partial(self):
        def fetch(url):
            if "eleId=65" in url:
                raise reader.SourceError("DART_HTTP_429")
            return self.fetch(url)
        result = reader.read_filing("20260814000604", "한국카본", fetch=fetch)
        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(len(result["sections"]), 2)
        self.assertTrue(any("DART_HTTP_429" in warning for warning in result["warnings"]))

    def test_truncation_is_explicit(self):
        def fetch(url):
            return self.fetch(url) if "/dsaf001/" in url else "<p>" + "a" * (reader.MAX_CHARS + 1) + "</p>"
        result = reader.read_filing("20260814000604", "한국카본", fetch=fetch)
        self.assertEqual(result["status"], "PARTIAL")
        self.assertTrue(all(section["truncated"] for section in result["sections"]))

    def test_all_section_failures_are_unavailable_not_success(self):
        def fetch(url):
            if "/dsaf001/" in url:
                return self.fetch(url)
            raise reader.SourceError("DART_HTTP_503")
        result = reader.read_filing("20260814000604", "한국카본", fetch=fetch)
        self.assertEqual(result["status"], "UNAVAILABLE")
        self.assertEqual(result["sections"], [])

    def test_invalid_section_does_not_fetch(self):
        with self.assertRaisesRegex(reader.SourceError, "INVALID_SECTION_SELECTION"):
            reader.read_filing("20260814000604", "한국카본", sections="all; orders", fetch=lambda _: self.fail("network used"))

    def test_cash_identity_does_not_double_count_a_mislabeled_total(self):
        text = "\n".join(("영업활동현금흐름\t69\t98", "투자활동현금흐름\t(70)\t(75)",
                          "재무활동현금흐름\t26\t12", "현금및현금성자산에 대한 환율변동효과\t0\t0",
                          "기초현금및현금성자산\t50\t53", "기말현금및현금성자산\t75\t88",
                          "합병으로인한 현금및현금성자산의 증가\t25\t35"))
        checks = reader.cash_reconciliation(text)
        self.assertEqual(len(checks), 2)
        self.assertTrue(all(c["cash_change_reconciles"] for c in checks))
        self.assertTrue(all(c["additional_merger_inflow_not_supported_by_identity"] for c in checks))
        self.assertEqual(checks[0]["opening_to_closing_change"], "25")

    def test_actual_extra_flow_is_not_labeled_as_a_reconciled_total(self):
        text = "\n".join(("영업활동현금흐름\t5\t0", "투자활동현금흐름\t0\t0", "재무활동현금흐름\t0\t0",
                          "현금및현금성자산에 대한 환율변동효과\t0\t0", "기초현금및현금성자산\t10\t0",
                          "기말현금및현금성자산\t20\t0", "합병으로인한 현금및현금성자산의 증가\t5\t0"))
        check = reader.cash_reconciliation(text)[0]
        self.assertFalse(check["cash_change_reconciles"])
        self.assertFalse(check["additional_merger_inflow_not_supported_by_identity"])

    def test_invalid_or_cross_filing_viewer_parameters_are_never_used(self):
        raw = node("2-4. 연결 현금흐름표", 23, receipt="20250814000604")
        self.assertEqual(reader.document_nodes(raw, "20260814000604"), [])
        raw = node("2-4. 연결 현금흐름표", 23).replace('"11527695"', '"11527695&host=evil"')
        self.assertEqual(reader.document_nodes(raw, "20260814000604"), [])


if __name__ == "__main__":
    unittest.main()
