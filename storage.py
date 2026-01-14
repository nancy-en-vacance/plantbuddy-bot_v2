import os
from typing import List, Tuple, Optional
from datetime import datetime, timezone
import psycopg

DATABASE_URL = os.environ["DATABASE_URL"]


def get_conn() -> psycopg.Connection:
    return psycopg.connect(DATABASE_URL)


def _ensure_column(cur, table: str, column: str, ddl: str) -> None:
    cur.execute(
        """
        SELECT 1
        FROM information_schema.columns
        WHERE table_name = %s AND column_name = %s
        """,
        (table, column),
    )
    exists = cur.fetchone() is not None
    if not exists:
        cur.execute(ddl)


def init_db() -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            # plants
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS plants (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    name TEXT NOT NULL,
                    active BOOLEAN NOT NULL DEFAULT TRUE,
                    water_every_days INTEGER NULL,
                    last_watered_at TIMESTAMPTZ NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE(user_id, name)
                );
                """
            )

            # мягкая миграция (если таблица уже была)
            _ensure_column(
                cur,
                "plants",
                "water_every_days",
                "ALTER TABLE plants ADD COLUMN water_every_days INTEGER NULL;",
            )
            _ensure_column(
                cur,
                "plants",
                "last_watered_at",
                "ALTER TABLE plants ADD COLUMN last_watered_at TIMESTAMPTZ NULL;",
            )

            # water_logs
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
            cur.execute("CREATE INDEX IF NOT EXISTS idx_water_logs_user_time ON water_logs(user_id, watered_at);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_water_logs_plant_time ON water_logs(plant_id, watered_at);")

        conn.commit()


# ---------- Plants CRUD ----------
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


def list_plants_with_last_watered(user_id: int, active_only: bool = True) -> List[Tuple[int, str, Optional[datetime]]]:
    query = "SELECT id, name, last_watered_at FROM plants WHERE user_id = %s"
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
        last = r[2]  # datetime or None
        out.append((pid, name, last))
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


# ---------- Watering ----------
def mark_watered(user_id: int, plant_ids: List[int], watered_at: Optional[datetime] = None) -> int:
    """
    Обновляет last_watered_at и пишет water_logs для списка plant_ids (только активные растения этого user_id).
    Возвращает число реально обновлённых растений.
    """
    if not plant_ids:
        return 0

    ts = watered_at or datetime.now(timezone.utc)

    with get_conn() as conn:
        with conn.cursor() as cur:
            # берём только те plant_id, которые принадлежат user_id и active=TRUE
            cur.execute(
                """
                SELECT id
                FROM plants
                WHERE user_id = %s AND active = TRUE AND id = ANY(%s)
                """,
                (user_id, plant_ids),
            )
            allowed = [int(r[0]) for r in cur.fetchall()]
            if not allowed:
                conn.commit()
                return 0

            # update last_watered_at
            cur.execute(
                """
                UPDATE plants
                SET last_watered_at = %s
                WHERE user_id = %s AND active = TRUE AND id = ANY(%s)
                """,
                (ts, user_id, allowed),
            )

            # insert logs
            cur.executemany(
                "INSERT INTO water_logs (user_id, plant_id, watered_at) VALUES (%s, %s, %s);",
                [(user_id, pid, ts) for pid in allowed],
            )

        conn.commit()

    return len(allowed)


def count_plants(user_id: int) -> int:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM plants WHERE user_id = %s;", (user_id,))
            return int(cur.fetchone()[0])
