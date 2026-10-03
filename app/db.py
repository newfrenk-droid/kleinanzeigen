"""SQLite-хранилище: аккаунты, шаблоны, объявления, диалоги, лог, настройки."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path

DB_PATH = Path(os.environ.get("KA_DB_PATH", Path(__file__).resolve().parent.parent / "data" / "bot.db"))

DEFAULT_SETTINGS: dict[str, str] = {
    # Vision
    "vision_token": "",
    "vision_folder_id": "",
    "vision_api_url": "https://v1.empr.cloud/api/v1",
    "vision_local_url": "http://127.0.0.1:3030",
    "vision_platform": "windows",
    "vision_browser": "Chrome",
    "vision_stop_on_exit": "1",
    # Поведение бота
    "poll_interval": "60",            # сек между проверками входящих
    "poll_jitter": "20",              # случайная добавка к интервалу
    "reply_delay_min": "20",          # задержка перед ответом, сек
    "reply_delay_max": "90",
    "typing_delay_ms": "60",          # задержка между символами при наборе
    "reply_mode": "once",             # once — один раз на диалог; every — на каждое новое входящее
    "only_as_seller": "1",            # отвечать только в диалогах по НАШИМ объявлениям
    "send_mode": "ui",                # ui — через окно диалога; api — запросом messagebox
    "ads_sync_minutes": "30",
    "autostart": "0",                 # запускать включённые аккаунты при старте панели
    # Kleinanzeigen (можно поправить, если сайт поменяет вёрстку/API)
    "ka_inbox_url": "https://www.kleinanzeigen.de/m-nachrichten.html",
    "ka_conversation_url": "https://www.kleinanzeigen.de/m-nachrichten.html?conversationId={conversation_id}",
    "ka_gateway_url": "https://gateway.kleinanzeigen.de/messagebox/api",
    "ka_my_ads_url": "https://www.kleinanzeigen.de/m-meine-anzeigen-verwalten.json?sort=DEFAULT&page={page}",
    "ka_ad_url": "https://www.kleinanzeigen.de/s-anzeige/{ad_id}",
    "ka_reply_textarea_selector": "textarea",
    "ka_send_button_selector": "button[type=submit], button[aria-label*='Senden' i], button:has-text('Senden')",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    cookies TEXT NOT NULL,
    proxy TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    vision_profile_id TEXT NOT NULL DEFAULT '',
    vision_proxy_id TEXT NOT NULL DEFAULT '',
    cookies_applied INTEGER NOT NULL DEFAULT 0,
    ka_user_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'stopped',
    last_error TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    text TEXT NOT NULL,
    account_id INTEGER,              -- NULL = для всех аккаунтов
    ad_id TEXT NOT NULL DEFAULT '',  -- конкретное объявление
    keywords TEXT NOT NULL DEFAULT '', -- ключевые слова в названии объявления, через запятую
    priority INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS ads (
    account_id INTEGER NOT NULL,
    ad_id TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    price TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    updated_at REAL NOT NULL,
    PRIMARY KEY (account_id, ad_id)
);

CREATE TABLE IF NOT EXISTS conversations (
    account_id INTEGER NOT NULL,
    conversation_id TEXT NOT NULL,
    ad_id TEXT NOT NULL DEFAULT '',
    ad_title TEXT NOT NULL DEFAULT '',
    buyer_name TEXT NOT NULL DEFAULT '',
    last_inbound_text TEXT NOT NULL DEFAULT '',
    last_inbound_id TEXT NOT NULL DEFAULT '',
    last_inbound_at TEXT NOT NULL DEFAULT '',
    replied_msg_id TEXT NOT NULL DEFAULT '',
    reply_count INTEGER NOT NULL DEFAULT 0,
    last_reply_text TEXT NOT NULL DEFAULT '',
    last_reply_at REAL,
    template_id INTEGER,
    updated_at REAL NOT NULL,
    PRIMARY KEY (account_id, conversation_id)
);

CREATE TABLE IF NOT EXISTS log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    account_id INTEGER,
    level TEXT NOT NULL,
    message TEXT NOT NULL
);
"""

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    """Открывает (или переоткрывает) базу. Без аргумента — путь по умолчанию."""
    global _conn
    with _lock:
        if _conn is not None and path is None:
            return _conn
        p = Path(path or DB_PATH)
        if str(p) != ":memory:":
            p.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(str(p), check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.executescript(SCHEMA)
        if not _conn.execute("SELECT 1 FROM templates LIMIT 1").fetchone():
            _conn.execute(
                "INSERT INTO templates (name, text, priority) VALUES (?, ?, ?)",
                (
                    "Общий шаблон",
                    "Hallo {buyer_name},\n\nvielen Dank für Ihr Interesse an „{ad_title}“! "
                    "Der Artikel ist noch verfügbar. Ich melde mich in Kürze persönlich bei Ihnen.\n\n"
                    "Viele Grüße",
                    0,
                ),
            )
        _conn.commit()
        return _conn


def execute(sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
    with _lock:
        conn = connect()
        cur = conn.execute(sql, params)
        conn.commit()
        return cur


def query(sql: str, params: tuple | dict = ()) -> list[dict]:
    with _lock:
        return [dict(r) for r in connect().execute(sql, params).fetchall()]


def query_one(sql: str, params: tuple | dict = ()) -> dict | None:
    rows = query(sql, params)
    return rows[0] if rows else None


# ---------- settings ----------

def get_settings() -> dict[str, str]:
    values = dict(DEFAULT_SETTINGS)
    for row in query("SELECT key, value FROM settings"):
        values[row["key"]] = row["value"]
    return values


def save_settings(values: dict[str, str]) -> None:
    for key, value in values.items():
        if key in DEFAULT_SETTINGS:
            execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)),
            )


