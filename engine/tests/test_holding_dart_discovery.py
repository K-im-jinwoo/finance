from __future__ import annotations

import importlib.util
import io
import json
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "hermes/skills/stock-research/scripts/read_dart.py"
SPEC = importlib.util.spec_from_file_location("holding_discovery_reader", SCRIPT)
reader = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(reader)


def row(receipt, title, company="한국카본", code="00159795"):
    return (f'<tr><td><a href="javascript:openCorpInfoNew(\'{code}\', \'win\');">{company}</a></td>'
            f'<td><a href="/dsaf001/main.do?rcpNo={receipt}">{title}</a></td></tr>')


def listing(rows, page=1, total=1):
    return '<table>' + ''.join(rows) + f'</table><div class="pageInfo">[{page}/{total}] [총 20건]</div>'


class PublicFixture:
    def __init__(self):
        self.calls = []
        self.rows = [row("20261014800592", "풍문또는보도에대한해명(미확정)"),
                     row("20261013000001", "사업보고서 (2025.12)"),
                     row("20260818000020", "[첨부정정]주요사항보고서(회사합병결정)"),
                     row("20260814000604", "반기보고서 (2026.06)")]
        self.pages = None
        self.failed = set()

    def __call__(self, url, *, data=None):
        self.calls.append((url, data))
        if data is not None:
            if self.pages:
                return self.pages[int(data['currentPage']) - 1]
            return listing(self.rows)
        if "rcpNo=20261014800592" in url:
            if "event" in self.failed:
                raise reader.SourceError("DART_SOURCE_UNAVAILABLE")
            if "/dsaf001/" in url:
                return ('<title>한국카본/풍문또는보도에대한해명/2026.10.14</title>'
                        'viewDoc("20261014800592", "11614168", "0", "0", "0", "HTML", "")')
            return '<p>케이조선 MOU 체결. 부담액 회사 미공개.</p>'
        if "/dsaf001/" in url:
            receipt = url.split("rcpNo=")[1]
            nodes = []
            for number, title in enumerate(("2-4. 연결 현금흐름표", "4-1. 재무상태표", "4-4. 현금흐름표",
                                            "6. 당기손익-공정가치측정 금융자산", "29-2. 사용이 제한된 금융자산",
                                            "31. 위험관리", "13-1. 차입금", "31. 위험관리 (연결)"), 1):
                nodes.append(f'node3[\'text\'] = "{title}"; node3[\'rcpNo\'] = "{receipt}"; '
                             f'node3[\'dcmNo\'] = "100"; node3[\'eleId\'] = "{number}"; '
                             'node3[\'offset\'] = "0"; node3[\'length\'] = "100"; node3[\'dtd\'] = "dart4.xsd";')
            return '<title>한국카본/반기보고서/2026.08.14</title>' + ''.join(nodes)
        return '<p>당반기말 (단위 : 천원)</p><table><tr><td>6개월 이내</td><td>77,535,030</td></tr></table>'


