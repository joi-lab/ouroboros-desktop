"""Vedyakhin ops: pdca_task, morning_briefing, weekly_report, decision_memo, inbox_route."""

from __future__ import annotations

import json
import pathlib
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from ouroboros.tools.registry import ToolContext, ToolEntry
from ouroboros.utils import utc_now_iso

log = logging.getLogger(__name__)

_TASKS_SUBDIR = "memory/pdca_tasks"
_PRIORITY_ORDER = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _tasks_dir(ctx: ToolContext) -> pathlib.Path:
    p = ctx.drive_root / _TASKS_SUBDIR
    p.mkdir(parents=True, exist_ok=True)
    return p


def _load_task(ctx: ToolContext, task_id: str) -> Optional[Dict]:
    p = _tasks_dir(ctx) / f"{task_id}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save_task(ctx: ToolContext, task: Dict) -> None:
    p = _tasks_dir(ctx) / f"{task['id']}.json"
    p.write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_all_tasks(ctx: ToolContext) -> List[Dict]:
    tasks = []
    for f in sorted(_tasks_dir(ctx).glob("*.json")):
        try:
            tasks.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            continue
    return tasks


# ---------------------------------------------------------------------------
# 1. pdca_task
# ---------------------------------------------------------------------------

def _pdca_task(
    ctx: ToolContext,
    action: str,
    task_id: str = "",
    title: str = "",
    description: str = "",
    priority: str = "P2",
    assignee: str = "",
    deadline: str = "",
    metrics: str = "",
    phase: str = "",
    notes: str = "",
    status: str = "",
    result: str = "",
    lessons_learned: str = "",
) -> str:
    action = action.lower().strip()

    if action == "create":
        tid = uuid.uuid4().hex[:8]
        task: Dict[str, Any] = {
            "id": tid,
            "title": title,
            "description": description,
            "priority": priority,
            "assignee": assignee,
            "deadline": deadline,
            "metrics": metrics,
            "phases": {
                "plan":  {"notes": "", "timestamp": "", "status": "pending"},
                "do":    {"notes": "", "timestamp": "", "status": "pending"},
                "check": {"notes": "", "timestamp": "", "status": "pending"},
                "act":   {"notes": "", "timestamp": "", "status": "pending"},
            },
            "created_at": utc_now_iso(),
            "closed_at": None,
            "result": None,
            "lessons_learned": None,
            "status": "open",
        }
        _save_task(ctx, task)
        return f"OK: task created id={tid} priority={priority} title={title!r}"

    if action == "update":
        task = _load_task(ctx, task_id)
        if task is None:
            return f"⚠️ Task not found: {task_id}"
        if phase:
            if phase not in task["phases"]:
                return f"⚠️ Invalid phase: {phase}. Must be plan/do/check/act"
            task["phases"][phase]["notes"] = notes
            task["phases"][phase]["timestamp"] = utc_now_iso()
            task["phases"][phase]["status"] = status or "in_progress"
        elif status:
            task["status"] = status
        _save_task(ctx, task)
        return f"OK: task {task_id} updated"

    if action == "list":
        open_tasks = [t for t in _load_all_tasks(ctx) if t.get("status") == "open"]
        open_tasks.sort(key=lambda t: _PRIORITY_ORDER.get(t.get("priority", "P3"), 3))
        if not open_tasks:
            return "No open tasks."
        lines = ["# Open PDCA Tasks\n"]
        for t in open_tasks:
            lines.append(
                f"- [{t['priority']}] **{t['id']}** — {t['title']}"
                f"  | assignee: {t.get('assignee') or '—'}"
                f"  | deadline: {t.get('deadline') or '—'}"
            )
        return "\n".join(lines)

    if action == "check":
        task = _load_task(ctx, task_id)
        if task is None:
            return f"⚠️ Task not found: {task_id}"
        return json.dumps(task, ensure_ascii=False, indent=2)

    if action == "close":
        task = _load_task(ctx, task_id)
        if task is None:
            return f"⚠️ Task not found: {task_id}"
        task["status"] = "closed"
        task["closed_at"] = utc_now_iso()
        task["result"] = result
        task["lessons_learned"] = lessons_learned
        _save_task(ctx, task)
        return f"OK: task {task_id} closed result={result}"

    return f"⚠️ Unknown action: {action}. Must be create/update/list/check/close"


# ---------------------------------------------------------------------------
# 2. morning_briefing
# ---------------------------------------------------------------------------

