import sqlite3
from datetime import date, timedelta
from uuid import uuid4

from app.db import get_db
from app.services.errors import ServiceError

# ---------------------------------------------------------------------------
# 占片取舍（全局只贯彻这一条路线）
#
# 选边：**唯一索引 + 先核后写（挡后到）**。
#   - slice_locks.slice_key 唯一：同片键只能有一把锁；
#   - backfill_target.slice_key 唯一：同片键目标表只留一行；
#   - 事务用 BEGIN IMMEDIATE（见 app/db.py），核与写在同一个写锁内完成。
#
# 被放弃的另一条路线：旁路片锁账册原子改写后再拒二次。该路线允许重复
# 请求先把锁行/落库行写进去再靠二次判断拒掉，双份脏行会短暂甚至长期占库。
# 本产品全局不采用，任何入口（占片闸口、重放、争抢编排）都走同一条路线。
# ---------------------------------------------------------------------------

ROUND_CLAIMERS = ("调度甲", "调度乙")


def list_windows(db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT w.*, "
            "(SELECT COUNT(*) FROM backfill_slices s WHERE s.window_id=w.id) AS slice_count, "
            "(SELECT COUNT(*) FROM backfill_slices s WHERE s.window_id=w.id AND s.status='已落库') AS loaded_count "
            "FROM backfill_windows w ORDER BY w.id"
        ).fetchall()
        return [dict(r) for r in rows]


def _daterange(start: str, end: str) -> list[str]:
    s = date.fromisoformat(start)
    e = date.fromisoformat(end)
    if e < s:
        raise ServiceError("结束日不能早于开始日")
    out = []
    cur = s
    while cur <= e:
        out.append(cur.isoformat())
        cur += timedelta(days=1)
    return out


def slice_key(table_name: str, slice_date: str) -> str:
    """片键：目标表 + 日期。跨窗口指向同一天同一张表时视为同一片。"""
    return f"{table_name}#{slice_date}"


def _load_slice(conn, slice_id: int) -> dict | None:
    row = conn.execute(
        "SELECT s.*, w.table_name AS table_name, w.start_date AS window_start, "
        "w.end_date AS window_end "
        "FROM backfill_slices s JOIN backfill_windows w ON w.id=s.window_id "
        "WHERE s.id=?",
        (slice_id,),
    ).fetchone()
    if not row:
        return None
    data = dict(row)
    data["slice_key"] = slice_key(data["table_name"], data["slice_date"])
    return data


def _load_slice_brief(conn, slice_id: int) -> dict | None:
    row = conn.execute(
        "SELECT s.* FROM backfill_slices s WHERE s.id=?", (slice_id,)
    ).fetchone()
    return dict(row) if row else None


def _lock_by_key(conn, key: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM slice_locks WHERE slice_key=?", (key,)
    ).fetchone()
    return dict(row) if row else None


def _target_by_key(conn, key: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM backfill_target WHERE slice_key=?", (key,)
    ).fetchone()
    return dict(row) if row else None


def _next_attempt_no(conn) -> int:
    return conn.execute(
        "SELECT COALESCE(MAX(attempt_no), 0) + 1 FROM claim_ledger"
    ).fetchone()[0]


def _record_ledger(
    conn,
    sl: dict,
    claimer: str,
    outcome: str,
    *,
    lock_no: str | None,
    round_no: str | None,
    detail: str,
) -> dict:
    attempt_no = _next_attempt_no(conn)
    cur = conn.execute(
        "INSERT INTO claim_ledger(attempt_no, slice_id, slice_key, claimer, "
        "outcome, lock_no, reject_no, round_no, detail) "
        "VALUES (?,?,?,?,?,?,NULL,?,?)",
        (
            attempt_no,
            sl["id"],
            sl["slice_key"],
            claimer,
            outcome,
            lock_no,
            round_no,
            detail,
        ),
    )
    ledger_id = cur.lastrowid
    reject_no = f"R{ledger_id:06d}" if outcome == "拒重" else None
    if reject_no:
        conn.execute(
            "UPDATE claim_ledger SET reject_no=? WHERE id=?",
            (reject_no, ledger_id),
        )
    return {
        "ledger_id": ledger_id,
        "attempt_no": attempt_no,
        "outcome": outcome,
        "lock_no": lock_no,
        "reject_no": reject_no,
        "detail": detail,
        "round_no": round_no,
    }


