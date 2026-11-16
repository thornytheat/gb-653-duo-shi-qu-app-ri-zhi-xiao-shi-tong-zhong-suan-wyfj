from __future__ import annotations
import json
from pathlib import Path
import sqlite3

class Store:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS runs (digest TEXT PRIMARY KEY, run_id TEXT NOT NULL, result TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS service_state (service TEXT PRIMARY KEY, generation INTEGER NOT NULL, state TEXT NOT NULL)")

    def connect(self):
        return sqlite3.connect(self.path)

    def record(self, digest: str, run_id: str, result: dict) -> dict:
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO runs VALUES (?, ?, ?)",
                       (digest, run_id, json.dumps(result, ensure_ascii=False, sort_keys=True)))
            row = db.execute("SELECT run_id, result FROM runs WHERE digest=?", (digest,)).fetchone()
        return {"run_id": row[0], "result": json.loads(row[1])}

    def count(self) -> int:
        with self.connect() as db:
            return db.execute("SELECT COUNT(*) FROM runs").fetchone()[0]

    def all(self) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT run_id, digest, result FROM runs ORDER BY run_id").fetchall()
        return [{"run_id": r[0], "digest": r[1], "result": json.loads(r[2])} for r in rows]

    def get_service(self, service: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT state FROM service_state WHERE service=?", (service,)).fetchone()
        return json.loads(row[0]) if row else None

    def save_service(self, state: dict) -> None:
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO service_state VALUES (?, ?, ?)",
                       (state["service"], state["generation"], json.dumps(state, ensure_ascii=False, sort_keys=True)))

    def save_service_cas(self, state: dict, expected_generation: int) -> bool:
        """仅当库中代次仍为 expected_generation 时写入；并发推进守卫。"""
        payload = json.dumps(state, ensure_ascii=False, sort_keys=True)
        with self.connect() as db:
            cur = db.execute("UPDATE service_state SET generation=?, state=? WHERE service=? AND generation=?",
                            (state["generation"], payload, state["service"], expected_generation))
            if cur.rowcount:
                return True
            if db.execute("SELECT 1 FROM service_state WHERE service=?", (state["service"],)).fetchone():
                return False
            db.execute("INSERT OR IGNORE INTO service_state VALUES (?, ?, ?)",
                       (state["service"], state["generation"], payload))
            return db.execute("SELECT 1 FROM service_state WHERE service=? AND generation=?",
                              (state["service"], expected_generation)).fetchone() is not None

    def all_services(self) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT state FROM service_state ORDER BY service").fetchall()
        return [json.loads(r[0]) for r in rows]
