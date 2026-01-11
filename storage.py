import sqlite3
from pathlib import Path
from typing import List, Tuple

DB_PATH = Path("data.db")


def get_conn() -> sqlite3.Connection:
    # check_same_thread=False обычно не нужен, но на webhooks может помочь в некоторых окружениях
    return sqlite3.connect(DB_PATH)


def init_db() -> None:
    with get_conn() as conn:
        # базовая таблица (создастся если её нет)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS plants (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT DEFAULT (datetime('now')),
                UNIQUE(user_id, name)
            )
            """
        )

        # мягкая миграция: если таблица была создана ранее без новых колонок
        cols = {row[1] for row in conn.execute("PRAGMA table_info(plants)").fetchall()}
        if "active" not in cols:
            conn.execute("ALTER TABLE plants ADD COLUMN active INTEGER NOT NULL DEFAULT 1")
        if "created_at" not in cols:
            conn.execute("ALTER TABLE plants ADD COLUMN created_at TEXT DEFAULT (datetime('now'))")


# ---------- Plants CRUD ----------

def add_plant(user_id: int, name: str) -> bool:
    """True если добавили, False если уже есть/ошибка."""
    try:
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO plants (user_id, name, active) VALUES (?, ?, 1)",
                (user_id, name),
            )
        return True
    except Exception:
        return False


def list_plants(user_id: int, active_only: bool = True) -> List[Tuple[int, str]]:
    query = "SELECT id, name FROM plants WHERE user_id = ?"
    params = [user_id]
    if active_only:
        query += " AND active = 1"
    query += " ORDER BY id"

    with get_conn() as conn:
        rows = conn.execute(query, params).fetchall()

    return [(int(r[0]), str(r[1])) for r in rows]


def rename_plant(user_id: int, plant_id: int, new_name: str) -> bool:
    """True если переименовали, False если не нашли/дубликат/ошибка."""
    try:
        with get_conn() as conn:
            cur = conn.execute(
                "UPDATE plants SET name = ? WHERE user_id = ? AND id = ? AND active = 1",
                (new_name, user_id, plant_id),
            )
        return cur.rowcount == 1
    except Exception:
        return False


def archive_plant(user_id: int, plant_id: int) -> bool:
    """True если архивировали, False если не нашли."""
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE plants SET active = 0 WHERE user_id = ? AND id = ? AND active = 1",
            (user_id, plant_id),
        )
    return cur.rowcount == 1


def restore_plant(user_id: int, plant_id: int) -> bool:
    """На будущее: вернуть из архива."""
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE plants SET active = 1 WHERE user_id = ? AND id = ? AND active = 0",
            (user_id, plant_id),
        )
    return cur.rowcount == 1
