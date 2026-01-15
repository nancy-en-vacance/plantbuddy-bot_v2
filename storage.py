import os
from datetime import datetime, timedelta, date
from typing import List, Tuple, Dict, Optional
import psycopg

DATABASE_URL = os.environ["DATABASE_URL"]


def get_conn():
    return psycopg.connect(DATABASE_URL)


def init_db():
    """
    Создаёт таблицы, если их нет.
    Никаких миграций/ALTER TABLE в рантайме — только CREATE IF NOT EXISTS.
    """
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS plants (
            id SERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL,
            name TEXT NOT NULL,
            water_every_days INT,
            last_watered_at TIMESTAMPTZ,
            active BOOLEAN DEFAULT TRUE,
            UNIQUE(user_id, name)
        );
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS reminder_state (
            user_id BIGINT PRIMARY KEY,
            last_sent_local_date DATE
        );
        """)

        # New: plant_photos (lightweight, stores only Telegram file ids + metadata)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS plant_photos (
            id SERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL,
            plant_id INT NOT NULL REFERENCES plants(id) ON DELETE CASCADE,
            tg_file_id TEXT NOT NULL,
            tg_file_unique_id TEXT,
            caption TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """)

        conn.commit()


# ---------- plants ----------
def add_plant(user_id: int, name: str):
    name = (name or "").strip()
    if not name:
        return
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""
        INSERT INTO plants (user_id, name)
        VALUES (%s, %s)
        ON CONFLICT (user_id, name) DO NOTHING
        """, (user_id, name))
        conn.commit()


def list_plants(user_id: int) -> List[Tuple[int, str]]:
    """Только активные растения."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""
        SELECT id, name FROM plants
        WHERE user_id=%s AND active=TRUE
        ORDER BY id
        """, (user_id,))
        return cur.fetchall()


def list_plants_archived(user_id: int) -> List[Tuple[int, str]]:
    """Только архивные (active=FALSE)."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""
        SELECT id, name FROM plants
        WHERE user_id=%s AND active=FALSE
        ORDER BY id
        """, (user_id,))
        return cur.fetchall()


def set_active(user_id: int, plant_id: int, active: bool) -> bool:
    """Переключает active. Возвращает True если обновилось 1 растение."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""
        UPDATE plants
        SET active=%s
        WHERE id=%s AND user_id=%s
        """, (active, plant_id, user_id))
        conn.commit()
        return cur.rowcount == 1


def rename_plant(user_id: int, plant_id: int, new_name: str) -> bool:
    new_name = (new_name or "").strip()
    if not new_name:
        return False

    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("""
            UPDATE plants
            SET name=%s
            WHERE id=%s AND user_id=%s AND active=TRUE
            """, (new_name, plant_id, user_id))
            conn.commit()
            return cur.rowcount == 1
    except psycopg.errors.UniqueViolation:
        return False


def set_norm(user_id: int, plant_id: int, days: int) -> bool:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""
        UPDATE plants
        SET water_every_days=%s
        WHERE id=%s AND user_id=%s
        """, (days, plant_id, user_id))
        conn.commit()
        return cur.rowcount == 1


def get_norms(user_id: int) -> List[Tuple[str, int]]:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""
        SELECT name, water_every_days
        FROM plants
        WHERE user_id=%s AND active=TRUE AND water_every_days IS NOT NULL
        ORDER BY name
        """, (user_id,))
        return cur.fetchall()


# ---------- watering ----------
def log_water(user_id: int, plant_id: int, when: datetime) -> bool:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""
        UPDATE plants
        SET last_watered_at=%s
        WHERE id=%s AND user_id=%s AND active=TRUE
        """, (when, plant_id, user_id))
        conn.commit()
        return cur.rowcount == 1


def log_water_many(user_id: int, plant_ids: List[int], when: datetime) -> int:
    if not plant_ids:
        return 0

    updated = 0
    with get_conn() as conn, conn.cursor() as cur:
        for pid in plant_ids:
            cur.execute("""
            UPDATE plants
            SET last_watered_at=%s
            WHERE id=%s AND user_id=%s AND active=TRUE
            """, (when, pid, user_id))
            if cur.rowcount == 1:
                updated += 1
        conn.commit()
    return updated


def set_last_watered_bulk(user_id: int, updates: Dict[int, datetime]) -> int:
    updated = 0
    with get_conn() as conn, conn.cursor() as cur:
        for plant_id, dt in updates.items():
            cur.execute("""
            UPDATE plants
            SET last_watered_at=%s
            WHERE id=%s AND user_id=%s AND active=TRUE
            """, (dt, plant_id, user_id))
            if cur.rowcount == 1:
                updated += 1
        conn.commit()
    return updated


# ---------- today logic ----------
def compute_today(user_id: int, today: date):
    overdue = []
    today_list = []
    unknown = []

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""
        SELECT name, water_every_days, last_watered_at
        FROM plants
        WHERE user_id=%s AND active=TRUE
        """, (user_id,))
        rows = cur.fetchall()

    for name, every, last in rows:
        if not every or not last:
            unknown.append(name)
            continue

        due = last.date() + timedelta(days=int(every))

        if due < today:
            overdue.append((name, (today - due).days))
        elif due == today:
            today_list.append(name)

    return overdue, today_list, unknown


# ---------- reminders ----------
def get_last_sent(user_id: int) -> Optional[date]:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""
        SELECT last_sent_local_date FROM reminder_state WHERE user_id=%s
        """, (user_id,))
        row = cur.fetchone()
        return row[0] if row else None


def set_last_sent(user_id: int, d: date):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""
        INSERT INTO reminder_state (user_id, last_sent_local_date)
        VALUES (%s, %s)
        ON CONFLICT (user_id)
        DO UPDATE SET last_sent_local_date=%s
        """, (user_id, d, d))
        conn.commit()


# ---------- photos ----------
def add_plant_photo(
    user_id: int,
    plant_id: int,
    tg_file_id: str,
    tg_file_unique_id: Optional[str] = None,
    caption: Optional[str] = None,
) -> int:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO plant_photos (user_id, plant_id, tg_file_id, tg_file_unique_id, caption)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id
            """,
            (user_id, plant_id, tg_file_id, tg_file_unique_id, caption),
        )
        row_id = cur.fetchone()[0]
        conn.commit()
        return row_id


def list_plant_photos(
    user_id: int,
    plant_id: int,
    limit: int = 10,
) -> List[Tuple[int, str, Optional[str], datetime]]:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, tg_file_id, caption, created_at
            FROM plant_photos
            WHERE user_id=%s AND plant_id=%s
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (user_id, plant_id, limit),
        )
        return cur.fetchall()



def get_plant_context(user_id: int, plant_id: int):
    """
    Returns (name, water_every_days, last_watered_at) for active plant.
    """
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT name, water_every_days, last_watered_at
            FROM plants
            WHERE user_id=%s AND id=%s AND active=TRUE
            """,
            (user_id, plant_id),
        )
        return cur.fetchone()


def get_last_photo_for_plant(user_id: int, plant_id: int):
    """
    Returns latest photo row: (id, tg_file_id, tg_file_unique_id, caption, created_at) or None.
    """
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, tg_file_id, tg_file_unique_id, caption, created_at
            FROM plant_photos
            WHERE user_id=%s AND plant_id=%s
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (user_id, plant_id),
        )
        return cur.fetchone()


# ---------- diagnostics ----------
def db_check(user_id: int) -> int:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM plants WHERE user_id=%s", (user_id,))
        return cur.fetchone()[0]