def _acquire_lock(conn, sl: dict, claimer: str) -> tuple[dict, bool]:
    """返回 (锁行, 是否本次新建)。唯一索引兜住后到者。"""
    existing = _lock_by_key(conn, sl["slice_key"])
    if existing:
        return existing, False
    # 先核后写：检查放在写事务内，唯一索引再兜一次底。
    temp_no = f"T{uuid4().hex[:12]}"
    try:
        cur = conn.execute(
            "INSERT INTO slice_locks(lock_no, slice_id, slice_key, claimer, status) "
            "VALUES (?,?,?,?, '持有')",
            (temp_no, sl["id"], sl["slice_key"], claimer),
        )
    except sqlite3.IntegrityError:
        # 极端并发下先核后写仍被后到者撞上唯一索引：读回已存在锁，按拒重处理。
        return _lock_by_key(conn, sl["slice_key"]), False
    lock_no = f"L{cur.lastrowid:06d}"
    conn.execute(
        "UPDATE slice_locks SET lock_no=? WHERE id=?", (lock_no, cur.lastrowid)
    )
    return _lock_by_key(conn, sl["slice_key"]), True

def _reject(conn, sl: dict, claimer: str, lock: dict | None, reason: str,
            round_no: str | None = None) -> dict:
    lock_no = lock["lock_no"] if lock else None
    detail = f"拒重：{reason}" + (f"（片锁 {lock_no}）" if lock_no else "")
    entry = _record_ledger(
        conn,
        sl,
        claimer,
        "拒重",
        lock_no=lock_no,
        round_no=round_no,
        detail=detail,
    )
    return {
        "outcome": "拒重",
        "slice_key": sl["slice_key"],
        "slice_id": sl["id"],
        "claimer": claimer,
        "lock_no": lock_no,
        "reject_no": entry["reject_no"],
        "attempt_no": entry["attempt_no"],
        "detail": entry["detail"],
    }


def _claim(conn, sl: dict, claimer: str, round_no: str | None = None) -> dict:
    """占片闸口核心。成功返回锁号，重复返回拒重号，异常态直接拦。"""
    lock = _lock_by_key(conn, sl["slice_key"])

    if sl["status"] != "待重放":
        # 已落库 / 锁已归属这片 -> 属于重复，走拒重留证。
        if sl["status"] == "已落库" or (lock and lock["slice_id"] == sl["id"]):
            return _reject(conn, sl, claimer, lock, "片键已落库", round_no)
        # 失败等不可写状态：片锁与可写未对齐，直接拦，不写台账。
        raise ServiceError("片锁与可写未对齐", 409)

    if lock and lock["status"] != "持有":
        raise ServiceError("片锁与可写未对齐", 409)

    if lock:
        return _reject(conn, sl, claimer, lock, "片键已被持有", round_no)

    lock, created = _acquire_lock(conn, sl, claimer)
    if not created and lock:
        return _reject(conn, sl, claimer, lock, "片键已被持有", round_no)
    if not lock:
        raise ServiceError("片锁写入失败", 500)

    entry = _record_ledger(
        conn,
        sl,
        claimer,
        "占片成功",
        lock_no=lock["lock_no"],
        round_no=round_no,
        detail=f"占片成功，锁 {lock['lock_no']}",
    )
    return {
        "outcome": "占片成功",
        "slice_key": sl["slice_key"],
        "slice_id": sl["id"],
        "claimer": claimer,
        "lock_no": lock["lock_no"],
        "reject_no": None,
        "attempt_no": entry["attempt_no"],
        "detail": entry["detail"],
    }


def _with_slice(sl: dict, result: dict) -> dict:
    out = dict(result)
    out.setdefault("slice_key", sl["slice_key"])
    out["slice"] = {
        k: sl.get(k)
        for k in ("id", "window_id", "slice_date", "status", "row_count")
    }
    out["slice"]["table_name"] = sl.get("table_name")
    return out


# ---------------------------------------------------------------------------
# 占片闸口
# ---------------------------------------------------------------------------


def claim_slice(slice_id: int, claimer: str, db_path: str | None = None) -> dict:
    claimer = (claimer or "").strip()
    if not claimer:
        raise ServiceError("值班人不能为空")
    rejected = None
    with get_db(db_path) as conn:
        sl = _load_slice(conn, slice_id)
        if not sl:
            raise ServiceError("切片不存在", 404)
        result = _claim(conn, sl, claimer)
        body = _with_slice(sl, result)
        if result["outcome"] == "拒重":
            # 拒重台账须先随本事务提交，再在事务外抛错，避免回滚丢证。
            rejected = body
            body = None
    if rejected is not None:
        raise ServiceError("片锁已被占用，拒重", 409, rejected)
    return body


