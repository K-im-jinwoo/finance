"""Ephemeral SSH reader. No agent calls, writable remote files, or messages."""
import hashlib
import json
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

path = Path('/home/ubuntu/.hermes/profiles/stock-cio/state.db')
for line in sys.stdin:
    try:
        request = json.loads(line)
        if request != {"operation":"final_reports"}:
            raise ValueError("unsupported read")
        rows = []
        with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as c:
            c.execute('PRAGMA query_only=ON')
            query = """SELECT id,session_id,timestamp,content,finish_reason FROM messages
                       WHERE role='assistant' AND (tool_calls IS NULL OR tool_calls='' OR tool_calls='[]')
                       AND (content LIKE '%CIO 최종판정%' OR content LIKE '%CIO 최종 판정%')
                       ORDER BY timestamp DESC,id DESC LIMIT 4"""
            for mid, sid, timestamp, content, finish in c.execute(query):
                if content and len(content) <= 256000:
                    rows.append({"message_id":mid,"session_ref":hashlib.sha256(str(sid).encode()).hexdigest()[:20],
                                 "timestamp":timestamp,"content":content,"finish_reason":finish,
                                 "content_sha256":hashlib.sha256(content.encode()).hexdigest()})
        response = {"retrieved_at":datetime.now(timezone.utc).isoformat(),"messages":rows,"read_only":True}
    except Exception as exc:
        response = {"error":type(exc).__name__}
    print(json.dumps(response,ensure_ascii=False),flush=True)
