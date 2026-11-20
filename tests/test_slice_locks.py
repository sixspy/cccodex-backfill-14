import sqlite3

import pytest

from app.services import windows as svc


async def _plan(authed, index=0):
    windows = (await authed.get("/api/windows")).json()["items"]
    wid = windows[index]["id"]
    slices = (await authed.post(f"/api/windows/{wid}/plan")).json()["slices"]
    return wid, slices


@pytest.mark.asyncio
async def test_gate_claim_once_then_reject(authed):
    _, slices = await _plan(authed)
    sid = slices[0]["id"]

    ok = await authed.post(f"/api/slices/{sid}/claim", json={"claimer": "调度甲"})
    assert ok.status_code == 200, ok.text
    gate = ok.json()
    assert gate["outcome"] == "占片成功"
    assert gate["lock_no"].startswith("L")

    dup = await authed.post(f"/api/slices/{sid}/claim", json={"claimer": "调度乙"})
    assert dup.status_code == 409
    details = dup.json()["details"]
    assert details["outcome"] == "拒重"
    assert details["reject_no"].startswith("R")
    # 闸口亮片锁号：拒重也带着同一把片锁号。
    assert details["lock_no"] == gate["lock_no"]

    # 争用台账与闸口读同一个拒重号。
    ledger = (await authed.get("/api/claim-ledger", params={"slice_key": gate["slice_key"]})).json()["items"]
    rejects = [e for e in ledger if e["outcome"] == "拒重"]
    assert rejects and rejects[0]["reject_no"] == details["reject_no"]
    assert rejects[0]["lock_no"] == gate["lock_no"]

    detail = (await authed.get(f"/api/slices/{sid}/detail")).json()
    assert detail["last_reject_no"] == details["reject_no"]
    assert detail["success_count"] == 0  # 只占片未落库
    assert detail["aligned"] is True


@pytest.mark.asyncio
async def test_claim_requires_claimer(authed):
    _, slices = await _plan(authed)
    r = await authed.post(f"/api/slices/{slices[0]['id']}/claim", json={"claimer": "  "})
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_same_slice_double_replay_is_refused(authed):
    wid, slices = await _plan(authed)
    sid = slices[0]["id"]

    first = await authed.post(f"/api/slices/{sid}/replay", json={"claimer": "调度甲"})
    assert first.status_code == 200, first.text
    rows = first.json()["slice"]["row_count"]
    assert rows > 0

    dup = await authed.post(f"/api/slices/{sid}/replay", json={"claimer": "调度乙"})
    assert dup.status_code == 409
    assert dup.json()["details"]["reject_no"].startswith("R")

    detail = (await authed.get(f"/api/slices/{sid}/detail")).json()
    assert detail["success_count"] == 1
    assert detail["target"]["row_count"] == rows

    progress = (await authed.get("/api/progress")).json()
    win = next(w for w in progress["windows"] if w["window_id"] == wid)
    assert win["loaded"] == 1
    assert win["target_rows"] == rows  # 不翻倍
    assert win["success_keys"] == 1


@pytest.mark.asyncio
async def test_contention_round_once_and_single_target(authed):
    wid, slices = await _plan(authed)
    sid = slices[0]["id"]
    key = slices[0]["slice_key"]

    r = await authed.post("/api/contention", json={"slice_id": sid})
    assert r.status_code == 200, r.text
    body = r.json()

    outcomes = sorted(c["outcome"] for c in body["claims"])
    assert outcomes == ["占片成功", "拒重"]
    lock_no = body["gate"]["lock_no"]
    reject_no = body["gate"]["reject_no"]
    assert lock_no and reject_no

    # 同片键在目标表只留一行。
    assert body["target_count"] == 1
    # 落败方二次重放被拒，且不新增目标行。
    assert body["replay"]["duplicate_replay"]["reject_no"].startswith("R")
    assert body["replay"]["duplicate_replay"]["lock_no"] == lock_no

    # 编排只留一条争抢记录。
    rounds = (await authed.get("/api/contention")).json()["items"]
    assert len([x for x in rounds if x["slice_key"] == key]) == 1
    assert rounds[0]["lock_no"] == lock_no
    assert rounds[0]["reject_no"] == reject_no

    # 切片计划里同片键成功次数只能是一。
    listed = (await authed.get(f"/api/windows/{wid}/slices")).json()["items"]
    target = next(s for s in listed if s["id"] == sid)
    assert target["success_count"] == 1
    assert target["lock_no"] == lock_no

    # 回灌进度不因拒重/重放虚高。
    win = (await authed.get("/api/progress", params={"window_id": wid})).json()["windows"][0]
    assert win["loaded"] == 1
    assert win["progress_pct"] == round(100 / 3, 1)


@pytest.mark.asyncio
async def test_replay_requires_target_not_double_after_claim(authed):
    """先占片再重放：同一条路线，落库一次，二次重放拒重。"""
    _, slices = await _plan(authed)
    sid = slices[0]["id"]
    claim = await authed.post(f"/api/slices/{sid}/claim", json={"claimer": "调度甲"})
    assert claim.status_code == 200
    lock_no = claim.json()["lock_no"]

    # 非持锁人不能越过片锁落库。
    steal = await authed.post(f"/api/slices/{sid}/replay", json={"claimer": "调度乙"})
    assert steal.status_code == 409
    assert steal.json()["details"]["lock_no"] == lock_no

    ok = await authed.post(f"/api/slices/{sid}/replay", json={"claimer": "调度甲"})
    assert ok.status_code == 200, ok.text
    assert ok.json()["result"]["lock_no"] == lock_no

    dup = await authed.post(f"/api/slices/{sid}/replay", json={"claimer": "调度甲"})
    assert dup.status_code == 409

    detail = (await authed.get(f"/api/slices/{sid}/detail")).json()
    assert detail["success_count"] == 1


def test_concurrent_replay_replay_proof(tmp_path):
    """两条调度抢同片：缺锁会双写片锁行/目标行，这里直接并发压。"""
    import threading

    db = str(tmp_path / "race.db")
    svc_plan = svc.plan_slices(1, db_path=db) if _seeded(db) else None
    assert svc_plan is not None
    sid = svc_plan["slices"][0]["id"]

    results = []
    guard = threading.Lock()

    def worker(name):
        try:
            svc.replay_slice(sid, name, db_path=db)
            with guard:
                results.append(("ok", name))
        except Exception as exc:  # noqa: BLE001
            with guard:
                results.append(("rej", getattr(exc, "status_code", 500)))

    threads = [threading.Thread(target=worker, args=(f"调度{i}",)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum(1 for r in results if r[0] == "ok") == 1
    assert sum(1 for r in results if r[0] == "rej") == 7

    conn = sqlite3.connect(db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM slice_locks").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM backfill_target").fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM claim_ledger WHERE outcome='拒重'"
        ).fetchone()[0] == 7
    finally:
        conn.close()

    prog = svc.progress_report(1, db_path=db)
    assert prog["windows"][0]["loaded"] == 1
    assert prog["windows"][0]["success_keys"] == 1


def _seeded(db_path):
    from app.db import init_db

    init_db(db_path)
    return True