# ---------- accounts ----------

def list_accounts() -> list[dict]:
    return query("SELECT * FROM accounts ORDER BY id")


def get_account(account_id: int) -> dict | None:
    return query_one("SELECT * FROM accounts WHERE id = ?", (account_id,))


def create_account(name: str, cookies: list[dict], proxy: str) -> int:
    cur = execute(
        "INSERT INTO accounts (name, cookies, proxy, created_at) VALUES (?, ?, ?, ?)",
        (name, json.dumps(cookies), proxy, time.time()),
    )
    return int(cur.lastrowid)


def update_account(account_id: int, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    execute(f"UPDATE accounts SET {cols} WHERE id = ?", (*fields.values(), account_id))


def delete_account(account_id: int) -> None:
    for table in ("ads", "conversations"):
        execute(f"DELETE FROM {table} WHERE account_id = ?", (account_id,))
    execute("DELETE FROM templates WHERE account_id = ?", (account_id,))
    execute("DELETE FROM accounts WHERE id = ?", (account_id,))


# ---------- templates ----------

def list_templates() -> list[dict]:
    return query("SELECT * FROM templates ORDER BY priority DESC, id")


def get_template(template_id: int) -> dict | None:
    return query_one("SELECT * FROM templates WHERE id = ?", (template_id,))


def save_template(template_id: int | None, **fields) -> int:
    if template_id:
        cols = ", ".join(f"{k} = ?" for k in fields)
        execute(f"UPDATE templates SET {cols} WHERE id = ?", (*fields.values(), template_id))
        return template_id
    cols = ", ".join(fields)
    marks = ", ".join("?" for _ in fields)
    return int(execute(f"INSERT INTO templates ({cols}) VALUES ({marks})", tuple(fields.values())).lastrowid)


def delete_template(template_id: int) -> None:
    execute("DELETE FROM templates WHERE id = ?", (template_id,))


# ---------- ads ----------

def upsert_ad(account_id: int, ad_id: str, title: str, price: str = "", url: str = "") -> None:
    execute(
        "INSERT INTO ads (account_id, ad_id, title, price, url, updated_at) VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(account_id, ad_id) DO UPDATE SET "
        "title = CASE WHEN excluded.title != '' THEN excluded.title ELSE ads.title END, "
        "price = CASE WHEN excluded.price != '' THEN excluded.price ELSE ads.price END, "
        "url = CASE WHEN excluded.url != '' THEN excluded.url ELSE ads.url END, "
        "updated_at = excluded.updated_at",
        (account_id, ad_id, title, price, url, time.time()),
    )


def get_ad(account_id: int, ad_id: str) -> dict | None:
    return query_one("SELECT * FROM ads WHERE account_id = ? AND ad_id = ?", (account_id, ad_id))


def list_ads(account_id: int | None = None) -> list[dict]:
    if account_id is None:
        return query("SELECT * FROM ads ORDER BY account_id, title")
    return query("SELECT * FROM ads WHERE account_id = ? ORDER BY title", (account_id,))


# ---------- conversations ----------

def get_conversation(account_id: int, conversation_id: str) -> dict | None:
    return query_one(
        "SELECT * FROM conversations WHERE account_id = ? AND conversation_id = ?",
        (account_id, conversation_id),
    )


def upsert_conversation(account_id: int, conversation_id: str, **fields) -> None:
    fields["updated_at"] = time.time()
    if get_conversation(account_id, conversation_id):
        cols = ", ".join(f"{k} = ?" for k in fields)
        execute(
            f"UPDATE conversations SET {cols} WHERE account_id = ? AND conversation_id = ?",
            (*fields.values(), account_id, conversation_id),
        )
    else:
        fields.update(account_id=account_id, conversation_id=conversation_id)
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        execute(f"INSERT INTO conversations ({cols}) VALUES ({marks})", tuple(fields.values()))


def list_conversations(limit: int = 200) -> list[dict]:
    return query(
        "SELECT c.*, a.name AS account_name, t.name AS template_name FROM conversations c "
        "LEFT JOIN accounts a ON a.id = c.account_id "
        "LEFT JOIN templates t ON t.id = c.template_id "
        "ORDER BY c.updated_at DESC LIMIT ?",
        (limit,),
    )


# ---------- log ----------

def add_log(account_id: int | None, level: str, message: str) -> None:
    execute(
        "INSERT INTO log (ts, account_id, level, message) VALUES (?, ?, ?, ?)",
        (time.time(), account_id, level, message),
    )
    # держим лог компактным
    execute("DELETE FROM log WHERE id <= (SELECT MAX(id) FROM log) - 5000")


def list_log(limit: int = 300, account_id: int | None = None) -> list[dict]:
    if account_id is None:
        return query(
            "SELECT l.*, a.name AS account_name FROM log l LEFT JOIN accounts a ON a.id = l.account_id "
            "ORDER BY l.id DESC LIMIT ?",
            (limit,),
        )
    return query(
        "SELECT l.*, a.name AS account_name FROM log l LEFT JOIN accounts a ON a.id = l.account_id "
        "WHERE l.account_id = ? ORDER BY l.id DESC LIMIT ?",
        (account_id, limit),
    )