class HoldingDiscoveryTests(unittest.TestCase):
    def test_company_only_discovers_current_period_and_latest_matching_event(self):
        fetch = PublicFixture()
        result = reader.research_holding("한국카본", event="케이조선", period="half", fetch=fetch)
        self.assertEqual(result["status"], "FETCHED")
        self.assertEqual(result["financial"]["receipt"], "20260814000604")
        self.assertEqual(result["events"][0]["receipt"], "20261014800592")
        self.assertEqual(result["event_candidates_read"], 1)
        self.assertEqual(fetch.calls[0][1]["textCrpNm"], "한국카본")
        self.assertEqual(fetch.calls[0][1]["textCrpCik"], "")
        self.assertNotIn("decision", result)
        self.assertNotIn("31. 위험관리 (연결)", [s['title'] for s in result['financial']['sections']])
        self.assertIn("31. 위험관리", [s['title'] for s in result['financial']['sections']])

    def test_event_phrases_and_spacing_find_same_original_without_scanning_unrelated_mergers(self):
        for term in ('케이조선 인수', '케이조선 인수 자금 부담', '케이조선인수', '케이조선인수부담'):
            with self.subTest(term=term):
                result = reader.research_holding('한국카본', event=term, fetch=PublicFixture())
                self.assertEqual(result['status'], 'FETCHED')
                self.assertEqual(result['matched_event_terms'], ['케이조선'])
                self.assertEqual(result['event_candidates_read'], 1)
                self.assertEqual(result['events'][0]['receipt'], '20261014800592')

    def test_event_subject_with_investment_in_its_name_is_preserved(self):
        self.assertEqual(reader.event_terms('한국투자 인수 부담'), ['한국투자'])
        self.assertEqual(reader.event_terms('한국투자인수'), ['한국투자'])
        self.assertEqual(reader.event_terms('유상증자'), ['유상증자'])

    def test_punctuation_only_event_is_rejected_before_public_search(self):
        with self.assertRaisesRegex(reader.SourceError, 'INVALID_EVENT_TERM'):
            reader.research_holding('한국카본', event='!!!', fetch=lambda *a, **k: self.fail('network used'))

    def test_latest_uses_reporting_period_not_later_old_annual_correction(self):
        result = reader.research_holding("한국카본", fetch=PublicFixture())
        self.assertEqual(result["financial"]["receipt"], "20260814000604")

    def test_same_period_uses_first_latest_final_report(self):
        fetch = PublicFixture()
        fetch.rows.insert(0, row("20261015000001", "[기재정정]반기보고서 (2026.06)"))
        result = reader.research_holding("한국카본", period="half", fetch=fetch)
        self.assertEqual(result["financial"]["receipt"], "20261015000001")

    def test_unknown_period_is_not_claimed_complete(self):
        fetch = PublicFixture(); fetch.rows = [row("20260814000604", "반기보고서")]
        result = reader.research_holding("한국카본", fetch=fetch)
        self.assertEqual(result["status"], "PARTIAL")
        self.assertIn("REPORT_PERIOD_NOT_PARSED", result["warnings"])

    def test_exact_issuer_required_and_ambiguity_is_rejected(self):
        for rows, reason in (([row("20260814000604", "반기보고서", company="한국카본자회사")], "EXACT_PUBLIC_ISSUER_NOT_FOUND"),
                             ([row("20260814000604", "반기보고서"), row("20260813000001", "반기보고서", code="00999999")], "AMBIGUOUS_PUBLIC_ISSUER")):
            fetch = PublicFixture(); fetch.rows = rows
            with self.subTest(reason=reason), self.assertRaisesRegex(reader.SourceError, reason):
                reader.discover_filings("한국카본", fetch=fetch)
            self.assertEqual(len(fetch.calls), 1)

    def test_pagination_unknown_or_date_order_conflict_fails_closed(self):
        fetch = PublicFixture(); fetch.pages = ['<table>' + ''.join(fetch.rows) + '</table>']
        with self.assertRaisesRegex(reader.SourceError, "PUBLIC_LIST_PAGINATION_UNKNOWN"):
            reader.discover_filings("한국카본", fetch=fetch)
        fetch = PublicFixture(); fetch.rows.reverse()
        with self.assertRaisesRegex(reader.SourceError, "PUBLIC_LIST_NOT_DATE_DESCENDING"):
            reader.discover_filings("한국카본", fetch=fetch)

    def test_future_filing_is_not_used_as_evidence(self):
        fetch = PublicFixture(); fetch.rows = [row("20261020000001", "반기보고서 (2026.06)")]
        with self.assertRaisesRegex(reader.SourceError, "FUTURE_FILING"):
            reader.discover_filings("한국카본", as_of=date(2026, 10, 19), fetch=fetch)

    def test_future_reporting_period_is_rejected_before_original_read(self):
        fetch = PublicFixture(); fetch.rows = [row("20261014000001", "반기보고서 (2027.06)")]
        with self.assertRaisesRegex(reader.SourceError, "FUTURE_REPORT_PERIOD"):
            reader.research_holding("한국카본", as_of=date(2026, 10, 19), fetch=fetch)
        self.assertEqual(len(fetch.calls), 1)

    def test_compact_output_keeps_units_and_full_parent_maturity_evidence(self):
        result = reader.research_holding("한국카본", fetch=PublicFixture())
        sections = result['financial']['sections']
        self.assertEqual(len(sections), 6)
        note = next(s for s in sections if s['title'] == '31. 위험관리')
        self.assertIn('단위 : 천원', note['text'])
        self.assertIn('6개월 이내\t77,535,030', note['text'])
        self.assertTrue(any(s['title']=='13-1. 차입금' for s in result['financial']['additional_detail_links']))
        self.assertLess(result['financial']['evidence_characters']['selected'], result['financial']['evidence_characters']['before_selection'])

    def test_unlocated_parent_maturity_note_is_partial_not_complete_funding(self):
        fetch = PublicFixture()
        def without_note(url, **kwargs):
            raw = fetch(url, **kwargs)
            return raw.replace('31. 위험관리"', '32. 기타주석"') if '/dsaf001/' in url else raw
        result = reader.research_holding('한국카본', fetch=without_note)
        self.assertFalse(result['evidence_located']['ofs_maturity_note'])
        self.assertEqual(result['status'], 'PARTIAL')
        self.assertIn('EVIDENCE_NOT_LOCATED: ofs_maturity_note', result['warnings'])

    def test_bounded_pagination_reports_partial_search(self):
        fetch = PublicFixture()
        fetch.pages = [listing([row(f"202608{day:02d}000001", "반기보고서 (2026.06)")], page=i, total=4)
                       for i, day in enumerate((14, 13, 12), 1)]
        result = reader.discover_filings("한국카본", fetch=fetch)
        self.assertEqual(len(fetch.calls), 3)
        self.assertIn("PUBLIC_LIST_PAGE_LIMIT_REACHED", result["warnings"])
        self.assertEqual(fetch.calls[-1][1]['textCrpCik'], '00159795')

    def test_missing_event_is_not_no_adverse_event(self):
        result = reader.research_holding("한국카본", event="다른사건", fetch=PublicFixture())
        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(result['events'], [])
        self.assertIn("EVENT_ORIGINAL_NOT_FOUND_IN_BOUNDED_SEARCH", result['warnings'])

    def test_event_request_failure_keeps_financial_evidence_with_warning(self):
        fetch = PublicFixture(); fetch.failed.add("event")
        result = reader.research_holding("한국카본", event="케이조선", fetch=fetch)
        self.assertEqual(result['status'], 'PARTIAL')
        self.assertTrue(result['financial']['sections'])
        self.assertTrue(any('DART_SOURCE_UNAVAILABLE' in w for w in result['warnings']))

    def test_invalid_discovery_inputs_do_not_fetch(self):
        for kwargs in ({'company':'한국\n카본'}, {'company':'한국카본/다른회사'}, {'company':'한국카본','event':'\n'},
                       {'company':'한국카본','period':'unknown'}):
            with self.subTest(kwargs=kwargs), self.assertRaises(reader.SourceError):
                reader.research_holding(fetch=lambda *a, **k: self.fail('network used'), **kwargs)

    def test_cli_without_receipt_uses_discovery_and_original_mode_stays_compatible(self):
        with patch.object(reader, 'research_holding', return_value={'financial':{'sections':[{}]}}) as research:
            with patch('sys.stdout', new_callable=io.StringIO) as output:
                self.assertEqual(reader.main(['--company','한국카본','--event','케이조선','--period','half']), 0)
            research.assert_called_once_with('한국카본', event='케이조선', period='half')
            self.assertTrue(json.loads(output.getvalue())['financial']['sections'])
        with patch.object(reader, 'read_filing', return_value={'sections':[{}]}) as original:
            with patch('sys.stdout', new_callable=io.StringIO):
                self.assertEqual(reader.main(['--receipt','20260814000604','--company','한국카본']), 0)
            original.assert_called_once_with('20260814000604','한국카본',sections='financials,funding')


if __name__ == '__main__':
    unittest.main()
