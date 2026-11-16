import pytest
from app.errors import DomainError
from app.modules.m5_watermark.core import advance, run
from app.modules.m5_watermark import core
from app.store import Store

def test_trap_1(): # TRAP-11
    assert advance("2026-11-15T01:00:00Z",["2026-11-15T00:40:00Z"])["new"].startswith("2026-11-15T01:00:00")
def test_trap_2(): assert advance("2026-11-15T01:00:00Z",["2026-11-15T00:40:00Z"])["late"]==["2026-11-15T00:40:00Z"]
def test_advances_after_window(): assert advance("2026-11-15T01:00:00Z",["2026-11-15T01:30:00Z"])["advanced"] is True
def test_exact_window_no_advance(): assert advance("2026-11-15T01:00:00Z",["2026-11-15T01:15:00Z"])["advanced"] is False
def test_empty_stable(): assert advance("2026-11-15T01:00:00Z",[])["advanced"] is False
def test_custom_window(): assert advance("2026-11-15T01:00:00Z",["2026-11-15T01:06:00Z"],5)["advanced"] is True
def test_sample_late(): assert run({})["late_count"]==1
def test_all_events_kept(): assert run({})["valid_count"]==2

EV=lambda rid,ts,status=200:{"request_id":rid,"timestamp":ts,"status":status}

@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path/"m5.db"))

def seed_pay(store):
    return core.advance_service(store,"pay",[EV("p1","2026-11-15T01:05:00Z"),EV("p2","2026-11-15T01:20:00Z",500),EV("p3","2026-11-15T02:10:00Z")])

def test_disable_freezes_progress_and_holds(store):
    seeded=seed_pay(store); wm=seeded["watermark"]; gen=seeded["generation"]
    disabled=core.disable(store,"pay")
    assert disabled["status"]=="DISABLED" and disabled["generation"]==gen+1 and disabled["catchup"]=="disabled"
    held=core.advance_service(store,"pay",[EV("p4","2026-11-15T01:40:00Z"),EV("p5","2026-11-15T02:30:00Z",500)])
    assert held["status"]=="HELD" and held["held_count"]==2
    assert held["watermark"]==wm and held["bucket_count"]==seeded["bucket_count"]

def test_enable_resumes_frozen_progress_and_revises_only_own_old_buckets(store):
    seed_pay(store)
    other=core.advance_service(store,"search",[EV("s1","2026-11-15T01:10:00Z"),EV("s2","2026-11-15T02:20:00Z")])
    core.disable(store,"pay")
    core.advance_service(store,"pay",[EV("p4","2026-11-15T01:40:00Z"),EV("p5","2026-11-15T02:30:00Z",500)])
    out=core.enable(store,"pay")
    assert out["catchup"]=="caught_up" and out["held_count"]==0 and out["backlog_remaining"]==0
    diffs=out["diffs"]
    assert [d["hour"] for d in diffs]==["2026-11-15T01:00:00Z"] # 仅本服务停用前旧桶
    assert diffs[0]["service"]=="pay" and diffs[0]["before"]["count"]==2 and diffs[0]["after"]["count"]==3
    assert out["watermark"]=="2026-11-15T02:15:00+00:00" # 按停用前进度追平，不倒退
    after=core.load(store,"search") # 其他服务已封结果保持不变
    assert after["buckets"]=={b["hour"]:{"count":b["count"],"errors":b["errors"]} for b in other["buckets"]}
    assert after["watermark"]==other["watermark"] and after["diffs"]==[]

def test_stale_generation_rejected(store):
    gen=seed_pay(store)["generation"]
    core.disable(store,"pay")
    with pytest.raises(DomainError) as err:
        core.advance_service(store,"pay",[EV("p9","2026-11-15T03:00:00Z")],generation=gen)
    assert err.value.code=="STALE_VERSION"
    core.enable(store,"pay")
    with pytest.raises(DomainError): # 恢复后旧代次仍拒绝
        core.advance_service(store,"pay",[EV("p9","2026-11-15T03:00:00Z")],generation=gen)
    with pytest.raises(DomainError): # 停用期代次同样过期
        core.advance_service(store,"pay",[EV("p9","2026-11-15T03:00:00Z")],generation=gen+1)
    ok=core.advance_service(store,"pay",[EV("p9","2026-11-15T03:00:00Z")],generation=gen+2)
    assert ok["status"]=="READY" and ok["generation"]==gen+2

def test_concurrent_generation_change_rejected(store, monkeypatch):
    seed_pay(store)
    original=core.load; fired={"done":False}
    def racy_load(s,svc):
        state=original(s,svc)
        if not fired["done"]:
            fired["done"]=True; core.disable(store,"pay") # 读取后并发停用推高代次
        return state
    monkeypatch.setattr(core,"load",racy_load)
    with pytest.raises(DomainError) as err:
        core.advance_service(store,"pay",[EV("p9","2026-11-15T03:00:00Z")])
    assert err.value.code=="STALE_VERSION"
    state=core.load(store,"pay") # 并发推进未落库：仍停用，事件未计数
    assert state["status"]=="disabled" and "p9" not in state["applied"]

def test_interrupted_resume_from_received_position(store):
    seed_pay(store); core.disable(store,"pay")
    core.advance_service(store,"pay",[EV("p4","2026-11-15T01:40:00Z"),EV("p5","2026-11-15T02:30:00Z"),EV("p6","2026-11-15T02:45:00Z")])
    first=core.enable(store,"pay",limit=1) # 中断：只接收 1 条
    assert first["received"]==1 and first["backlog_remaining"]==2 and first["catchup"]=="pending"
    resumed=core.enable(store,"pay") # 恢复：从已接收位置续跑
    assert resumed["received"]==3 and resumed["backlog_remaining"]==0 and resumed["catchup"]=="caught_up"
    buckets={b["hour"]:b for b in resumed["buckets"]}
    assert buckets["2026-11-15T01:00:00Z"]["count"]==3 and buckets["2026-11-15T02:00:00Z"]["count"]==3

def test_rerun_does_not_double_count(store):
    seed_pay(store); core.disable(store,"pay")
    batch=[EV("p4","2026-11-15T01:40:00Z"),EV("p5","2026-11-15T02:30:00Z",500)]
    core.advance_service(store,"pay",batch); core.advance_service(store,"pay",batch) # 停用期重复上报
    assert core.load(store,"pay")["held"]==batch
    core.enable(store,"pay"); again=core.enable(store,"pay") # 重跑恢复
    assert again["diffs"]==[]
    core.advance_service(store,"pay",batch) # 恢复后重跑同批
    buckets=core.load(store,"pay")["buckets"]
    assert buckets["2026-11-15T01:00:00Z"]["count"]==3 and buckets["2026-11-15T02:00:00Z"]["count"]==2
    assert buckets["2026-11-15T02:00:00Z"]["errors"]==1

def test_catchup_status_and_bucket_diff_visible(store):
    assert seed_pay(store)["catchup"]=="active"
    core.disable(store,"pay")
    core.advance_service(store,"pay",[EV("p4","2026-11-15T01:40:00Z")])
    pending=core.enable(store,"pay",limit=0)
    assert pending["catchup_label"]=="待追赶"
    done=core.enable(store,"pay")
    assert done["catchup_label"]=="已追平" and done["diff_count"]==1
    overview=core.overview(store)
    assert overview["services"][0]["diffs"][0]["hour"]=="2026-11-15T01:00:00Z"

def test_legacy_run_untouched_without_service(store):
    out=run({"store":store}) # 无 service 时保持原样例契约
    assert out["late_count"]==1 and out["valid_count"]==2 and store.all_services()==[]
