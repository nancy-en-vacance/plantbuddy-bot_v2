import os
from datetime import datetime, timezone, timedelta, date
from typing import List, Tuple, Dict, Optional
import psycopg

DATABASE_URL = os.environ["DATABASE_URL"]


def get_conn() -> psycopg.Connection:
    return psycopg.connect(DATABASE_URL)


def init_db() -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS plants (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    name TEXT NOT NULL,
                    active BOOLEAN NOT NULL DEFAULT TRUE,
                    water_every_days INTEGER,
                    last_watered_at TIMESTAMPTZ,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE(user_id, name)
                );
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS water_logs (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    plant_id BIGINT NOT NULL REFERENCES plants(id) ON DELETE CASCADE,
                    watered_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                """
            )

            # хранит "мы уже отправляли авто-сводку за эту локальную дату"
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS reminder_state (
                    user_id BIGINT PRIMARY KEY,
                    last_sent_local_date DATE
                );
                """
            )
        conn.commit()


# -------- Plants CRUD --------
def add_plant(user_id: int, name: str) -> None:
    name = name.strip()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO plants (user_id, name, active)
                VALUES (%s, %s, TRUE)
                ON CONFLICT (user_id, name) DO UPDATE
                SET active = TRUE
                """,
                (user_id, name),
            )
        conn.commit()


def list_plants(user_id: int) -> List[Tuple[int, str]]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, name FROM plants WHERE user_id=%s AND active=TRUE ORDER BY id;",
                (user_id,),
            )
            return [(int(r[0]), r[1]) for r in cur.fetchall()]


def set_norm(user_id: int, plant_id: int, days: int) -> bool:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE plants
                SET water_every_days=%s
                WHERE user_id=%s AND id=%s AND active=TRUE
                """,
                (days, user_id, plant_id),
            )
            ok = cur.rowcount == 1
        conn.commit()
    return ok


def get_norms(user_id: int) -> List[Tuple[str, int]]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT name, water_every_days
                FROM plants
                WHERE user_id=%s AND active=TRUE AND water_every_days IS NOT NULL
                ORDER BY name
                """,
                (user_id,),
            )
            return [(r[0], int(r[1])) for r in cur.fetchall()]


# -------- Water logging --------
def log_water(user_id: int, plant_id: int, when_utc: datetime) -> bool:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE plants
                SET last_watered_at=%s
                WHERE user_id=%s AND id=%s AND active=TRUE
                """,
                (when_utc, user_id, plant_id),
            )
            ok = cur.rowcount == 1

            if ok:
                cur.execute(
                    """
                    INSERT INTO water_logs (user_id, plant_id, watered_at)
                    VALUES (%s, %s, %s)
                    """,
                    (user_id, plant_id, when_utc),
                )
        conn.commit()
    return ok


def set_last_watered_bulk(user_id: int, updates: Dict[int, datetime]) -> int:
    """
    updates: {plant_id: datetime(UTC)}
    """
    count = 0
    with get_conn() as conn:
        with conn.cursor() as cur:
            for plant_id, dt in updates.items():
                cur.execute(
                    """
                    UPDATE plants
                    SET last_watered_at=%s
                    WHERE user_id=%s AND id=%s AND active=TRUE
                    """,
                    (dt, user_id, plant_id),
                )
                if cur.rowcount == 1:
                    count += 1
        conn.commit()
    return count


# -------- Reminder state --------
def get_last_sent_local_date(user_id: int) -> Optional[date]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT last_sent_local_date FROM reminder_state WHERE user_id=%s",
                (user_id,),
            )
            row = cur.fetchone()
            return row[0] if row else None


def set_last_sent_local_date(user_id: int, d: date) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO reminder_state (user_id, last_sent_local_date)
                VALUES (%s, %s)
                ON CONFLICT (user_id) DO UPDATE
                SET last_sent_local_date = EXCLUDED.last_sent_local_date
                """,
                (user_id, d),
            )
        conn.commit()


# -------- Today / overdue calculation (local date) --------
def compute_due_lists(user_id: int, local_today: date):
    """
    Возвращает:
      overdue: List[(name, days_overdue)]
      due_today: List[name]
      unknown: List[name]  (нет нормы или last_watered)
    """
    overdue = []
    due_today = []
    unknown = []

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT name, water_every_days, last_watered_at
                FROM plants
                WHERE user_id=%s AND active=TRUE
                ORDER BY name
                """,
                (user_id,),
            )
            rows = cur.fetchall()

    for name, every, last in rows:
        if every is None or last is None:
            unknown.append(name)
            continue

        last_date = last.date()  # last is timestamptz, .date() ok for our use
        due_date = last_date + timedelta(days=int(every))

        if due_date < local_today:
            days = (local_today - due_date).days
            overdue.append((name, days))
        elif due_date == local_today:
            due_today.append(name)

    return overdue, due_today, unknown


# -------- DB check --------
def db_check(user_id: int) -> Tuple[bool, int]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1;")
            cur.execute(
                "SELECT COUNT(*) FROM plants WHERE user_id=%s AND active=TRUE;",
                (user_id,),
            )
            cnt = int(cur.fetchone()[0])
    return True, cnt
