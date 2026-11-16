from fastapi import APIRouter, Body, Request
from . import service
from .service import execute
router = APIRouter(tags=["M5-迟到修订"])

@router.get("/watermark")
def page(request: Request):
    data = execute()
    data["services"] = service.overview(request.app.state.store)["services"]
    return request.app.state.templates.TemplateResponse(request, "m5_watermark.html", {"title": "迟到修订", "data": data})

@router.post("/api/watermark/advance")
def api(request: Request, payload: dict = Body(default={})):
    payload = payload or {}
    if "service" in payload:
        payload["store"] = request.app.state.store
    return execute(payload or None)

@router.post("/api/watermark/service/disable")
def api_disable(request: Request, payload: dict = Body(default={})):
    return service.disable(request.app.state.store, (payload or {}).get("service") or "pay")

@router.post("/api/watermark/service/enable")
def api_enable(request: Request, payload: dict = Body(default={})):
    p = payload or {}
    return service.enable(request.app.state.store, p.get("service") or "pay", p.get("minutes", 15), p.get("limit"))

@router.get("/api/watermark/services")
def api_services(request: Request):
    return service.overview(request.app.state.store)
