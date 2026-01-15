import os
from datetime import datetime, timedelta, date
from typing import List, Tuple, Dict, Optional
import psycopg

DATABASE_URL = os.environ["DATABASE_URL"]


def get_conn():
    return psycopg.connect(DATABASE_URL)


def init_db():
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

        conn.commit()


# ---------- plants ----------
def add_plant(user_id: int, name: str):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""
        INSERT INTO plants (user_id, name)
        VALUES (%s, %s)
        ON CONFLICT (user_id, name) DO NOTHING
        """, (user_id, name))
        conn.commit()


def list_plants(user_id: int) -> List[Tuple[int, str]]:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""
        SELECT id, name FROM plants
        WHERE user_id=%s AND active=TRUE
        ORDER BY id
        """, (user_id,))
        return cur.fetchall()


def rename_plant(user_id: int, plant_id: int, new_name: str) -> bool:
    """
    Переименовать растение (только active=TRUE).
    Возвращает True если обновилось, иначе False.
    False может означать: нет такого plant_id у юзера, растение не активно,
    или имя уже занято (UNIQUE(user_id, name)).
    """
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
        # имя уже есть у этого user_id
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
        WHERE user_id=%s AND water_every_days IS NOT NULL
        ORDER BY name
        """, (user_id,))
        return cur.fetchall()


# ---------- watering ----------
def log_water(user_id: int, plant_id: int, when: datetime) -> bool:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""
        UPDATE plants
        SET last_watered_at=%s
        WHERE id=%s AND user_id=%s
        """, (when, plant_id, user_id))
        conn.commit()
        return cur.rowcount == 1


def log_water_many(user_id: int, plant_ids: List[int], when: datetime) -> int:
    """
    Обновляет last_watered_at для списка растений.
    Возвращает сколько реально обновилось (принадлежит user_id).
    """
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


def db_check(user_id: int):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM plants WHERE user_id=%s", (user_id,))
        return cur.fetchone()[0]
