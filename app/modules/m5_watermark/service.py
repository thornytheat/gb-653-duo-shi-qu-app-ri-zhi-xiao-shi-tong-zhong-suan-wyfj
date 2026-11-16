from . import core

def execute(payload: dict | None = None) -> dict:
    return core.run(payload or core.SAMPLE)

def disable(store, service: str) -> dict:
    return core.disable(store, service)

def enable(store, service: str, minutes: int = 15, limit: int | None = None) -> dict:
    return core.enable(store, service, minutes, limit)

def overview(store) -> dict:
    return core.overview(store)
