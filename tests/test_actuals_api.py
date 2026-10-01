"""装船实绩登记 API 端到端测试（事件流水、晚到重放、冻结基线、只读）。"""

import threading

from tests.test_api_workflow import SLOT_PAYLOAD

SLOT4 = SLOT_PAYLOAD  # A01/A02/B01/B02，每堆两层
MANIFEST4 = {
    "containers": [
        {"no": "C001", "weight": 40, "hazmat": None, "discharge_order": 1},
        {"no": "C002", "weight": 30, "hazmat": None, "discharge_order": 1},
        {"no": "C003", "weight": 20, "hazmat": None, "discharge_order": 1},
        {"no": "C004", "weight": 10, "hazmat": None, "discharge_order": 1},
    ]
}


def _setup(client, crane_count=2, duration=10):
    sr = client.post("/api/slot-revisions", json=SLOT4).json()["slots_revision"]
    mr = client.post("/api/manifest-revisions", json=MANIFEST4) \
        .json()["manifest_revision"]
    plan = client.post("/api/plans/compute", json={}).json()
    client.post(f"/api/plans/{plan['plan_id']}/confirm", json={
        "slots_revision": sr, "manifest_revision": mr
    })
    body = {
        "crane_count": crane_count,
        "entries": [
            {"no": c["no"], "duration": duration, "release_minute": 0,
             "deadline_minute": 100}
            for c in MANIFEST4["containers"]
        ],
    }
    sch = client.post(f"/api/plans/{plan['plan_id']}/schedules", json=body).json()
    assert sch["makespan"] == 20  # 每堆自底向上串行，两堆并行
    return plan, sch


def _loc_map(sch):
    return {(e["stack"], e["tier"]): e["container_no"] for e in sch["entries"]}


def _ev(seq, kind, no, crane, minute):
    return {"event_seq": seq, "event_type": kind, "container_no": no,
            "crane_no": crane, "minute": minute}


def _post(client, plan, seq, kind, no, crane, minute):
    return client.post(f"/api/plans/{plan['plan_id']}/actuals/events",
                        json=_ev(seq, kind, no, crane, minute))


def _get(client, plan):
    return client.get(f"/api/plans/{plan['plan_id']}/actuals")


def test_register_start_finish_happy_path_and_query(client):
    plan, sch = _setup(client)
    loc = _loc_map(sch)
    a1, a2 = loc[("A", 1)], loc[("A", 2)]
    b1, b2 = loc[("B", 1)], loc[("B", 2)]

    r = _post(client, plan, seq=1, kind="start", no=a1, minute=0, crane=1)
    assert r.status_code == 201, r.text
    view = r.json()
    assert view["event_count"] == 1
    assert view["read_only"] is False
    assert view["plan_status"] == "confirmed"
    assert view["baseline_crane_count"] == 2
    assert view["baseline_makespan"] == 20
    assert view["frozen_at"] is not None

    boxes = {b["container_no"]: b for b in view["boxes"]}
    assert boxes[a1]["status"] == "in_progress"
    assert boxes[a1]["actual_start_minute"] == 0
    assert boxes[a1]["actual_finish_minute"] is None
    assert boxes[a1]["start_minute_delta"] == 0 - boxes[a1]["planned_start_minute"]
    assert boxes[a1]["finish_minute_delta"] is None
    assert boxes[a1]["crane_changed"] in (True, False)
    assert boxes[a2]["status"] == "not_started"
    assert view["unfinished"] == sorted(boxes)  # 仅 a1 开工，全部未完

    # B 堆底与 A 堆底并行（桥 2），完工
    assert _post(client, plan, seq=2, kind="start", no=b1, minute=0,
                 crane=2).status_code == 201
    assert _post(client, plan, seq=3, kind="finish", no=b1, minute=10,
                 crane=2).status_code == 201
    assert _post(client, plan, seq=4, kind="finish", no=a1, minute=10,
                 crane=1).status_code == 201

    # 第 10 分钟：先完工后开工，两堆上层均可开工
    assert _post(client, plan, seq=5, kind="start", no=a2, minute=10,
                 crane=1).status_code == 201
    assert _post(client, plan, seq=6, kind="start", no=b2, minute=10,
                 crane=2).status_code == 201

    view = _get(client, plan).json()
    boxes = {b["container_no"]: b for b in view["boxes"]}
    assert boxes[a1]["status"] == "finished"
    assert boxes[b1]["status"] == "finished"
    assert boxes[a2]["status"] == "in_progress"
    assert boxes[a2]["actual_start_minute"] == 10
    assert sorted(view["unfinished"]) == sorted([a2, b2])

    # 偏差：实际 10 完工，计划开工 0 的底箱 finish 偏差 = 10 - 10 = 0
    assert boxes[a1]["actual_finish_minute"] == 10
    assert boxes[a1]["finish_minute_delta"] == 10 - boxes[a1]["planned_finish_minute"]