# ---------------------------------------------------------------------------
# 切片计划 / 重放
# ---------------------------------------------------------------------------


def plan_slices(window_id: int, db_path: str | None = None) -> dict:
    with get_db(db_path) as conn:
        win = conn.execute(
            "SELECT * FROM backfill_windows WHERE id=?", (window_id,)
        ).fetchone()
        if not win:
            raise ServiceError("回灌窗口不存在", 404)
        if win["status"] not in ("草稿", "已计划"):
            raise ServiceError("当前状态不可再生成切片", 409)
        days = _daterange(win["start_date"], win["end_date"])
        keyed = [(d, slice_key(win["table_name"], d)) for d in days]
        for _, key in keyed:
            if _lock_by_key(conn, key):
                raise ServiceError("切片已占片，不可重排", 409)
        conn.execute("DELETE FROM backfill_slices WHERE window_id=?", (window_id,))
        for d, _ in keyed:
            conn.execute(
                "INSERT INTO backfill_slices(window_id, slice_date, status, row_count) "
                "VALUES (?,?, '待重放', 0)",
                (window_id, d),
            )
        conn.execute(
            "UPDATE backfill_windows SET status='已计划' WHERE id=?", (window_id,)
        )
        return {
            "window_id": window_id,
            "slices": _list_slices_conn(conn, window_id),
        }


def _list_slices_conn(conn, window_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT s.*, w.table_name AS table_name "
        "FROM backfill_slices s JOIN backfill_windows w ON w.id=s.window_id "
        "WHERE s.window_id=? ORDER BY s.slice_date",
        (window_id,),
    ).fetchall()
    out = []
    for r in rows:
        item = dict(r)
        key = slice_key(item.pop("table_name"), item["slice_date"])
        item["slice_key"] = key
        lock = _lock_by_key(conn, key)
        target = _target_by_key(conn, key)
        # 同片键成功次数：目标表唯一索引保证只可能是 0 或 1。
        item["lock_no"] = lock["lock_no"] if lock else None
        item["success_count"] = 1 if target else 0
        item["target_rows"] = target["row_count"] if target else 0
        out.append(item)
    return out


