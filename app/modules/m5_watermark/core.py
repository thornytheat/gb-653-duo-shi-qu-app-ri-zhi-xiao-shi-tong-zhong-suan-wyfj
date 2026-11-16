from datetime import datetime, timedelta, timezone
from app.errors import DomainError
from app.manifest import canonical_digest

SAMPLE={"current":"2026-11-15T01:00:00Z","events":["2026-11-15T01:05:00Z","2026-11-15T00:40:00Z"],"minutes":15}
DEFAULT_SERVICE="pay"
CATCHUP_LABELS={"active":"正常","disabled":"已停用","pending":"待追赶","caught_up":"已追平"}

def parse(value): return datetime.fromisoformat(value.replace("Z","+00:00")).astimezone(timezone.utc)
def advance(current, events, minutes=15):
    old=parse(current); newest=max([old]+[parse(x) for x in events]); proposed=newest-timedelta(minutes=minutes); new=max(old,proposed)
    return {"old":old.isoformat(),"new":new.isoformat(),"late":[x for x in events if parse(x)<old],"advanced":new>old}

def event_id(ev): return str(ev.get("request_id") or canonical_digest(ev))

def new_state(service):
    return {"service":service,"status":"active","generation":1,"watermark":None,
            "held":[],"backlog":[],"received":0,"applied":[],"buckets":{},"diffs":[]}

def load(store, service):
    return store.get_service(service) or new_state(service)

def catchup(state):
    if state["status"]=="disabled": return "disabled"
    if state["received"]<len(state["backlog"]): return "pending"
    if state["backlog"]: return "caught_up"
    return "active"

def apply_event(state, ev):
    """按事件落桶；水位之前的旧桶记修订差异。已接收过的事件不重复计数。"""
    eid=event_id(ev)
    if eid in state["applied"]: return None
    ts=parse(ev["timestamp"]); hour=ts.strftime("%Y-%m-%dT%H:00:00Z")
    buckets=state["buckets"]; before=dict(buckets.get(hour,{"count":0,"errors":0}))
    bucket=buckets.setdefault(hour,{"count":0,"errors":0})
    bucket["count"]+=1; bucket["errors"]+=int(int(ev.get("status",200))>=500)
    state["applied"].append(eid)
    wm=state["watermark"]
    if wm and ts<parse(wm):
        diff={"hour":hour,"service":state["service"],"before":before,"after":dict(bucket)}
        state["diffs"].append(diff); return diff
    return None

def advance_watermark(state, events, minutes=15):
    old=parse(state["watermark"]) if state["watermark"] else None
    times=[parse(ev["timestamp"]) for ev in events]
    if old is None and not times: return None,None
    newest=max(([old] if old else [])+times); proposed=newest-timedelta(minutes=minutes)
    new=max(old,proposed) if old else proposed
    state["watermark"]=new.isoformat()
    return old,new

def catch_up(state, minutes=15, limit=None):
    """从已接收位置继续追赶积压；位置随状态一次提交，中断后从该位置恢复。"""
    diffs=[]; done=0
    while state["received"]<len(state["backlog"]):
        if limit is not None and done>=limit: break
        diff=apply_event(state,state["backlog"][state["received"]])
        state["received"]+=1; done+=1
        if diff: diffs.append(diff)
    if state["backlog"] and state["received"]>=len(state["backlog"]):
        advance_watermark(state,state["backlog"],minutes)
    return diffs

def view(state, status="READY", reason="OK", diffs=None, input_count=0, old=None, new=None, late=None, held_now=None):
    remaining=len(state["backlog"])-state["received"]
    out={"status":status,"service":state["service"],"service_status":state["status"],
         "generation":state["generation"],"catchup":catchup(state),"catchup_label":CATCHUP_LABELS[catchup(state)],
         "watermark":state["watermark"],"held_count":len(state["held"]),"received":state["received"],
         "backlog_remaining":remaining,"bucket_count":len(state["buckets"]),
         "buckets":[dict({"hour":h},**state["buckets"][h]) for h in sorted(state["buckets"])],
         "diffs":state["diffs"] if diffs is None else diffs,"diff_count":len(state["diffs"]),
         "input_count":input_count,"valid_count":input_count,"quarantine_count":0,"reason":reason}
    if old is not None or new is not None: out["old"]=old.isoformat() if old else None; out["new"]=new.isoformat() if new else None; out["advanced"]=bool(new and (old is None or new>old))
    if late is not None: out["late"]=late; out["late_count"]=len(late)
    if held_now is not None: out["held_now"]=held_now
    return out

