import os
from typing import List, Tuple, Optional
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
                    water_every_days INTEGER NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE(user_id, name)
                );
                """
            )

            # мягкая миграция: если таблица была создана ранее без water_every_days
            cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name='plants';")
            cols = {r[0] for r in cur.fetchall()}
            if "water_every_days" not in cols:
                cur.execute("ALTER TABLE plants ADD COLUMN water_every_days INTEGER NULL;")

        conn.commit()


def add_plant(user_id: int, name: str) -> bool:
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO plants (user_id, name, active) VALUES (%s, %s, TRUE);",
                    (user_id, name),
                )
            conn.commit()
        return True
    except Exception:
        return False


def list_plants(user_id: int, active_only: bool = True) -> List[Tuple[int, str]]:
    query = "SELECT id, name FROM plants WHERE user_id = %s"
    params = [user_id]
    if active_only:
        query += " AND active = TRUE"
    query += " ORDER BY id"

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            rows = cur.fetchall()

    return [(int(r[0]), str(r[1])) for r in rows]


def list_plants_with_norms(user_id: int, active_only: bool = True) -> List[Tuple[int, str, Optional[int]]]:
    query = "SELECT id, name, water_every_days FROM plants WHERE user_id = %s"
    params = [user_id]
    if active_only:
        query += " AND active = TRUE"
    query += " ORDER BY id"

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            rows = cur.fetchall()

    out = []
    for r in rows:
        pid = int(r[0])
        name = str(r[1])
        every = int(r[2]) if r[2] is not None else None
        out.append((pid, name, every))
    return out


def rename_plant(user_id: int, plant_id: int, new_name: str) -> bool:
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE plants
                    SET name = %s
                    WHERE user_id = %s AND id = %s AND active = TRUE
                    """,
                    (new_name, user_id, plant_id),
                )
                updated = cur.rowcount == 1
            conn.commit()
        return updated
    except Exception:
        return False


def archive_plant(user_id: int, plant_id: int) -> bool:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE plants
                SET active = FALSE
                WHERE user_id = %s AND id = %s AND active = TRUE
                """,
                (user_id, plant_id),
            )
            updated = cur.rowcount == 1
        conn.commit()
    return updated


def set_norm_days(user_id: int, plant_id: int, days: int) -> bool:
    if days <= 0:
        return False
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE plants
                SET water_every_days = %s
                WHERE user_id = %s AND id = %s AND active = TRUE
                """,
                (days, user_id, plant_id),
            )
            updated = cur.rowcount == 1
        conn.commit()
    return updated


def count_plants(user_id: int) -> int:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM plants WHERE user_id = %s;", (user_id,))
            return int(cur.fetchone()[0])