def test_no_schedule_rejected_404_and_draft_409(client):
    sr = client.post("/api/slot-revisions", json=SLOT4).json()["slots_revision"]
    client.post("/api/manifest-revisions", json=MANIFEST4)
    plan = client.post("/api/plans/compute", json={}).json()
    # 草稿方案：409
    r = client.post(f"/api/plans/{plan['plan_id']}/actuals/events",
                    json=_ev(1, "start", "C001", 1, 0))
    assert r.status_code == 409 and "已确认" in r.json()["detail"]
    # 确认但无排程：404
    client.post(f"/api/plans/{plan['plan_id']}/confirm", json={
        "slots_revision": sr, "manifest_revision": 1
    })
    r = client.post(f"/api/plans/{plan['plan_id']}/actuals/events",
                    json=_ev(1, "start", "C001", 1, 0))
    assert r.status_code == 404 and "排程" in r.json()["detail"]
    # 方案不存在
    assert client.post("/api/plans/nope/actuals/events",
                       json=_ev(1, "start", "C001", 1, 0)).status_code == 404
    # 未登记前查询 404
    assert _get(client, plan).status_code == 404


def test_invalid_payload_422(client):
    plan, _ = _setup(client)
    bad_minute = _ev(1, "start", "C001", 1, -1)
    assert client.post(f"/api/plans/{plan['plan_id']}/actuals/events",
                       json=bad_minute).status_code == 422
    bad_crane = _ev(1, "start", "C001", 0, 0)
    assert client.post(f"/api/plans/{plan['plan_id']}/actuals/events",
                       json=bad_crane).status_code == 422
    bad_seq = _ev(0, "start", "C001", 1, 0)
    assert client.post(f"/api/plans/{plan['plan_id']}/actuals/events",
                       json=bad_seq).status_code == 422
    bad_type = _ev(1, "pause", "C001", 1, 0)
    assert client.post(f"/api/plans/{plan['plan_id']}/actuals/events",
                       json=bad_type).status_code == 422


def test_box_not_in_plan_rejected_422(client):
    plan, _ = _setup(client)
    r = _post(client, plan, seq=1, kind="start", no="C999", minute=0, crane=1)
    assert r.status_code == 422 and "不在该方案" in r.json()["detail"]
    assert _get(client, plan).status_code == 404  # 拒绝不落任何记录


def test_crane_out_of_frozen_range_rejected_422(client):
    plan, _ = _setup(client, crane_count=1)
    r = _post(client, plan, seq=1, kind="start", no="C001", minute=0, crane=2)
    assert r.status_code == 422 and "岸桥" in r.json()["detail"]


def test_start_requires_crane_idle(client):
    plan, sch = _setup(client)
    loc = _loc_map(sch)
    a1, b1 = loc[("A", 1)], loc[("B", 1)]
    _post(client, plan, seq=1, kind="start", no=a1, minute=0, crane=1)
    # 桥 1 忙：另一底箱第 5 分钟想用桥 1
    r = _post(client, plan, seq=2, kind="start", no=b1, minute=5, crane=1)
    assert r.status_code == 422 and "不空闲" in r.json()["detail"]
    # 桥 2 空闲，可并行
    assert _post(client, plan, seq=3, kind="start", no=b1, minute=5,
                 crane=2).status_code == 201


def test_start_requires_lower_stack_finished(client):
    plan, sch = _setup(client)
    loc = _loc_map(sch)
    a1, a2 = loc[("A", 1)], loc[("A", 2)]
    # 上层先开工：下层未完工
    r = _post(client, plan, seq=1, kind="start", no=a2, minute=0, crane=1)
    assert r.status_code == 422 and "下层" in r.json()["detail"]
    # 下层开工但未完工，上层仍不可开工
    _post(client, plan, seq=2, kind="start", no=a1, minute=0, crane=1)
    r = _post(client, plan, seq=3, kind="start", no=a2, minute=10, crane=2)
    assert r.status_code == 422 and "下层" in r.json()["detail"]
    # 下层第 10 分钟完工后，上层同分钟可开工（同分钟先完工后开工）
    _post(client, plan, seq=4, kind="finish", no=a1, minute=10, crane=1)
    assert _post(client, plan, seq=5, kind="start", no=a2, minute=10,
                 crane=1).status_code == 201


