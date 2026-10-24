import os
import sqlite3
from pathlib import Path


def data_dir() -> Path:
    d = Path(os.environ.get("FORESIGHT_HOME") or Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Foresight")
    d.mkdir(parents=True, exist_ok=True)
    return d


SCHEMA = """
CREATE TABLE IF NOT EXISTS samples(
  id INTEGER PRIMARY KEY,
  ts REAL NOT NULL,          -- unix seconds; a sample's duration is derived from the next ts (capped), so gaps = no data
  kind TEXT NOT NULL,        -- window | nowindow | private
  exe TEXT, title TEXT, url TEXT,
  category TEXT,             -- set only for kind=private
  idle_s REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS samples_ts ON samples(ts);
CREATE TABLE IF NOT EXISTS tasks(
  id INTEGER PRIMARY KEY,
  user TEXT NOT NULL,        -- 'me', 'demo' (read-only seed) or 'demo:<session>'
  desc TEXT NOT NULL, type TEXT NOT NULL, guess REAL,
  est TEXT,                  -- JSON Estimate made at start, kept for calibration
  started REAL, ended REAL,
  measured REAL,             -- minutes the agent counted
  actual REAL,               -- user-confirmed minutes; only these train the model
  status TEXT NOT NULL       -- active | review | done | discarded
);
CREATE INDEX IF NOT EXISTS tasks_user ON tasks(user, status);
CREATE TABLE IF NOT EXISTS segments(
  task_id INTEGER NOT NULL, start REAL, end REAL, counted INTEGER, source TEXT, reason TEXT, kind TEXT
);
CREATE TABLE IF NOT EXISTS live(task_id INTEGER PRIMARY KEY, json TEXT NOT NULL, updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS overrides(task_id INTEGER NOT NULL, start REAL NOT NULL, counted INTEGER NOT NULL,
  PRIMARY KEY(task_id, start));
CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS judged(  -- LLM verdict cache; the snippet is stored only as a hash
  task_id INTEGER NOT NULL, h TEXT NOT NULL, on_task INTEGER NOT NULL, reason TEXT, PRIMARY KEY(task_id, h));
"""


def connect(path=None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or data_dir() / "foresight.db", check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def get_setting(conn, k, default=None):
    r = conn.execute("SELECT v FROM settings WHERE k=?", (k,)).fetchone()
    return r[0] if r else default


def set_setting(conn, k, v):
    conn.execute("INSERT OR REPLACE INTO settings(k, v) VALUES (?,?)", (k, str(v)))


def delete_all(conn):
    """Drop every table and vacuum, then recreate an empty schema that won't reseed."""
    for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
        conn.execute(f'DROP TABLE IF EXISTS "{name}"')
    conn.execute("VACUUM")
    conn.executescript(SCHEMA)
    set_setting(conn, "no_seed", 1)
