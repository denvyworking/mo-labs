"""Веб-демонстрация МАС (только стандартная библиотека).

    python lab1/web/server.py [--port 8000]
    открыть http://localhost:8000

Режимы ЛР2–ЛР4 подхватываются автоматически из labN/plugin.py.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lab1.mas import SimConfig, Simulation, registry  # noqa: E402
from lab1.mas.metrics import LABELS  # noqa: E402

INDEX = Path(__file__).with_name("index.html")
MAX_MSG_EVENTS = 6000
FIELD_TYPES = {f.name: f.type for f in dataclasses.fields(SimConfig)}
NUMERIC = {"seed", "n_couriers", "heavy_couriers", "n_orders", "n_stations", "station_slots", "order_rate",
           "msg_latency", "grid_power_limit", "max_ticks", "telemetry_period"}


def build_config(q: dict[str, str]) -> SimConfig:
    kw: dict = {"record_frames": True}
    for k, v in q.items():
        if k not in FIELD_TYPES or v == "":
            continue
        if k in ("allocation", "charging", "scenario", "defense"):
            kw[k] = v
        elif k in NUMERIC:
            kw[k] = float(v) if k == "order_rate" else int(float(v))
    return SimConfig(**kw)


def run(q: dict[str, str]) -> dict:
    cfg = build_config(q)
    sim = Simulation(cfg)
    metrics = sim.run()
    events, n_msg = [], 0
    for e in sim.trace.events:
        if e["kind"] == "start":
            continue
        if e["kind"] == "msg":
            n_msg += 1
            if n_msg > MAX_MSG_EVENTS:
                continue
            e = {"t": e["t"], "kind": "msg", "from": e["from"], "to": e["to"], "perf": e["perf"],
                 "proto": e["proto"], "content": e["content"]}
        events.append(e)
    return {"config": sim.cfg.to_dict(), "static": sim.static_info(), "frames": sim.frames,
            "events": events, "metrics": metrics, "labels": LABELS, "truncated_msgs": max(0, n_msg - MAX_MSG_EVENTS)}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False, default=list).encode("utf-8"),
                   "application/json; charset=utf-8")

    def do_GET(self) -> None:
        url = urlparse(self.path)
        try:
            if url.path in ("/", "/index.html"):
                self._send(200, INDEX.read_bytes(), "text/html; charset=utf-8")
            elif url.path == "/api/modes":
                defaults = {k: v for k, v in SimConfig().to_dict().items() if k in NUMERIC or k in registry.CATEGORIES}
                self._json({"modes": registry.listing(), "defaults": defaults,
                            "plugin_errors": registry.PLUGIN_ERRORS})
            elif url.path == "/api/run":
                q = {k: v[-1] for k, v in parse_qs(url.query).items()}
                self._json(run(q))
            else:
                self._send(404, b"not found", "text/plain")
        except Exception as e:  # ошибка режима не должна ронять сервер
            self._json({"error": str(e), "trace": traceback.format_exc()}, 500)

    def log_message(self, fmt, *args) -> None:
        sys.stderr.write("[web] " + fmt % args + "\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    a = ap.parse_args()
    errors = registry.load_lab_plugins()
    for lab, err in errors.items():
        print(f"[web] не удалось загрузить {lab}: {err.splitlines()[-1]}")
    print(f"[web] режимы: { {c: list(registry.MODES[c]) for c in registry.CATEGORIES} }")
    print(f"[web] http://localhost:{a.port}")
    ThreadingHTTPServer(("127.0.0.1", a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