def test_duplicate_start_rejected(client):
    plan, sch = _setup(client)
    a1 = _loc_map(sch)[("A", 1)]
    _post(client, plan, seq=1, kind="start", no=a1, minute=0, crane=1)
    r = _post(client, plan, seq=2, kind="start", no=a1, minute=5, crane=1)
    assert r.status_code == 422 and "重复开工" in r.json()["detail"]


def test_finish_rules(client):
    plan, sch = _setup(client)
    loc = _loc_map(sch)
    a1 = loc[("A", 1)]
    # 未开工先完工
    r = _post(client, plan, seq=1, kind="finish", no=a1, minute=10, crane=1)
    assert r.status_code == 422 and "尚未开工" in r.json()["detail"]
    _post(client, plan, seq=2, kind="start", no=a1, minute=0, crane=1)
    # 岸桥不一致
    r = _post(client, plan, seq=3, kind="finish", no=a1, minute=10, crane=2)
    assert r.status_code == 422 and "岸桥" in r.json()["detail"]
    # 完工不晚于开工
    r = _post(client, plan, seq=4, kind="finish", no=a1, minute=0, crane=1)
    assert r.status_code == 422 and "晚于开工" in r.json()["detail"]
    # 正常完工后重复完工
    assert _post(client, plan, seq=5, kind="finish", no=a1, minute=10,
                 crane=1).status_code == 201
    r = _post(client, plan, seq=6, kind="finish", no=a1, minute=12, crane=1)
    assert r.status_code == 422 and "重复完工" in r.json()["detail"]


def test_late_event_valid_same_minute_finish_before_start(client):
    """晚到事件按实际分钟重放：m10 先完工底箱、后开工上层（即使开工流水号更小）。"""
    plan, sch = _setup(client)
    loc = _loc_map(sch)
    a1, a2 = loc[("A", 1)], loc[("A", 2)]
    _post(client, plan, seq=10, kind="start", no=a1, minute=0, crane=1)
    # 先到的是完工（seq 更大）
    assert _post(client, plan, seq=11, kind="finish", no=a1, minute=10,
                 crane=1).status_code == 201
    # 晚到的开工流水号更小：重放时同分钟 finish 在 start 前，合法
    r = _post(client, plan, seq=9, kind="start", no=a2, minute=10, crane=1)
    assert r.status_code == 201, r.text
    view = _get(client, plan).json()
    # 事件按重放次序返回：m0 开工，m10 完工(seq11)，m10 开工(seq9)
    keys = [(e["minute"], e["event_type"], e["event_seq"]) for e in view["events"]]
    assert keys == [(0, "start", 10), (10, "finish", 11), (10, "start", 9)]


def test_late_event_invalidates_chain_whole_request_rejected(client):
    plan, sch = _setup(client)
    loc = _loc_map(sch)
    a1, b1 = loc[("A", 1)], loc[("B", 1)]
    # 桥 1 上 a1 [0,10)
    _post(client, plan, seq=1, kind="start", no=a1, minute=0, crane=1)
    _post(client, plan, seq=2, kind="finish", no=a1, minute=10, crane=1)
    # 晚到事件：b1 第 5 分钟想用桥 1 -> 与既有链矛盾，整次拒绝
    r = _post(client, plan, seq=3, kind="start", no=b1, minute=5, crane=1)
    assert r.status_code == 422 and "不空闲" in r.json()["detail"]
    view = _get(client, plan).json()
    assert view["event_count"] == 2  # 记录不变


def test_idempotent_same_seq_returns_original_and_conflict(client):
    plan, sch = _setup(client)
    a1 = _loc_map(sch)[("A", 1)]
    ev = _ev(1, "start", a1, 1, 0)
    r1 = client.post(f"/api/plans/{plan['plan_id']}/actuals/events", json=ev)
    assert r1.status_code == 201
    # 同流水号同内容重试：200 幂等，返回原结果，不新增事件
    r2 = client.post(f"/api/plans/{plan['plan_id']}/actuals/events", json=ev)
    assert r2.status_code == 200
    assert r2.json()["event_count"] == 1
    # 同流水号异内容：409，记录不变
    diff = _ev(1, "start", a1, 1, 3)
    r3 = client.post(f"/api/plans/{plan['plan_id']}/actuals/events", json=diff)
    assert r3.status_code == 409 and "内容不同" in r3.json()["detail"]
    assert _get(client, plan).json()["event_count"] == 1


