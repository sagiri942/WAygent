"""用户记忆读写：偏好提取、行程保存、历史查询。"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any


# ─── 行程保存 / 查询 ─────────────────────────────────────────

def _core_connection(conn: Any) -> bool:
    if isinstance(conn, sqlite3.Connection):
        return False
    from sqlalchemy.engine import Connection
    if not isinstance(conn, Connection):
        raise TypeError("expected SQLite or SQLAlchemy Core Connection")
    return True


def _itineraries():
    from sqlalchemy import table, column
    return table("itineraries", *(column(name) for name in (
        "id", "user_id", "parent_id", "query", "modification_notes", "destination",
        "start_date", "end_date", "plan_json", "planner_state_json", "root_id",
        "version", "lock_version", "created_at")))


def itinerary_owner(plan_id: str, conn: Any) -> str | None:
    """Return the persisted owner through either supported storage seam."""
    if _core_connection(conn):
        from sqlalchemy import select
        table = _itineraries()
        row = conn.execute(select(table.c.user_id).where(table.c.id == plan_id)).first()
        return str(row[0]) if row else None
    row = conn.execute("SELECT user_id FROM itineraries WHERE id=?", (plan_id,)).fetchone()
    return str(row["user_id"]) if row else None


def get_itinerary_record(plan_id: str, user_id: str, conn: Any) -> dict[str, Any] | None:
    """Read the existing itinerary fields used by chat and edit APIs."""
    if _core_connection(conn):
        from sqlalchemy import select
        table = _itineraries()
        row = conn.execute(select(table).where(
            table.c.id == plan_id, table.c.user_id == user_id
        )).mappings().first()
    else:
        row = conn.execute(
            "SELECT id,user_id,parent_id,root_id,version,lock_version,query,modification_notes,"
            "destination,start_date,end_date,created_at,plan_json,planner_state_json "
            "FROM itineraries WHERE id=? AND user_id=?", (plan_id, user_id),
        ).fetchone()
    return dict(row) if row else None


def list_itinerary_records(user_id: str, conn: Any) -> list[dict[str, Any]]:
    """Read saved itinerary rows for the chat's private history tools."""
    if _core_connection(conn):
        from sqlalchemy import select
        table = _itineraries()
        rows = conn.execute(select(table).where(table.c.user_id == user_id)).mappings().all()
    else:
        rows = conn.execute(
            "SELECT id,user_id,parent_id,root_id,version,lock_version,query,modification_notes,"
            "destination,start_date,end_date,created_at,plan_json,planner_state_json "
            "FROM itineraries WHERE user_id=?", (user_id,),
        ).fetchall()
    return [dict(row) for row in rows]


class ItineraryVersionConflict(ValueError):
    """The requested base itinerary is stale or has already been revised."""


