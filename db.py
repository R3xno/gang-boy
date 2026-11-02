import sqlite3, time
from contextlib import contextmanager

conn = sqlite3.connect("bot.db", isolation_level=None, check_same_thread=False)
conn.row_factory = sqlite3.Row

conn.executescript("""
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS users(
  user_id INTEGER PRIMARY KEY, name TEXT, coins INTEGER NOT NULL DEFAULT 500,
  candies INTEGER NOT NULL DEFAULT 0, selected INTEGER, last_daily REAL NOT NULL DEFAULT 0,
  sort_by TEXT NOT NULL DEFAULT 'caught', display TEXT NOT NULL DEFAULT 'type',
  pagesize INTEGER NOT NULL DEFAULT 20);
CREATE TABLE IF NOT EXISTS pokemon(
  id INTEGER PRIMARY KEY AUTOINCREMENT, owner INTEGER NOT NULL, species INTEGER NOT NULL,
  level INTEGER NOT NULL, exp INTEGER NOT NULL, nature TEXT NOT NULL, shiny INTEGER NOT NULL DEFAULT 0,
  ivs TEXT NOT NULL, evs TEXT NOT NULL DEFAULT '[0,0,0,0,0,0]', moves TEXT NOT NULL,
  nickname TEXT, caught_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS idx_owner ON pokemon(owner);
CREATE TABLE IF NOT EXISTS pending(id INTEGER PRIMARY KEY AUTOINCREMENT, pid INTEGER NOT NULL, move TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS craft(id TEXT PRIMARY KEY, owner INTEGER NOT NULL, ids TEXT NOT NULL, expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS cache(k TEXT PRIMARY KEY, v TEXT NOT NULL);
""")


@contextmanager
def tx():
    """Atomic transaction. The bot runs on one event loop and these calls never await,
    so every tx() is also atomic w.r.t. other handlers (no double-spend / double-claim)."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def cache_get(k):
    r = conn.execute("SELECT v FROM cache WHERE k=?", (k,)).fetchone()
    return r["v"] if r else None


def cache_set(k, v):
    conn.execute("INSERT OR REPLACE INTO cache(k,v) VALUES(?,?)", (k, v))


def get_user(uid, name=""):
    conn.execute("INSERT OR IGNORE INTO users(user_id,name) VALUES(?,?)", (uid, name))
    if name:
        conn.execute("UPDATE users SET name=? WHERE user_id=?", (name, uid))
    return conn.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()


def pokemon(pid):
    return conn.execute("SELECT * FROM pokemon WHERE id=?", (pid,)).fetchone()


def by_number(uid, n):
    return conn.execute("SELECT * FROM pokemon WHERE owner=? ORDER BY id LIMIT 1 OFFSET ?", (uid, n - 1)).fetchone()


def number_of(uid, pid):
    return conn.execute("SELECT COUNT(*) c FROM pokemon WHERE owner=? AND id<=?", (uid, pid)).fetchone()["c"]


def all_of(uid):
    return conn.execute("SELECT * FROM pokemon WHERE owner=? ORDER BY id", (uid,)).fetchall()


def insert_pokemon(c, owner, species, level, exp, nature, shiny, ivs, moves):
    import json
    cur = c.execute(
        "INSERT INTO pokemon(owner,species,level,exp,nature,shiny,ivs,moves,caught_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (owner, species, level, exp, nature, int(shiny), json.dumps(ivs), json.dumps(moves), time.time()))
    return cur.lastrowid
