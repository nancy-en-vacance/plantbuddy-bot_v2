import os
from typing import List, Tuple, Optional
from datetime import datetime, timezone, date
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
        conn.commit()


# ---------- helpers ----------
def list_plants(user_id: int) -> List[Tuple[int, str]]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, name FROM plants WHERE user_id=%s AND active=TRUE ORDER BY id;",
                (user_id,),
            )
            return [(int(r[0]), r[1]) for r in cur.fetchall()]


def set_last_watered_bulk(
    user_id: int, updates: dict[int, datetime]
) -> List[str]:
    """
    updates: {plant_id: datetime}
    """
    applied = []
    with get_conn() as conn:
        with conn.cursor() as cur:
            for plant_id, dt in updates.items():
                cur.execute(
                    """
                    UPDATE plants
                    SET last_watered_at = %s
                    WHERE user_id=%s AND id=%s AND active=TRUE
                    """,
                    (dt, user_id, plant_id),
                )
                if cur.rowcount == 1:
                    applied.append(str(plant_id))
        conn.commit()
    return applied