def list_slices(window_id: int, db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        return _list_slices_conn(conn, window_id)


def _slice_rows(sl: dict) -> int:
    return 100 + int(sl["slice_date"].replace("-", "")) % 97


def _write_target(conn, sl: dict, lock: dict | None) -> dict | None:
    """落库写目标表。同片键已有行则返回 None（由调用方按拒重处理）。"""
    if _target_by_key(conn, sl["slice_key"]):
        return None
    rows = _slice_rows(sl)
    try:
        conn.execute(
            "INSERT INTO backfill_target(slice_key, slice_id, window_id, table_name, "
            "slice_date, row_count, lock_no) VALUES (?,?,?,?,?,?,?)",
            (
                sl["slice_key"],
                sl["id"],
                sl["window_id"],
                sl["table_name"],
                sl["slice_date"],
                rows,
                lock["lock_no"] if lock else None,
            ),
        )
    except sqlite3.IntegrityError:
        return None
    conn.execute(
        "UPDATE backfill_slices SET status='已落库', row_count=? WHERE id=?",
        (rows, sl["id"]),
    )
    conn.execute(
        "UPDATE backfill_windows SET status='执行中' WHERE id=?", (sl["window_id"],)
    )
    pending = conn.execute(
        "SELECT COUNT(*) AS c FROM backfill_slices WHERE window_id=? AND status!='已落库'",
        (sl["window_id"],),
    ).fetchone()["c"]
    if pending == 0:
        conn.execute(
            "UPDATE backfill_windows SET status='已完成' WHERE id=?",
            (sl["window_id"],),
        )
    return _target_by_key(conn, sl["slice_key"])


def replay_slice(
    slice_id: int, claimer: str = "回灌值班", db_path: str | None = None
) -> dict:
    """重放落库。与占片闸口同一路线：先核后写，唯一索引兜底。"""
    rejected: tuple[str, dict] | None = None
    body = None
    with get_db(db_path) as conn:
        sl = _load_slice(conn, slice_id)
        if not sl:
            raise ServiceError("切片不存在", 404)

        lock = _lock_by_key(conn, sl["slice_key"])
        if sl["status"] == "已落库":
            result = _reject(conn, sl, claimer, lock, "片键已落库")
            rejected = ("切片已落库", _with_slice(sl, result))
        elif sl["status"] != "待重放":
            raise ServiceError("片锁与可写未对齐", 409)
        else:
            if lock is None:
                lock, created = _acquire_lock(conn, sl, claimer)
                if not created:
                    result = _reject(conn, sl, claimer, lock, "片键已被持有")
                    rejected = ("片锁已被占用，拒重", _with_slice(sl, result))
            elif lock["status"] != "持有":
                raise ServiceError("片锁与可写未对齐", 409)
            elif lock["slice_id"] != sl["id"]:
                # 同片键被另一窗口的切片占住：物理同片，只允许成功一次。
                result = _reject(conn, sl, claimer, lock, "片键已归属其他切片")
                rejected = ("片键已归属其他切片，拒重", _with_slice(sl, result))

            if rejected is None:
                if lock["claimer"] != claimer:
                    # 片锁在别人手里：闸口就是锁，后到者即便切片仍「待重放」也不许改写。
                    result = _reject(conn, sl, claimer, lock, "片锁由他人持有")
                    rejected = (
                        f"片锁由 {lock['claimer']} 持有，拒重",
                        _with_slice(sl, result),
                    )
                else:
                    target = _write_target(conn, sl, lock)
                    if target is None:
                        result = _reject(conn, sl, claimer, lock, "目标表已存在同片键")
                        rejected = ("目标表已存在同片键，拒重", _with_slice(sl, result))
                    else:
                        fresh = _load_slice(conn, slice_id)
                        body = _with_slice(
                            fresh,
                            {
                                "outcome": "已落库",
                                "slice_key": sl["slice_key"],
                                "slice_id": slice_id,
                                "lock_no": lock["lock_no"],
                                "reject_no": None,
                                "target_rows": target["row_count"],
                            },
                        )
                        body["slice"] = {
                            "id": fresh["id"],
                            "window_id": fresh["window_id"],
                            "slice_date": fresh["slice_date"],
                            "status": fresh["status"],
                            "row_count": fresh["row_count"],
                        }
                        body["target"] = target
    # 拒重已随事务提交，异常移到事务外抛，保证台账与目标表一致。
    if rejected is not None:
        message, payload = rejected
        raise ServiceError(message, 409, payload)
    return body


# ---------------------------------------------------------------------------
# 争用台账 / 片键详情
# ---------------------------------------------------------------------------


def list_ledger(
    slice_key_value: str | None = None,
    round_no: str | None = None,
    limit: int = 200,
    db_path: str | None = None,
) -> list[dict]:
    sql = (
        "SELECT l.*, w.table_name AS table_name, s.slice_date AS slice_date "
        "FROM claim_ledger l "
        "LEFT JOIN backfill_slices s ON s.id=l.slice_id "
        "LEFT JOIN backfill_windows w ON w.id=s.window_id WHERE 1=1"
    )
    args: list = []
    if slice_key_value:
        sql += " AND l.slice_key=?"
        args.append(slice_key_value)
    if round_no:
        sql += " AND l.round_no=?"
        args.append(round_no)
    sql += " ORDER BY l.id DESC LIMIT ?"
    args.append(max(1, min(int(limit), 500)))
    with get_db(db_path) as conn:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]


def slice_key_detail(slice_id: int, db_path: str | None = None) -> dict:
    with get_db(db_path) as conn:
        sl = _load_slice(conn, slice_id)
        if not sl:
            raise ServiceError("切片不存在", 404)
        key = sl["slice_key"]
        lock = _lock_by_key(conn, key)
        target = _target_by_key(conn, key)
        ledger = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM claim_ledger WHERE slice_key=? ORDER BY id", (key,)
            ).fetchall()
        ]
        success = sum(1 for e in ledger if e["outcome"] == "占片成功")
        rejects = [e for e in ledger if e["outcome"] == "拒重"]
        return {
            "slice": {
                "id": sl["id"],
                "window_id": sl["window_id"],
                "table_name": sl["table_name"],
                "slice_date": sl["slice_date"],
                "status": sl["status"],
                "row_count": sl["row_count"],
                "slice_key": key,
            },
            "writable": sl["status"] == "待重放",
            "aligned": (lock is not None and lock["slice_id"] == sl["id"]),
            "lock": lock,
            "target": target,
            "ledger": ledger,
            "success_count": 1 if target else 0,
            "claim_successes": success,
            "reject_count": len(rejects),
            "last_reject_no": rejects[-1]["reject_no"] if rejects else None,
            "last_lock_no": lock["lock_no"] if lock else None,
        }


