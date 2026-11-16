"""Анализ трасс выполнения (JSONL): выявление отказов по таксономии ЛР4.

    python lab4/analyze.py lab4/traces/*.jsonl             # сводка по каждой трассе
    python lab4/analyze.py lab4/traces/rogue_courier__none.jsonl --examples 5

Анализатор работает только с тем, что видно в трассе: сообщения (с истинным origin из журнала шины),
события агентов, события защиты и снимки истинного состояния курьеров (`truth`, если записаны).
Разметку атак сценариев он не использует. Результат — число отказов каждого класса и примеры строк.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lab1.mas import roles  # noqa: E402

# Таксономия отказов: код -> название
TAXONOMY = {
    "F1": "Заказ не доставлен",
    "F2": "Ложное подтверждение доставки",
    "F3": "Циклическая передача заказа",
    "F4": "Подмена отправителя",
    "F5": "Нарушение полномочий",
    "F6": "Противоречивые команды",
    "F7": "Дублирующая или ложная поездка",
    "F8": "Зависание у станции",
    "F9": "Разрядка в пути",
    "F10": "Недостоверная телеметрия",
    "F11": "Исполнение команд из данных",
    "F12": "Повтор сообщения",
    "F13": "Потеря сообщения",
}
CAUSES = {
    "not_registered": "заказ не принят диспетчером",
    "data_command": "отменён/закрыт командой из текста заказа",
    "false_confirm": "закрыт ложным подтверждением",
    "foreign_cancel": "отозван у исполнителя чужой командой",
    "cycle": "циклическая передача",
    "holder_stuck": "исполнитель завис у станции",
    "accepted_not_done": "принят, но не исполняется",
    "on_board": "посылка на борту, не довезена",
    "other": "прочее",
}
SUSPICIOUS_NOTE = re.compile(r"\bo\d{3}\b|\b[оo]\s?\d{3}\b|courier-\d+|систем|игнорир|отмен|назнач|(?:\w ){4,}\w",
                             re.IGNORECASE)
STUCK_TICKS = 20


def role_of(aid: str) -> str:
    if aid == "client":
        return "client"
    if aid.startswith("dispatcher"):
        return "dispatcher"
    if aid.startswith("courier-"):
        return "courier"
    if aid.startswith("station-"):
        return "station"
    return "unknown"


def strip_sig(content):
    if isinstance(content, dict):
        return {k: v for k, v in content.items() if k != "_sig"}
    return content


def load(path: str | Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def analyze(events: list[dict], max_examples: int = 3) -> dict:
    start = next((e for e in events if e["kind"] == "start"), {})
    cfg = start.get("config", {})
    by_kind = defaultdict(list)
    for e in events:
        by_kind[e["kind"]].append(e)
    # сообщения, отброшенные защитой у получателя, на систему не повлияли — в отказы их не включаем
    rejected = {e["id"] for e in by_kind["def_block"] if "at" in e}
    msgs = [e for e in by_kind["msg"] if e["id"] not in rejected]
    failures = Counter()
    examples = defaultdict(list)

    def ex(code: str, row: dict) -> None:
        failures[code] += 1
        if len(examples[code]) < max_examples:
            examples[code].append(row)

    # --- заказы и их судьба ---
    created = [e["order"] for e in by_kind["order_created"]]
    delivered_at = {}
    for e in by_kind["delivered"]:
        delivered_at.setdefault(e["order"], e["t"])
    notes = {}
    for m in msgs:
        if m["proto"] == "order" and m["perf"] == "request":
            c = m["content"] or {}
            notes.setdefault(c.get("id"), (c.get("note", ""), c.get("priority", "")))
    accepted_by_disp = {(m["content"] or {}).get("order_id") for m in msgs
                        if m["proto"] == "order" and m["perf"] == "agree" and m["from"] == "dispatcher"}
    released = defaultdict(list)
    for e in by_kind["released"]:
        released[e["order"]].append(e)
    holder = {}
    for e in by_kind["task_accepted"]:
        holder[e["order"]] = e["agent"]
    picked = {e["order"] for e in by_kind["picked"]}

    # --- F2 ложные подтверждения ---
    false_conf = {}
    for m in msgs:
        c = m["content"] or {}
        if m["proto"] == "order-status" and m["to"] == "client" and c.get("status") == "delivered":
            oid = c.get("order_id")
            if oid not in false_conf and (oid not in delivered_at or delivered_at[oid] > m["t"]):
                false_conf[oid] = m["t"]
                ex("F2", {"t": m["t"], "order": oid, "подтверждено": m["t"], "доставлено": delivered_at.get(oid, "нет")})

    # --- F4 подмена, F5 полномочия, F6 противоречия ---
    known = {k[2] for k in roles.PERMISSIONS}
    issuers = defaultdict(set)
    foreign_cancels = defaultdict(list)
    for m in msgs:
        origin, frm, to = m.get("origin") or "", m["from"], m["to"]
        if origin and origin != frm:
            ex("F4", {"t": m["t"], "от имени": frm, "истинный": origin, "кому": to, "сообщение": f"{m['perf']}/{m['proto']}",
                      "заказ": (m["content"] or {}).get("order_id") or (m["content"] or {}).get("id")})
        sr, rr = role_of(frm), role_of(to)
        if m["proto"] in known and not roles.is_allowed(sr, rr, m["proto"], m["perf"]):
            ex("F5", {"t": m["t"], "от": frm, "кому": to, "сообщение": f"{m['perf']}/{m['proto']}", "причина": "нет в PERMISSIONS"})
        elif sr == "dispatcher" and frm != "dispatcher" and rr in ("courier", "client"):
            ex("F5", {"t": m["t"], "от": frm, "кому": to, "сообщение": f"{m['perf']}/{m['proto']}", "причина": "не основной диспетчер"})
        if m["proto"] == "assign" and m["perf"] in ("request", "cancel") and sr == "dispatcher":
            c = m["content"] or {}
            oid = c.get("id") if m["perf"] == "request" else c.get("order_id")
            issuers[oid].add(origin or frm)
            if m["perf"] == "cancel" and (origin != "dispatcher" or frm != "dispatcher"):
                foreign_cancels[(oid, to)].append(m)
    for oid, who in issuers.items():
        if len(who) >= 2:
            ex("F6", {"order": oid, "источники команд": ", ".join(sorted(who))})

    # --- F3 циклы ---
    cycle_orders = set()
    for oid, evs in released.items():
        if len(evs) >= 3:
            chain = [e.get("courier") for e in evs]
            cycle_orders.add(oid)
            repeats = [c for c, n in Counter(chain).items() if n >= 2]
            ex("F3", {"order": oid, "передач": len(evs), "цепочка": "→".join(str(c) for c in chain[:6]) + ("→…" if len(chain) > 6 else ""),
                      "повторяются": ", ".join(repeats) or "—"})

    # --- F7 дублирующие и ложные поездки ---
    for e in by_kind["pickup_failed"]:
        ex("F7", {"t": e["t"], "курьер": e["agent"], "order": e["order"], "причина": e.get("reason")})

    # --- F8 зависание у станции (по снимкам истинного состояния) ---
    stuck_couriers = {}
    truth = by_kind["truth"]
    if truth:
        run = {}
        for snap in truth:
            seen = set()
            for cid, x, y, state, bat, ntasks, carrying in snap["c"]:
                seen.add(cid)
                if state in ("waiting_charge", "charging"):
                    r = run.get(cid)
                    if r is None or bat > r["bat"] + 0.5:
                        run[cid] = {"since": snap["t"], "bat": bat, "pos": (x, y), "tasks": ntasks}
                    else:
                        r["tasks"] = max(r["tasks"], ntasks)
                        r["until"] = snap["t"]
                else:
                    r = run.pop(cid, None)
                    if r and r.get("until", r["since"]) - r["since"] >= STUCK_TICKS:
                        stuck_couriers.setdefault(cid, r)
                        ex("F8", {"курьер": cid, "с такта": r["since"], "по такт": r["until"], "место": r["pos"],
                                  "заказов в очереди": r["tasks"], "итог": "ушёл"})
        for cid, r in run.items():
            if r.get("until", r["since"]) - r["since"] >= STUCK_TICKS:
                stuck_couriers[cid] = r
                ex("F8", {"курьер": cid, "с такта": r["since"], "по такт": r["until"], "место": r["pos"],
                          "заказов в очереди": r["tasks"], "итог": "до конца прогона"})

    # --- F9 разрядки ---
    for e in by_kind["stranded"]:
        ex("F9", {"t": e["t"], "курьер": e["agent"], "место": e.get("pos")})

    # --- F10 недостоверная телеметрия (сравнение с истиной в тот же такт) ---
    truth_at = {snap["t"]: {row[0]: row for row in snap["c"]} for snap in truth}
    for m in msgs:
        if m["proto"] != "telemetry" or m["t"] not in truth_at:
            continue
        row = truth_at[m["t"]].get(m["from"])
        if row is None:
            continue
        c = m["content"] or {}
        _, x, y, state, bat, ntasks, _ = row
        diffs = []
        if list(c.get("pos", [x, y])) != [x, y]:
            diffs.append(f"позиция {c.get('pos')} вместо {[x, y]}")
        if abs(float(c.get("battery", bat)) - bat) > 1.0:
            diffs.append(f"заряд {c.get('battery')} вместо {bat}")
        if len(c.get("tasks", [])) != ntasks:
            diffs.append(f"заказов {len(c.get('tasks', []))} вместо {ntasks}")
        if diffs:
            ex("F10", {"t": m["t"], "курьер": m["from"], "расхождение": "; ".join(diffs)})

    # --- F11 команды из данных ---
    for e in by_kind["note_cmd"]:   # пожелание о своём же заказе («срочно») — штатная функция, не отказ
        if e.get("executed") and not (e["action"] == "set_priority" and e.get("args") == [e["order"]]):
            ex("F11", {"t": e["t"], "заказ-носитель": e["order"], "действие": e["action"], "аргументы": e.get("args")})
    suspicious = {oid for oid, (note, prio) in notes.items() if SUSPICIOUS_NOTE.search(f"{note} {prio}")}

    # --- F12 повторы: шина записывает сообщение в момент входа в канал, поэтому копия, внесённая в канал
    # злоумышленником (повтор с задержкой или лавина), видна как второе такое же сообщение в тот же такт ---
    first_seen = {}
    for m in msgs:
        if m["proto"] == "telemetry":
            continue
        key = (m["t"], m["from"], m["to"], m["proto"], m["perf"], m.get("conv"), m.get("reply_to"),
               json.dumps(strip_sig(m["content"]), sort_keys=True, ensure_ascii=False))
        if key in first_seen:
            ex("F12", {"t": m["t"], "сообщение": f"{m['from']}→{m['to']} {m['perf']}/{m['proto']}",
                       "id оригинала": first_seen[key], "id копии": m["id"],
                       "заказ": (m["content"] or {}).get("order_id") or (m["content"] or {}).get("id") or "—"})
        else:
            first_seen[key] = m["id"]

    # --- F13 потери ---
    for e in by_kind["msg_lost"] + by_kind["msg_dropped"]:
        ex("F13", {"t": e["t"], "кому": e.get("to"), "протокол": e.get("proto"), "событие": e["kind"]})

    # --- F1 недоставленные и их причины ---
    data_cmd_targets = set()
    for e in by_kind["note_cmd"]:
        if e.get("executed") and e["action"] in ("cancel", "mark_delivered"):
            data_cmd_targets.update(e.get("args") or [])
    causes = Counter()
    for oid in created:
        if oid in delivered_at:
            continue
        h = holder.get(oid)
        if oid not in accepted_by_disp:
            cause = "not_registered"
        elif oid in data_cmd_targets:
            cause = "data_command"
        elif oid in false_conf:
            cause = "false_confirm"
        elif h and foreign_cancels.get((oid, h)):
            cause = "foreign_cancel"
        elif oid in cycle_orders:
            cause = "cycle"
        elif h in stuck_couriers:
            cause = "holder_stuck"
        elif oid in picked:
            cause = "on_board"
        elif h:
            cause = "accepted_not_done"
        else:
            cause = "other"
        causes[cause] += 1
        ex("F1", {"order": oid, "последний исполнитель": h or "—", "передач": len(released.get(oid, [])),
                  "причина": CAUSES[cause]})

    defense = Counter()
    for e in events:
        if e["kind"] == "def_block":
            defense[f"block:{e['layer']}"] += 1
        elif e["kind"].startswith("def_") and e["kind"] != "def_strike":
            defense[e["kind"]] += 1
    return {
        "scenario": cfg.get("scenario"), "defense": cfg.get("defense"), "seed": cfg.get("seed"),
        "orders": len(created), "delivered": len(delivered_at),
        "failures": {k: failures.get(k, 0) for k in TAXONOMY}, "causes": dict(causes),
        "suspicious_notes": len(suspicious), "defense_events": dict(defense),
        "examples": {k: v for k, v in examples.items()},
    }


def print_report(name: str, r: dict, n_examples: int) -> None:
    print(f"\n=== {name}: scenario={r['scenario']} defense={r['defense']} seed={r['seed']} "
          f"доставлено {r['delivered']}/{r['orders']}")
    for code, title in TAXONOMY.items():
        n = r["failures"][code]
        if n:
            print(f"  {code:<4} {title:<36} {n}")
    if r["causes"]:
        print("  причины недоставки: " + "; ".join(f"{CAUSES[k]} — {v}" for k, v in r["causes"].items()))
    if r["suspicious_notes"]:
        print(f"  заметок, похожих на инструкции: {r['suspicious_notes']}")
    if r["defense_events"]:
        print("  срабатывания защиты: " + ", ".join(f"{k}={v}" for k, v in sorted(r["defense_events"].items())))
    if n_examples:
        for code, rows in r["examples"].items():
            for row in rows[:n_examples]:
                print(f"    [{code}] " + ", ".join(f"{k}={v}" for k, v in row.items()))


def main() -> None:
    ap = argparse.ArgumentParser(description="Выявление отказов по трассам ЛР4")
    ap.add_argument("traces", nargs="+")
    ap.add_argument("--examples", type=int, default=0, help="сколько примеров каждого отказа печатать")
    ap.add_argument("--csv", help="сохранить сводку в CSV")
    ap.add_argument("--json", help="сохранить сводку с примерами в JSON")
    a = ap.parse_args()
    out = {}
    for path in a.traces:
        r = analyze(load(path), max_examples=max(3, a.examples))
        out[Path(path).name] = r
        print_report(Path(path).name, r, a.examples)
    if a.csv:
        Path(a.csv).parent.mkdir(parents=True, exist_ok=True)
        with open(a.csv, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["trace", "scenario", "defense", "seed", "orders", "delivered", *TAXONOMY, "suspicious_notes"])
            for name, r in out.items():
                w.writerow([name, r["scenario"], r["defense"], r["seed"], r["orders"], r["delivered"],
                            *[r["failures"][k] for k in TAXONOMY], r["suspicious_notes"]])
    if a.json:
        Path(a.json).write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str), encoding="utf-8")


if __name__ == "__main__":
    main()
