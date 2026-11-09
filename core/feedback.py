"""AI 问答反馈存储 —— 独立数据库 data/ai_feedback.db。

用户对超纲问答（超出知识库、由模型自行回答）打分后落库：
  · thumbs_good  用户点了 👍（认为有用）
  · thumbs_other 用户点了 👎 或未打分

两个表结构一致，分表是为了后续单独复用「被认可的回答」。
这张库与只读的 medical_kg.db 完全独立，写入不影响知识库。
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "ai_feedback.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS thumbs_good (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  question TEXT NOT NULL,
  answer TEXT NOT NULL,
  out_of_kb INTEGER DEFAULT 1,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS thumbs_other (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  question TEXT NOT NULL,
  answer TEXT NOT NULL,
  out_of_kb INTEGER DEFAULT 1,
  created_at TEXT
);
"""


def _connect():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    con.executescript(_SCHEMA)
    return con


def save(question: str, answer: str, vote: str, out_of_kb: bool = True) -> dict:
    """vote: 'good' | 'bad' | 'none'。存进对应表，返回记录 id。"""
    table = "thumbs_good" if vote == "good" else "thumbs_other"
    con = _connect()
    try:
        cur = con.execute(
            f"INSERT INTO {table} (question, answer, out_of_kb, created_at) VALUES (?,?,?,?)",
            (question, answer, 1 if out_of_kb else 0,
             time.strftime("%Y-%m-%d %H:%M:%S")))
        con.commit()
        rid = cur.lastrowid
    finally:
        con.close()
    return {"ok": True, "id": rid, "table": table}


def stats() -> dict:
    con = _connect()
    try:
        good = con.execute("SELECT COUNT(*) FROM thumbs_good").fetchone()[0]
        other = con.execute("SELECT COUNT(*) FROM thumbs_other").fetchone()[0]
    finally:
        con.close()
    return {"good": good, "other": other}