# ---------------------------------------------------------------------------
# 同片争抢编排：一轮只留一条争抢记录
# ---------------------------------------------------------------------------


def run_contention_round(
    slice_id: int,
    claimers: list[str] | None = None,
    replay: bool = True,
    db_path: str | None = None,
) -> dict:
    names = [c.strip() for c in (claimers or ROUND_CLAIMERS) if c and c.strip()]
    if len(names) != 2:
        raise ServiceError("同片争抢需两侧调度")
    if names[0] == names[1]:
        raise ServiceError("两侧调度不得同名")

    with get_db(db_path) as conn:
        sl = _load_slice(conn, slice_id)
        if not sl:
            raise ServiceError("切片不存在", 404)
        if sl["status"] != "待重放" or _lock_by_key(conn, sl["slice_key"]):
            raise ServiceError("切片不可争抢", 409)

        round_no = f"CR-{conn.execute('SELECT COALESCE(MAX(id),0)+1 FROM contention_rounds').fetchone()[0]:04d}"

        claims = [_claim(conn, sl, name, round_no) for name in names]
        winner = next((c for c in claims if c["outcome"] == "占片成功"), None)
        loser = next((c for c in claims if c["outcome"] == "拒重"), None)
        if not winner or not loser:
            # 第一次就未形成一胜一拒，说明状态异常；回滚且不留争抢记录。
            raise ServiceError("争抢未形成一胜一拒", 409)

        replay_info = None
        target = None
        if replay:
            lock = _lock_by_key(conn, sl["slice_key"])
            target = _write_target(conn, sl, lock)
            if target is None:
                raise ServiceError("目标表已存在同片键，拒重", 409)
            replay_info = {
                "slice_id": slice_id,
                "slice_key": sl["slice_key"],
                "lock_no": lock["lock_no"],
                "target_rows": target["row_count"],
                "claimer": winner["claimer"],
            }
            # 落库后，落败方再点一次重放：必须拒重，且不得再写目标表。
            fresh = _load_slice(conn, slice_id)
            dup_lock = _lock_by_key(conn, fresh["slice_key"])
            dup = _reject(conn, fresh, loser["claimer"], dup_lock, "切片已落库",
                          round_no)
            replay_info["duplicate_replay"] = dup

        conn.execute(
            "INSERT INTO contention_rounds(round_no, slice_key, lock_no, "
            "reject_no, attempts) VALUES (?,?,?,?,?)",
            (
                round_no,
                sl["slice_key"],
                winner["lock_no"],
                loser["reject_no"],
                len(claims),
            ),
        )
        round_row = dict(
            conn.execute(
                "SELECT * FROM contention_rounds WHERE round_no=?", (round_no,)
            ).fetchone()
        )

        return {
            "round": round_row,
            "claims": claims,
            "gate": {
                "lock_no": winner["lock_no"],
                "reject_no": loser["reject_no"],
                "slice_key": sl["slice_key"],
            },
            "replay": replay_info,
            "target_count": conn.execute(
                "SELECT COUNT(*) AS c FROM backfill_target WHERE slice_key=?",
                (sl["slice_key"],),
            ).fetchone()["c"],
            "progress": _progress_conn(conn, sl["window_id"]),
        }


def list_rounds(db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM contention_rounds ORDER BY id DESC"
        ).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# 回灌进度：一切按唯一片键聚合，拒重不计数
# ---------------------------------------------------------------------------