def _morning_briefing(ctx: ToolContext) -> str:
    now = utc_now_iso()
    today = now[:10]
    tasks = _load_all_tasks(ctx)
    open_tasks = [t for t in tasks if t.get("status") == "open"]
    open_tasks.sort(key=lambda t: _PRIORITY_ORDER.get(t.get("priority", "P3"), 3))

    overdue = [t for t in open_tasks if t.get("deadline") and t["deadline"] < today]
    critical = [t for t in open_tasks if t.get("priority") in ("P0", "P1")]

    chat_snippet = ""
    chat_path = ctx.drive_root / "logs" / "chat.jsonl"
    if chat_path.exists():
        try:
            raw_lines = chat_path.read_text(encoding="utf-8").strip().splitlines()[-5:]
            entries = []
            for raw in raw_lines:
                try:
                    e = json.loads(raw)
                    entries.append(
                        f"  [{e.get('ts','')[:16]}] {e.get('direction','')}: "
                        f"{str(e.get('text',''))[:120]}"
                    )
                except Exception:
                    pass
            chat_snippet = "\n".join(entries)
        except Exception:
            pass

    lines = [
        f"# Утренний брифинг — {today}",
        f"_Сгенерировано: {now}_",
        "",
        "## Просроченные / критические задачи",
    ]
    if overdue:
        for t in overdue:
            lines.append(
                f"- **[{t['priority']}] {t['id']}** — {t['title']}"
                f" | дедлайн: {t['deadline']} | исполнитель: {t.get('assignee') or '—'}"
            )
    else:
        lines.append("- Нет просроченных задач.")

    lines += ["", "## Приоритеты на сегодня (P0–P1)"]
    if critical:
        for t in critical:
            lines.append(
                f"- **[{t['priority']}] {t['id']}** — {t['title']}"
                f" | исполнитель: {t.get('assignee') or '—'}"
            )
    else:
        lines.append("- Критических задач нет.")

    by_priority: Dict[str, int] = {}
    for t in open_tasks:
        p = t.get("priority", "—")
        by_priority[p] = by_priority.get(p, 0) + 1

    lines += ["", "## Метрики"]
    lines.append(f"- Открытых задач: **{len(open_tasks)}**  |  Просрочено: **{len(overdue)}**")
    for p in ["P0", "P1", "P2", "P3"]:
        if p in by_priority:
            lines.append(f"  - {p}: {by_priority[p]}")

    lines += ["", "## Риски и блокеры"]
    blockers = []
    for t in open_tasks:
        check_notes = (t.get("phases") or {}).get("check", {}).get("notes") or ""
        if "блокер" in check_notes.lower() or "риск" in check_notes.lower():
            blockers.append(f"- [{t['id']}] {t['title']}: {check_notes[:100]}")
    lines += blockers if blockers else ["- Явных блокеров не выявлено."]

    if chat_snippet:
        lines += ["", "## Последние сообщения", "```", chat_snippet, "```"]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 3. weekly_report
# ---------------------------------------------------------------------------