def disable(store, service=DEFAULT_SERVICE):
    """办理停用：冻结本服务进度，代次+1，此后到达的日志留待恢复。"""
    state=load(store,service)
    if state["status"]!="disabled":
        state["status"]="disabled"; state["generation"]+=1; store.save_service(state)
    return view(state,status="DISABLED")

def enable(store, service=DEFAULT_SERVICE, minutes=15, limit=None):
    """办理恢复：代次+1，停用期积压转入追赶队列，从已接收位置续跑，只修订本服务旧桶。"""
    state=load(store,service)
    if state["status"]=="disabled":
        state["status"]="active"; state["generation"]+=1
        state["backlog"]=state["backlog"]+state["held"]; state["held"]=[]
    diffs=catch_up(state,minutes,limit)
    store.save_service(state)
    return view(state,reason="REVISED" if diffs else "OK",diffs=diffs)

def advance_service(store, service, events, generation=None, minutes=15, limit=None):
    state=load(store,service); g0=state["generation"]
    if generation is not None and generation!=g0:
        raise DomainError("STALE_VERSION",f"服务 {service} 代次已过期：提交 {generation}，当前 {g0}")
    if state["status"]=="disabled":
        known={event_id(e) for e in state["held"]}|set(state["applied"])|{event_id(e) for e in state["backlog"]}
        held_now=[]
        for ev in events:
            if event_id(ev) not in known: known.add(event_id(ev)); state["held"].append(ev); held_now.append(ev)
        out=view(state,status="HELD",reason="HELD",input_count=len(events),held_now=[e.get("timestamp") for e in held_now])
    elif state["received"]<len(state["backlog"]):
        known={event_id(e) for e in state["backlog"]}|set(state["applied"])
        for ev in events:
            if event_id(ev) not in known: known.add(event_id(ev)); state["backlog"].append(ev)
        diffs=catch_up(state,minutes,limit)
        out=view(state,reason="REVISED" if diffs else "OK",diffs=diffs,input_count=len(events))
    else:
        diffs=[d for d in (apply_event(state,ev) for ev in events) if d]
        late=[ev.get("timestamp") for ev in events if state["watermark"] and parse(ev["timestamp"])<parse(state["watermark"])]
        old,new=advance_watermark(state,events,minutes)
        out=view(state,reason="LATE" if late else "OK",diffs=diffs,input_count=len(events),old=old,new=new,late=late)
    if not store.save_service_cas(state,g0):
        raise DomainError("STALE_VERSION",f"服务 {service} 代次已并发变更，请按当前代次重试")
    return out

def overview(store):
    services=[view(s) for s in store.all_services()]
    return {"status":"READY","services":services,"service_count":len(services),
            "pending_count":sum(1 for s in services if s["catchup"]=="pending"),
            "disabled_count":sum(1 for s in services if s["catchup"]=="disabled"),
            "input_count":len(services),"valid_count":len(services),"quarantine_count":0,"reason":"OK"}

def run(payload):
    payload=payload or {}
    if payload.get("service") and payload.get("store") is not None:
        return advance_service(payload["store"],payload["service"],payload.get("events",[]),
                               payload.get("generation"),payload.get("minutes",15),payload.get("limit"))
    d=SAMPLE|payload; out=advance(d["current"],d["events"],d.get("minutes",15)); return {"status":"READY","input_count":len(d["events"]),"valid_count":len(d["events"]),"quarantine_count":0,"late_count":len(out["late"]),**out,"reason":"LATE" if out["late"] else "OK"}
