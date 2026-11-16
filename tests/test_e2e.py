def test_all_module_pages(client):
    for path in ['/logs', '/time', '/events', '/buckets', '/watermark', '/runs']:
        response = client.get(path)
        assert response.status_code == 200
        assert "panel" in response.text

def test_all_module_apis_and_idempotent_run(client):
    for path in ['/api/logs/import', '/api/time/normalize', '/api/events/dedupe', '/api/buckets/rebuild', '/api/watermark/advance']:
        response = client.post(path, json={})
        assert response.status_code == 200
    first = client.get("/api/runs").json()
    second = client.get("/api/runs").json()
    assert len(first["runs"]) == len(second["runs"]) == 1
    assert first == second

def test_watermark_service_lifecycle(client):
    ev = lambda rid, ts, status=200: {"request_id": rid, "timestamp": ts, "status": status}
    seeded = client.post("/api/watermark/advance", json={"service": "pay", "events": [ev("p1", "2026-11-15T01:05:00Z"), ev("p2", "2026-11-15T02:10:00Z")]}).json()
    assert seeded["catchup"] == "active" and seeded["generation"] == 1
    disabled = client.post("/api/watermark/service/disable", json={"service": "pay"}).json()
    assert disabled["status"] == "DISABLED" and disabled["generation"] == 2
    held = client.post("/api/watermark/advance", json={"service": "pay", "events": [ev("p3", "2026-11-15T01:40:00Z")]}).json()
    assert held["status"] == "HELD" and held["watermark"] == seeded["watermark"]
    stale = client.post("/api/watermark/advance", json={"service": "pay", "generation": 1, "events": [ev("p9", "2026-11-15T03:00:00Z")]})
    assert stale.status_code == 422 and stale.json()["error"] == "STALE_VERSION"
    resumed = client.post("/api/watermark/service/enable", json={"service": "pay"}).json()
    assert resumed["catchup"] == "caught_up" and resumed["diff_count"] == 1
    assert resumed["diffs"][0]["hour"] == "2026-11-15T01:00:00Z"
    rerun = client.post("/api/watermark/service/enable", json={"service": "pay"}).json()
    assert rerun["diff_count"] == 1 and rerun["buckets"] == resumed["buckets"]
    services = client.get("/api/watermark/services").json()
    assert services["services"][0]["catchup_label"] == "已追平"
    page = client.get("/watermark")
    assert page.status_code == 200 and "服务启停与追赶" in page.text and "桶差异" in page.text