def _weekly_report(ctx: ToolContext, week_start: str = "") -> str:
    now = utc_now_iso()
    if not week_start:
        today = datetime.now(timezone.utc).date()
        week_start = str(today - timedelta(days=today.weekday()))

    try:
        ws = datetime.strptime(week_start, "%Y-%m-%d").date()
        week_end = str(ws + timedelta(days=6))
    except Exception:
        week_end = ""

    tasks = _load_all_tasks(ctx)
    created = [t for t in tasks if (t.get("created_at") or "")[:10] >= week_start]
    closed = [
        t for t in tasks
        if t.get("status") == "closed" and (t.get("closed_at") or "")[:10] >= week_start
    ]
    succeeded = [t for t in closed if t.get("result") == "success"]
    failed    = [t for t in closed if t.get("result") == "fail"]
    pivoted   = [t for t in closed if t.get("result") == "pivot"]
    open_tasks = [t for t in tasks if t.get("status") == "open"]

    lines = [
        f"# Еженедельный отчёт: {week_start} — {week_end or '?'}",
        f"_Сгенерировано: {now}_",
        "",
        "## Факт / план",
        f"- Задач создано: **{len(created)}**",
        f"- Задач закрыто: **{len(closed)}**  "
        f"(выполнено: {len(succeeded)} | не выполнено: {len(failed)} | пивот: {len(pivoted)})",
        f"- В работе (открыто): **{len(open_tasks)}**",
        "",
        "## Ключевые достижения",
    ]
    if succeeded:
        for t in succeeded:
            lines.append(f"- [{t['priority']}] **{t['title']}** (id: {t['id']})")
            if t.get("lessons_learned"):
                lines.append(f"  _Выводы: {t['lessons_learned']}_")
    else:
        lines.append("- Успешно закрытых задач за неделю нет.")

    lines += ["", "## Блокеры и проблемы"]
    if failed:
        for t in failed:
            lines.append(f"- [{t['priority']}] {t['title']}: {t.get('lessons_learned') or '—'}")
    else:
        lines.append("- Критических блокеров не зафиксировано.")

    lines += ["", "## Приоритеты следующей недели"]
    next_up = sorted(open_tasks, key=lambda t: _PRIORITY_ORDER.get(t.get("priority", "P3"), 3))[:5]
    if next_up:
        for t in next_up:
            lines.append(
                f"- [{t['priority']}] {t['title']}"
                f" | дедлайн: {t.get('deadline') or '—'} | {t.get('assignee') or '—'}"
            )
    else:
        lines.append("- Открытых задач нет.")

    conserv_lo = max(1, len(closed) - 1)
    conserv_hi = len(closed) + 1
    lines += [
        "",
        "## PDCA-циклы",
        f"- Завершено за неделю: **{len(closed)}**",
        f"- Консервативный прогноз на следующую неделю: {conserv_lo}–{conserv_hi} циклов",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 4. decision_memo
# ---------------------------------------------------------------------------

def _decision_memo(
    ctx: ToolContext,
    topic: str,
    context: str,
    options: List[str],
) -> str:
    now = utc_now_iso()
    recommended = options[0] if options else "—"
    opts_text = "\n".join(f"{i + 1}. {o}" for i, o in enumerate(options))

    sections = [
        f"# Аналитическая записка: {topic}",
        f"_Дата: {now[:10]} | Автор: Цифровой Ведяхин (P9)_\n",
        f"## 1. Масштаб проблемы (цифры)\n{context}\n",
        "## 2. Специфика Сбера / наша позиция\n"
        "Сбер как системообразующий институт несёт повышенную ответственность за устойчивость "
        "решений. Любое решение должно соответствовать стратегическим приоритетам Группы "
        "и минимизировать регуляторные и репутационные риски.\n",
        "## 3. Прогноз\n"
        "- Краткосрочный (3–6 мес.): требует дополнительной аналитики.\n"
        "- Долгосрочный (1–3 года): зависит от выбранного варианта.\n",
        "## 4. Связь с национальными приоритетами\n"
        "Решение учитывает национальные цели: цифровая трансформация, "
        "технологический суверенитет, социальная ответственность бизнеса.\n",
        "## 5. Международный контекст\n"
        "Мировые тренды подтверждают актуальность. Лучшие практики рекомендуют "
        "взвешенный подход с поэтапным внедрением изменений.\n",
        f"---\n\n## Варианты решения\n{opts_text}\n",
        f"## Рекомендация\n**Рекомендуемый вариант: {recommended}**\n\n"
        "Обоснование: оптимальный баланс скорости, управляемости рисков и стратегических "
        "ориентиров Группы. Приступить после согласования с профильными подразделениями.\n",
        "---\n_Окончательное решение остаётся за руководством._",
    ]
    return "\n".join(sections)


# ---------------------------------------------------------------------------
# 5. inbox_route
# ---------------------------------------------------------------------------

_KW_DECISION = ["решить", "согласовать", "одобрить", "утвердить", "решение", "разрешить"]
_KW_DELEGATE  = ["поручить", "передать", "назначить", "пусть", "делегировать", "направить"]
_KW_INFO      = ["fyi", "к сведению", "информирую", "сообщаю", "отчёт", "данные", "результат"]
_KW_CONTROL   = ["проверить", "контроль", "статус", "как дела", "что с", "где", "выполнено"]


def _classify(msg: str) -> str:
    ml = msg.lower()
    for kw in _KW_DECISION:
        if kw in ml:
            return "decision_needed"
    for kw in _KW_DELEGATE:
        if kw in ml:
            return "delegate"
    for kw in _KW_INFO:
        if kw in ml:
            return "info_only"
    for kw in _KW_CONTROL:
        if kw in ml:
            return "control_check"
    return "info_only"


_DRAFTS = {
    "decision_needed": (
        "{sender}, получил. Вопрос рассмотрю, дам ответ до конца рабочего дня. "
        "Подготовьте краткую аналитическую записку с вариантами решения."
    ),
    "delegate": (
        "{sender}, принято. Задача будет делегирована профильному подразделению. "
        "Прошу обеспечить контроль исполнения и доложить о результате."
    ),
    "info_only": (
        "{sender}, информация получена. Принято к сведению. "
        "Если потребуются дополнительные данные — запрошу."
    ),
    "control_check": (
        "{sender}, статус вопроса уточняется. "
        "Исполнитель предоставит обновление в ближайшее время."
    ),
}

_ACTIONS = {
    "decision_needed": "Подготовить аналитическую записку и вынести на рассмотрение.",
    "delegate":        "Определить исполнителя и поставить задачу через PDCA-трекер.",
    "info_only":       "Принять к сведению, при необходимости — занести в базу знаний.",
    "control_check":   "Запросить статус у исполнителя, обновить PDCA-фазу.",
}

_PRI = {"decision_needed": "P1", "delegate": "P2", "control_check": "P2", "info_only": "P3"}


def _inbox_route(ctx: ToolContext, message: str, sender: str = "Unknown") -> str:
    cls = _classify(message)
    draft = _DRAFTS.get(cls, "{sender}, сообщение получено.").format(sender=sender)
    result = {
        "classification": cls,
        "priority": _PRI.get(cls, "P3"),
        "sender": sender,
        "recommended_action": _ACTIONS.get(cls, "Принять к сведению."),
        "draft_response": draft,
        "message_preview": message[:200],
    }
    return json.dumps(result, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------

def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry("pdca_task", {
            "name": "pdca_task",
            "description": (
                "PDCA Task Manager (P9 / Digital Vedyakhin). "
                "Plan-Do-Check-Act cycle. actions: create, update, list, check, close. "
                "Cards stored at memory/pdca_tasks/{id}.json on Drive."
            ),
            "parameters": {"type": "object", "properties": {
                "action":          {"type": "string", "enum": ["create", "update", "list", "check", "close"]},
                "task_id":         {"type": "string"},
                "title":           {"type": "string"},
                "description":     {"type": "string"},
                "priority":        {"type": "string", "enum": ["P0", "P1", "P2", "P3"]},
                "assignee":        {"type": "string"},
                "deadline":        {"type": "string", "description": "YYYY-MM-DD"},
                "metrics":         {"type": "string", "description": "Success metrics (create)"},
                "phase":           {"type": "string", "enum": ["plan", "do", "check", "act"]},
                "notes":           {"type": "string"},
                "status":          {"type": "string"},
                "result":          {"type": "string", "enum": ["success", "fail", "pivot"]},
                "lessons_learned": {"type": "string"},
            }, "required": ["action"]},
        }, _pdca_task),

        ToolEntry("morning_briefing", {
            "name": "morning_briefing",
            "description": (
                "Generate morning briefing in Vedyakhin style: "
                "overdue/critical tasks, today's priorities, key metrics, risks and blockers."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        }, _morning_briefing),

        ToolEntry("weekly_report", {
            "name": "weekly_report",
            "description": (
                "Generate weekly report in Vedyakhin style: "
                "plan/fact comparison, achievements, blockers, next week priorities."
            ),
            "parameters": {"type": "object", "properties": {
                "week_start": {"type": "string", "description": "Week start date YYYY-MM-DD (defaults to current week Monday)"},
            }, "required": []},
        }, _weekly_report),

        ToolEntry("decision_memo", {
            "name": "decision_memo",
            "description": (
                "Generate analytical decision memo in Vedyakhin style: "
                "problem scale, Sber specifics, forecast, national priorities, "
                "international context, recommended option."
            ),
            "parameters": {"type": "object", "properties": {
                "topic":   {"type": "string", "description": "Decision topic"},
                "context": {"type": "string", "description": "Background context and data"},
                "options": {"type": "array", "items": {"type": "string"},
                            "description": "List of options (first will be recommended)"},
            }, "required": ["topic", "context", "options"]},
        }, _decision_memo),

        ToolEntry("inbox_route", {
            "name": "inbox_route",
            "description": (
                "Classify incoming request and draft a response in Vedyakhin style. "
                "Types: decision_needed / delegate / info_only / control_check."
            ),
            "parameters": {"type": "object", "properties": {
                "message": {"type": "string", "description": "Incoming message text"},
                "sender":  {"type": "string", "description": "Sender name or role"},
            }, "required": ["message"]},
        }, _inbox_route),
    ]
