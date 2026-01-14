import os
from typing import List, Tuple, Optional
from datetime import datetime, timedelta, timezone
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
        conn.commit()


def list_plants_today(user_id: int):
    """
    Возвращает кортежи списков:
    overdue, today, upcoming, unknown
    """
    now = datetime.now(timezone.utc).date()

    overdue = []
    today = []
    upcoming = []
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

        last_date = last.date()
        due_date = last_date + timedelta(days=every)
        delta = (due_date - now).days

        if delta < 0:
            overdue.append((name, abs(delta)))
        elif delta == 0:
            today.append(name)
        else:
            upcoming.append((name, delta))

    return overdue, today, upcoming, unknown