def test_schedule_replace_after_freeze_keeps_baseline(client):
    plan, sch = _setup(client, crane_count=1)
    loc = _loc_map(sch)
    a1 = loc[("A", 1)]
    planned = next(e for e in sch["entries"] if e["container_no"] == a1)

    # 首次登记冻结单桥排程
    _post(client, plan, seq=1, kind="start", no=a1, minute=2, crane=1)

    # 排程替换为双桥；已冻结基线与已登记实绩不变
    body = {
        "crane_count": 2,
        "entries": [
            {"no": c["no"], "duration": 10, "release_minute": 0,
             "deadline_minute": 100}
            for c in MANIFEST4["containers"]
        ],
    }
    client.post(f"/api/plans/{plan['plan_id']}/schedules", json=body)

    view = _get(client, plan).json()
    assert view["baseline_crane_count"] == 1  # 基线仍是冻结时的单桥排程
    box = next(b for b in view["boxes"] if b["container_no"] == a1)
    assert box["actual_start_minute"] == 2
    assert box["planned_start_minute"] == planned["start_minute"]
    # 新事件仍按冻结基线校验：桥 2 超出基线岸桥范围
    r = _post(client, plan, seq=2, kind="start",
              no=loc[("B", 1)], minute=2, crane=2)
    assert r.status_code == 422 and "岸桥" in r.json()["detail"]


def test_superseded_plan_actuals_readonly_but_queryable(client):
    plan, sch = _setup(client)
    a1 = _loc_map(sch)[("A", 1)]
    _post(client, plan, seq=1, kind="start", no=a1, minute=0, crane=1)

    # 新箱清单 -> 修订并确认，旧修订 superseded
    client.post("/api/manifest-revisions", json={
        "containers": [{"no": "C001", "weight": 15, "hazmat": None,
                        "discharge_order": 1}]
    })
    p2 = client.post(f"/api/plans/{plan['plan_id']}/amend", json={}).json()
    ok = client.post(f"/api/plans/{p2['plan_id']}/confirm", json={
        "slots_revision": 1, "manifest_revision": 2
    })
    assert ok.status_code == 200

    view = _get(client, plan).json()
    assert view["read_only"] is True
    assert view["plan_status"] == "superseded"
    box = next(b for b in view["boxes"] if b["container_no"] == a1)
    assert box["actual_start_minute"] == 0

    r = _post(client, plan, seq=2, kind="finish", no=a1, minute=10, crane=1)
    assert r.status_code == 409 and "只读" in r.json()["detail"]
    assert _get(client, plan).json()["event_count"] == 1


def test_concurrent_first_registration_single_baseline(client):
    plan, sch = _setup(client)
    loc = _loc_map(sch)
    a1, b1 = loc[("A", 1)], loc[("B", 1)]
    results = []
    barrier = threading.Barrier(2)

    def register(seq, no, crane):
        barrier.wait()
        r = client.post(
            f"/api/plans/{plan['plan_id']}/actuals/events",
            json=_ev(seq, "start", no, crane, 0),
        )
        results.append(r.status_code)

    t1 = threading.Thread(target=register, args=(1, a1, 1))
    t2 = threading.Thread(target=register, args=(2, b1, 2))
    t1.start(); t2.start(); t1.join(); t2.join()
    assert sorted(results) == [201, 201]
    view = _get(client, plan).json()
    assert view["event_count"] == 2
    # 两个首登只冻结同一条基线（baseline_crane_count 一致即说明均基于冻结基线）
    assert view["baseline_crane_count"] == 2


def test_concurrent_same_seq_conflict(client):
    plan, sch = _setup(client)
    a1 = _loc_map(sch)[("A", 1)]
    results = []
    barrier = threading.Barrier(2)

    def register(minute):
        barrier.wait()
        r = client.post(
            f"/api/plans/{plan['plan_id']}/actuals/events",
            json=_ev(1, "start", a1, 1, minute),
        )
        results.append(r.status_code)

    t1 = threading.Thread(target=register, args=(0,))
    t2 = threading.Thread(target=register, args=(3,))
    t1.start(); t2.start(); t1.join(); t2.join()
    assert sorted(results) == [201, 409]
    assert _get(client, plan).json()["event_count"] == 1
