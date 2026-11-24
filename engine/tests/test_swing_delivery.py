from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import Mock

from stock_assistant.swing_delivery import (
    deliver_once, render_swing, summarize_with_hermes, swing_route,
)

ROOT = Path(__file__).resolve().parents[1]
GIT_BASH = Path('C:/Program Files/Git/bin/bash.exe')
BASH = str(GIT_BASH) if GIT_BASH.is_file() else shutil.which('bash')


class SwingDeliveryTests(unittest.TestCase):
    def test_dedicated_topic_cannot_alias_longterm_destination(self):
        env = {'TELEGRAM_HOME_CHANNEL': '-100123'}
        with self.assertRaisesRegex(ValueError, 'SWING_TELEGRAM_ROUTE_NOT_DISTINCT'):
            swing_route(env)
        self.assertEqual(swing_route({**env, 'TELEGRAM_SWING_MESSAGE_THREAD_ID': '2'}).message_thread_id, 2)
        with self.assertRaisesRegex(ValueError, 'SWING_TELEGRAM_ROUTE_NOT_DISTINCT'):
            swing_route({**env, 'TELEGRAM_SWING_MESSAGE_THREAD_ID': '1'})

    def test_hermes_returns_only_evidence_selection_and_duration(self):
        row = {'symbol':'005930', 'name':'삼성전자', 'turnover_multiple':'2.5',
               'news_reference_date':'2026-11-24', 'news_evidence':[
                   {'title':'삼성전자 공급계약 체결', 'category':'CONTRACT', 'url':'https://example.test/news'}]}
        report = {'report_id':'SW-fixture', 'price_session':'2026-11-24', 'candidates':[row],
                  'news_checked_count':12, 'status':'READY'}
        valid = {'symbol':'005930', 'evidence_index':0, 'duration':'POTENTIALLY_PERSISTENT'}
        runner = Mock(return_value={'status':'COMPLETE', 'memo':json.dumps([valid])})
        summaries = summarize_with_hermes(report, runner)
        self.assertIn('지속 가능', render_swing(report, summaries))
        self.assertIn('뉴스 기준일 2026-11-24', render_swing(report, summaries))
        runner.return_value['memo'] = json.dumps([{**valid, 'order':'즉시 주문', 'score':100}])
        rejected = summarize_with_hermes(report, runner)
        self.assertEqual(rejected, {})
        text = render_swing(report, rejected)
        self.assertTrue(text.startswith('[스윙]\n'))
        self.assertIn('지속성 확인 필요', text)
        self.assertNotIn('즉시 주문', text)

    def test_reservation_never_sends_same_report_twice_across_workers(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / 'delivery.sqlite3'
            sender = Mock(return_value={'status':'SENT', 'http_status':200})
            with ThreadPoolExecutor(max_workers=2) as workers:
                states = list(workers.map(lambda _: deliver_once(db, 'SW-fixture', '스윙', sender), range(2)))
            self.assertEqual(sorted(states), ['ALREADY_RESERVED', 'SENT'])
            sender.assert_called_once_with('스윙')
            with closing(sqlite3.connect(db)) as connection:
                self.assertEqual(connection.execute('SELECT bot_channel, delivery_policy, status FROM deliveries').fetchone(),
                                 ('SWING', 'AT_MOST_ONCE', 'SENT'))

    @unittest.skipUnless(BASH, 'POSIX shell required')
    def test_daily_and_weekly_jobs_dispatch_directly_to_swing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / 'fixture.env'
            config.write_text('# Offline fixture\n', encoding='utf-8')
            log = root / 'commands.txt'
            docker = root / 'docker'
            docker.write_text('''#!/bin/sh
case "$*" in
  *refresh-daily*) printf 'refresh-daily\\n' >> "$SWING_TEST_LOG" ;;
  *refresh-swing*) printf 'refresh-swing\\n' >> "$SWING_TEST_LOG"; printf '{"daily":true}\\n' ;;
  *evaluate-swing*) printf 'evaluate-swing\\n' >> "$SWING_TEST_LOG"; printf '{"weekly":true}\\n' ;;
  *) exit 23 ;;
esac
''', encoding='utf-8', newline='\n')
            sender = root / 'swing-python'
            sender.write_text('''#!/bin/sh
printf 'swing:%s\\n' "$4" >> "$SWING_TEST_LOG"
[ "$4" = check ] || cat >> "$SWING_TEST_LOG"
''', encoding='utf-8', newline='\n')
            for path in (docker, sender):
                path.chmod(0o700)
            env = {**os.environ, 'PATH': str(root) + os.pathsep + os.environ['PATH'],
                   'STOCK_SCHEDULE_ENV_FILE': config.as_posix(), 'STOCK_CANDIDATE_ENV_FILE': config.as_posix(),
                   'STOCK_SWING_PYTHON': sender.as_posix(), 'SWING_TEST_LOG': log.as_posix()}
            for mode in ('daily', 'weekly'):
                result = subprocess.run([BASH, (ROOT / 'deploy/jobs/stock-swing.sh').as_posix(), mode],
                                        env=env, capture_output=True, text=True, timeout=20)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, '')
            self.assertEqual(log.read_text(encoding='utf-8').splitlines(), [
                'swing:check', 'refresh-daily', 'refresh-swing', 'swing:daily', '{"daily":true}',
                'swing:check', 'evaluate-swing', 'swing:weekly', '{"weekly":true}',
            ])