def _progress_conn(conn, window_id: int | None = None) -> dict:
    win_sql = "SELECT * FROM backfill_windows"
    args: list = []
    if window_id is not None:
        win_sql += " WHERE id=?"
        args.append(window_id)
    windows = [dict(r) for r in conn.execute(win_sql + " ORDER BY id", args).fetchall()]

    items = []
    for w in windows:
        keys = [
            slice_key(w["table_name"], r["slice_date"])
            for r in conn.execute(
                "SELECT slice_date FROM backfill_slices WHERE window_id=?", (w["id"],)
            ).fetchall()
        ]
        planned = len(keys)
        if keys:
            marks = ",".join("?" for _ in keys)
            tgt = conn.execute(
                f"SELECT COUNT(*) AS c, COALESCE(SUM(row_count),0) AS r "
                f"FROM backfill_target WHERE slice_key IN ({marks})",
                keys,
            ).fetchone()
            rej = conn.execute(
                f"SELECT COUNT(*) AS c FROM claim_ledger "
                f"WHERE outcome='拒重' AND slice_key IN ({marks})",
                keys,
            ).fetchone()["c"]
        else:
            tgt = {"c": 0, "r": 0}
            rej = 0
        loaded = int(tgt["c"])
        items.append(
            {
                "window_id": w["id"],
                "table_name": w["table_name"],
                "status": w["status"],
                "planned": planned,
                "loaded": loaded,
                "missing": planned - loaded,
                "success_keys": loaded,
                "target_rows": int(tgt["r"]),
                "reject_count": int(rej),
                "progress_pct": round(loaded * 100 / planned, 1) if planned else 0.0,
            }
        )

    # 全局按唯一片键去重，防止跨窗口重复计划把进度抬高。
    all_keys = [
        slice_key(r["table_name"], r["slice_date"])
        for r in conn.execute(
            "SELECT w.table_name AS table_name, s.slice_date AS slice_date "
            "FROM backfill_slices s JOIN backfill_windows w ON w.id=s.window_id"
        ).fetchall()
    ]
    unique_planned = len(set(all_keys))
    tgt_all = conn.execute(
        "SELECT COUNT(*) AS c, COALESCE(SUM(row_count),0) AS r FROM backfill_target"
    ).fetchone()
    rej_all = conn.execute(
        "SELECT COUNT(*) AS c FROM claim_ledger WHERE outcome='拒重'"
    ).fetchone()["c"]
    totals = {
        "windows": len(windows),
        "planned_keys": unique_planned,
        "success_keys": int(tgt_all["c"]),
        "target_rows": int(tgt_all["r"]),
        "reject_count": int(rej_all),
        "progress_pct": (
            round(int(tgt_all["c"]) * 100 / unique_planned, 1)
            if unique_planned
            else 0.0
        ),
    }
    return {"windows": items, "totals": totals}


def progress_report(window_id: int | None = None, db_path: str | None = None) -> dict:
    with get_db(db_path) as conn:
        if window_id is not None:
            exists = conn.execute(
                "SELECT 1 FROM backfill_windows WHERE id=?", (window_id,)
            ).fetchone()
            if not exists:
                raise ServiceError("回灌窗口不存在", 404)
        return _progress_conn(conn, window_id)


# ---------------------------------------------------------------------------
# 覆盖对账（沿用基线，按片键去重）
# ---------------------------------------------------------------------------


def coverage_report(window_id: int, db_path: str | None = None) -> dict:
    with get_db(db_path) as conn:
        win = conn.execute(
            "SELECT * FROM backfill_windows WHERE id=?", (window_id,)
        ).fetchone()
        if not win:
            raise ServiceError("回灌窗口不存在", 404)
        planned = conn.execute(
            "SELECT COUNT(*) AS c FROM backfill_slices WHERE window_id=?",
            (window_id,),
        ).fetchone()["c"]
        loaded = conn.execute(
            "SELECT COUNT(*) AS c FROM backfill_slices s "
            "JOIN backfill_target t ON t.slice_key = ? || '#' || s.slice_date "
            "WHERE s.window_id=?",
            (win["table_name"], window_id),
        ).fetchone()["c"]
        missing = planned - loaded
        summary = (
            f"窗口#{window_id} {win['table_name']}：计划{planned}片，已落库{loaded}，缺口{missing}"
        )
        cur = conn.execute(
            "INSERT INTO coverage_reports(window_id, planned, loaded, missing, summary) "
            "VALUES (?,?,?,?,?)",
            (window_id, planned, loaded, missing, summary),
        )
        row = conn.execute(
            "SELECT * FROM coverage_reports WHERE id=?", (cur.lastrowid,)
        ).fetchone()
        return dict(row)


def list_coverage(db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM coverage_reports ORDER BY id DESC"
        ).fetchall()
        return [dict(r) for r in rows]
