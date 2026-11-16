"""Трасса выполнения: все события и сообщения с отметкой такта."""
from __future__ import annotations

import json
from pathlib import Path


class Trace:
    def __init__(self, sim, messages: bool = True):
        self.sim = sim
        self.messages = messages
        self.events: list[dict] = []

    def event(self, kind: str, **data) -> None:
        self.events.append({"t": self.sim.tick, "kind": kind, **data})

    def filter(self, kind: str | None = None, agent: str | None = None) -> list[dict]:
        return [e for e in self.events
                if (kind is None or e["kind"] == kind) and (agent is None or e.get("agent") == agent)]

    def save_jsonl(self, path: str | Path) -> None:
        with open(path, "w", encoding="utf-8") as f:
            for e in self.events:
                f.write(json.dumps(e, ensure_ascii=False, default=str) + "\n")