def save_itinerary(
    user_id: str,
    plan: dict,
    query: str,
    conn: sqlite3.Connection,
    *,
    parent_id: str | None = None,
    modification_notes: str | None = None,
    planner_state: dict | None = None,
    expected_parent_lock_version: int | None = None,
    require_latest_parent: bool = False,
) -> str:
    plan_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    root_id = plan_id
    version = 1
    if parent_id:
        # SQLite SELECTs do not begin a transaction. Reserve the write before
        # reading the guarded parent/latest version so competing children cannot
        # both approve the same stale snapshot before their first INSERT.
        if require_latest_parent and not _core_connection(conn) and not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        if _core_connection(conn):
            from sqlalchemy import select
            t = _itineraries()
            # Every child-version writer locks root before parent.  The read is
            # only to discover the immutable root id; its lock is acquired next.
            hint = conn.execute(select(t.c.id, t.c.root_id).where(
                t.c.id == parent_id, t.c.user_id == user_id
            )).mappings().first()
            if not hint:
                raise ValueError("base itinerary not found or not owned by user")
            root_hint = hint["root_id"] or hint["id"]
            conn.execute(select(t.c.id).where(t.c.id == root_hint).with_for_update()).first()
            parent = conn.execute(select(
                t.c.id, t.c.root_id, t.c.version, t.c.lock_version
            ).where(
                t.c.id == parent_id, t.c.user_id == user_id
            ).with_for_update()).mappings().first()
        else:
            hint = conn.execute(
                "SELECT id,root_id FROM itineraries WHERE id=? AND user_id=?",
                (parent_id, user_id),
            ).fetchone()
            if not hint:
                raise ValueError("base itinerary not found or not owned by user")
            root_hint = hint["root_id"] or hint["id"]
            conn.execute("SELECT id FROM itineraries WHERE id=?", (root_hint,)).fetchone()
            parent = conn.execute(
                "SELECT id,root_id,version,lock_version FROM itineraries WHERE id=? AND user_id=?",
                (parent_id, user_id),
            ).fetchone()
        if not parent:
            raise ValueError("base itinerary not found or not owned by user")
        root_id = parent["root_id"] or parent["id"]
        if (
            expected_parent_lock_version is not None
            and int(parent["lock_version"] or 1) != expected_parent_lock_version
        ):
            raise ItineraryVersionConflict("base itinerary changed while the revision was being prepared")
        if require_latest_parent:
            if _core_connection(conn):
                from sqlalchemy import select
                t = _itineraries()
                # The root lock was acquired before the parent lock above; this
                # locking read sees the latest committed child under REPEATABLE READ.
                latest = conn.execute(select(t.c.id, t.c.version).where(
                    t.c.root_id == root_id
                ).order_by(t.c.version.desc(), t.c.created_at.desc(), t.c.id.desc())
                 .limit(1).with_for_update()).mappings().first()
            else:
                latest = conn.execute(
                    "SELECT id,version FROM itineraries WHERE root_id=? "
                    "ORDER BY version DESC,created_at DESC,id DESC LIMIT 1",
                    (root_id,),
                ).fetchone()
            if not latest or str(latest["id"]) != str(parent_id):
                raise ItineraryVersionConflict("base itinerary is no longer the latest version")
        version = int(parent["version"] or 1) + 1
    if _core_connection(conn):
        conn.execute(_itineraries().insert().values(
            id=plan_id, user_id=user_id, parent_id=parent_id, query=query,
            modification_notes=modification_notes, destination=plan.get("destination", ""),
            start_date=plan.get("start_date", ""), end_date=plan.get("end_date", ""),
            plan_json=json.dumps(plan, ensure_ascii=False),
            planner_state_json=json.dumps(planner_state, ensure_ascii=False) if planner_state else None,
            root_id=root_id, version=version, created_at=now))
        return plan_id
    conn.execute(
        """INSERT INTO itineraries
           (id, user_id, parent_id, query, modification_notes,
            destination, start_date, end_date, plan_json, planner_state_json,
            root_id, version, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            plan_id, user_id, parent_id, query, modification_notes,
            plan.get("destination", ""),
            plan.get("start_date", ""),
            plan.get("end_date", ""),
            json.dumps(plan, ensure_ascii=False),
            json.dumps(planner_state, ensure_ascii=False) if planner_state else None,
            root_id,
            version,
            now,
        ),
    )
    return plan_id


def load_itinerary(plan_id: str, conn: sqlite3.Connection) -> dict | None:
    if _core_connection(conn):
        from sqlalchemy import select
        t = _itineraries()
        row = conn.execute(select(t).where(t.c.id == plan_id)).mappings().first()
    else:
        row = conn.execute(
            "SELECT id,parent_id,root_id,version,lock_version,plan_json,modification_notes,planner_state_json "
            "FROM itineraries WHERE id=?",
            (plan_id,),
        ).fetchone()
    if not row:
        return None
    return {
        "plan": json.loads(row["plan_json"]),
        "id": row["id"],
        "parent_id": row["parent_id"],
        "root_id": row["root_id"] or row["id"],
        "version": int(row["version"] or 1),
        "lock_version": int(row["lock_version"] or 1),
        "modification_notes": row["modification_notes"],
        "planner_state": json.loads(row["planner_state_json"]) if row["planner_state_json"] else None,
    }


def update_plan_json(
    plan_id: str,
    user_id: str,
    new_plan: dict,
    conn: sqlite3.Connection,
    *,
    expected_lock_version: int,
    new_planner_state: dict | None = None,
) -> bool:
    """用乐观锁更新行程；版本已变化时不覆盖其他请求的编辑。"""
    if _core_connection(conn):
        t = _itineraries()
        values = dict(plan_json=json.dumps(new_plan, ensure_ascii=False), lock_version=t.c.lock_version + 1)
        if new_planner_state is not None:
            values["planner_state_json"] = json.dumps(new_planner_state, ensure_ascii=False, default=str)
        cur = conn.execute(t.update().where(t.c.id == plan_id, t.c.user_id == user_id,
            t.c.lock_version == expected_lock_version).values(**values))
        return cur.rowcount > 0
    state_assignment = ",planner_state_json=?" if new_planner_state is not None else ""
    values = [json.dumps(new_plan, ensure_ascii=False)]
    if new_planner_state is not None:
        values.append(json.dumps(new_planner_state, ensure_ascii=False, default=str))
    cur = conn.execute(
        "UPDATE itineraries SET plan_json=?,lock_version=lock_version+1" + state_assignment + " "
        "WHERE id=? AND user_id=? AND lock_version=?",
        (*values, plan_id, user_id, expected_lock_version),
    )
    return cur.rowcount > 0


def list_itineraries(user_id: str, conn: sqlite3.Connection) -> list[dict[str, Any]]:
    if _core_connection(conn):
        from sqlalchemy import select
        t = _itineraries()
        rows = conn.execute(select(*(t.c[name] for name in (
            "id", "parent_id", "root_id", "version", "destination", "start_date", "end_date", "created_at"
        ))).where(t.c.user_id == user_id).order_by(t.c.created_at.desc()).limit(50)).mappings().all()
        return [dict(r) for r in rows]
    rows = conn.execute(
        """SELECT id, parent_id, root_id, version, destination, start_date, end_date, created_at
           FROM itineraries WHERE user_id=? ORDER BY created_at DESC LIMIT 50""",
        (user_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def list_itineraries_page(
    user_id: str,
    conn: sqlite3.Connection,
    *,
    limit: int,
    cursor: tuple[str, str] | None = None,
) -> tuple[list[dict[str, Any]], tuple[str, str] | None]:
    """Return a stable, cursor-paginated history slice ordered newest first."""
    if _core_connection(conn):
        from sqlalchemy import select, or_, and_
        t = _itineraries()
        statement = select(*(t.c[name] for name in (
            "id", "parent_id", "root_id", "version", "destination", "start_date", "end_date", "created_at"
        ))).where(t.c.user_id == user_id)
        if cursor:
            statement = statement.where(or_(t.c.created_at < cursor[0],
                and_(t.c.created_at == cursor[0], t.c.id < cursor[1])))
        rows = conn.execute(statement.order_by(t.c.created_at.desc(), t.c.id.desc()).limit(limit + 1)).mappings().all()
        items = [dict(row) for row in rows[:limit]]
        next_cursor = (str(items[-1]["created_at"]), str(items[-1]["id"])) if len(rows) > limit and items else None
        return items, next_cursor
    params: list[Any] = [user_id]
    where = "WHERE user_id=?"
    if cursor:
        where += " AND (created_at < ? OR (created_at = ? AND id < ?))"
        params.extend([cursor[0], cursor[0], cursor[1]])
    params.append(limit + 1)
    rows = conn.execute(
        """SELECT id, parent_id, root_id, version, destination, start_date, end_date, created_at
           FROM itineraries """ + where + " ORDER BY created_at DESC, id DESC LIMIT ?",
        params,
    ).fetchall()
    has_more = len(rows) > limit
    items = [dict(row) for row in rows[:limit]]
    next_cursor = None
    if has_more and items:
        tail = items[-1]
        next_cursor = (str(tail["created_at"]), str(tail["id"]))
    return items, next_cursor


def summarize_plan_for_prompt(plan: dict) -> str:
    """提取行程摘要用于 planner 修改模式的 prompt 注入。"""
    lines = [f"目的地：{plan.get('destination', '')}，{plan.get('start_date', '')} 至 {plan.get('end_date', '')}"]
    for day in plan.get("days", []):
        spots = [t["name"] for t in day.get("timeline", []) if t.get("type") == "attraction"]
        lines.append(f"第{day['day']}天（{day.get('date', '')}）：{'、'.join(spots) or '无景点'}")
    return "\n".join(lines)
