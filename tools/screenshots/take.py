"""Take the README screenshots of the operator UI, with no model and no gateway.

The built UI (desktop/out) is served as is; every API call it makes is answered here with
made-up demo data, including a scripted run streamed as Server-Sent Events. Nothing touches
the desktop, a model, or the real data dir.

    cd desktop && corepack pnpm build
    %VILAGENT_PYTHON% tools/screenshots/take.py        # writes docs/screenshots/*.png
"""

from __future__ import annotations

import dataclasses
import functools
import http.server
import json
import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import Page, Route, sync_playwright

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "desktop" / "out"
SHOTS = ROOT / "docs" / "screenshots"
VIEWPORT = {"width": 1600, "height": 960}
API = "/api/computer-use"
RUN_ID = "operator-run-demo"

sys.path.insert(0, str(ROOT / "agent"))
from vilagent import connections  # noqa: E402  (the real interface catalog, so the forms match)

NOW = datetime.now(UTC)


def iso(seconds_ago: float = 0) -> str:
    return (NOW - timedelta(seconds=seconds_ago)).isoformat()


# --- the demo task ------------------------------------------------------------------

PROMPT = "Check tomorrow's weather in Istanbul and save a short summary as weather.txt on the Desktop"

STEPS = [
    ("s1", "Open the browser and search for tomorrow's weather in Istanbul", "A forecast for Istanbul is on screen"),
    ("s2", "Read the forecast: high and low temperature, chance of rain and wind", "The forecast values are noted"),
    ("s3", "Open Notepad", "An empty Notepad window is focused"),
    ("s4", "Type the weather summary into Notepad", "The summary is in the Notepad document"),
    ("s5", "Save the file as weather.txt on the Desktop", "weather.txt exists on the Desktop"),
]


def plan_steps(statuses: list[str]) -> list[dict]:
    summaries = {
        "s1": "Searched the web; the forecast card for Istanbul is showing.",
        "s2": "Tomorrow: 17°C / 11°C, 40% chance of rain, NE wind 18 km/h.",
        "s3": "Notepad is open and focused.",
        "s4": "The summary is typed into Notepad.",
        "s5": "Saved C:\\Users\\demo\\Desktop\\weather.txt.",
    }
    return [
        {
            "step_id": step_id,
            "instruction": instruction,
            "completion_criteria": criteria,
            "max_actions": 12,
            "status": status,
            "requires_vision": True,
            "summary": summaries[step_id] if status == "completed" else None,
        }
        for (step_id, instruction, criteria), status in zip(STEPS, statuses, strict=True)
    ]


def activity(statuses: list[str], thought: str | None, event: str | None) -> dict:
    running = "running" in statuses
    return {
        "thread_id": "operator-thread",
        "run_id": RUN_ID,
        "agents": [
            {"agent_id": "computer_use_plan_execute", "role": "lead", "status": "idle" if running else "idle", "request_count": 2, "input_tokens": 3100, "output_tokens": 820, "total_tokens": 3920, "tool_calls": []},
            {
                "agent_id": "fara",
                "role": "computer_use",
                "status": "running" if running else "idle",
                "model_name": "microsoft/Fara-7B",
                "request_count": 11,
                "input_tokens": 38400,
                "output_tokens": 1250,
                "total_tokens": 39650,
                "tool_calls": [],
                "last_event": event,
                "current_thought": thought,
            },
        ],
        "plan_steps": plan_steps(statuses),
        "total_request_count": 13,
        "total_tokens": 43570,
    }


def budget(vision: int, vision_tokens: int, planner: int, planner_tokens: int, supervisor: int = 0, supervisor_tokens: int = 0) -> dict:
    return {
        "used": {"vision": vision, "planner": planner, "supervisor": supervisor},
        "tokens": {"vision": vision_tokens, "planner": planner_tokens, "supervisor": supervisor_tokens},
        "limits": {"vision": 120, "planner": 20, "supervisor": 20},
        "active_seconds": 74,
        "duration_seconds": 81,
    }


class Spans:
    """Builds trace events the way the gateway sends them."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []
        self.clock = 90.0

    def add(self, span_id: str, name: str, *, parent: str | None = None, kind: str = "node", status: str = "done", label: str | None = None, output: str | None = None, thinking: str | None = None, memory: list[str] | None = None, meta: dict | None = None) -> None:
        self.clock -= 3
        self.events.append(
            (
                "trace",
                {
                    "id": span_id,
                    "parent": parent,
                    "name": name,
                    "kind": kind,
                    "label": label,
                    "status": status,
                    "started_at": iso(self.clock),
                    "ended_at": None if status in ("running", "waiting") else iso(self.clock - 2),
                    "output": output,
                    "thinking": thinking,
                    "memory": memory or [],
                    "meta": meta or {},
                },
            )
        )


MEMORY = [
    "Run: open \"sweet disposition\" song on youtube",
    "Lesson: Notepad's Save dialog opens in Documents; type the full Desktop path into the file name box.",
    "Lesson: Search pages show a cookie banner first; accept it before reading the results.",
]
OUTLINE = "\n".join(f"{n}. {s[1]}" for n, s in enumerate(STEPS, 1))


def run_events(stage: str) -> list[tuple[str, dict]]:
    """Every event of the demo run up to `stage`: acting | approval | finished."""
    t = Spans()
    t.add("recall", "recall", output="1 similar run(s), 2 lesson(s).", memory=MEMORY)
    t.add("plan", "plan", output=OUTLINE, thinking="Browser for the forecast, then Notepad for the file; saving to disk is a medium-risk step.")

    fara_turns = {
        "s1": [
            ("browser_action {\"action\": \"web_search\", \"query\": \"Istanbul weather tomorrow\"}", "I'll search for the forecast directly."),
            ("click {\"coordinate\": [1184, 642]}", "A cookie banner covers the results; accept it."),
            ("finish", "The forecast card for Istanbul is visible."),
        ],
        "s2": [
            ("scroll {\"direction\": \"down\", \"amount\": 3}", "Scroll to the hourly forecast and wind."),
            ("finish", "17°C / 11°C, 40% rain, NE 18 km/h."),
        ],
        "s3": [
            ("launch_app {\"name\": \"Notepad\"}", "Open Notepad from the Start menu."),
            ("finish", "Notepad is open with an empty document."),
        ],
        "s4": [
            ("click {\"coordinate\": [640, 420]}", "Focus the Notepad text area."),
            ("type_text {\"text\": \"«typed text»\"}", "Type the summary: temperatures, rain chance and wind."),
            ("hotkey {\"keys\": [\"ctrl\", \"home\"]}", "Back to the top to check the text."),
            ("finish", "The summary is in the document."),
        ],
        "s5": [
            ("hotkey {\"keys\": [\"ctrl\", \"s\"]}", "Open the Save dialog."),
            ("type_text {\"text\": \"«typed text»\"}", "Type the full Desktop path as the file name."),
            ("click {\"coordinate\": [1022, 688]}", "Press Save."),
            ("finish", "weather.txt is saved on the Desktop."),
        ],
    }

    def step(index: int, *, final: bool = True, running_turns: int | None = None) -> None:
        step_id = STEPS[index][0]
        n = index + 1
        rnd = f"-{n}"
        t.add(f"select{rnd}", "select_step", output=f"Next: {STEPS[index][1]}", meta={"step_id": step_id})
        t.add(f"gate{rnd}", "gate", output=f"No approval needed ({'medium' if step_id == 's5' else 'low'} risk).", meta={"step_id": step_id})
        turns = fara_turns[step_id]
        shown = turns if running_turns is None else turns[:running_turns]
        exec_id = f"exec{rnd}"
        t.add(exec_id, "execute_step", status="done" if final else "running", label=STEPS[index][1], memory=[MEMORY[1]] if step_id == "s5" else ([MEMORY[2]] if step_id == "s1" else []), meta={"step_id": step_id})
        for i, (output, thinking) in enumerate(shown, 1):
            is_last = i == len(shown)
            status = "running" if (not final and is_last) else "done"
            t.add(f"fara{rnd}-{i}", "fara", parent=exec_id, kind="model", label=f"FARA #{i}", status=status, output=output, thinking=thinking)
        if final:
            t.add(f"verify{rnd}", "verify", parent=exec_id, kind="model", label="Step check (FARA)", output=f"Passed: {STEPS[index][2]}.")
            # Re-emit the step as done with its summary.
            t.events.append(("trace", {**dict(t.events[[e[1]["id"] for e in t.events].index(exec_id)][1]), "status": "done", "output": f"completed: {plan_steps(['completed'] * 5)[index]['summary']}", "ended_at": iso(1)}))

    if stage == "acting":
        for i in range(3):
            step(i)
        step(3, final=False, running_turns=2)
        statuses = ["completed", "completed", "completed", "running", "pending"]
        events = t.events + [("activity", {"activity": activity(statuses, "Type the summary: temperatures, rain chance and wind.", "FARA #2 · type_text"), "budget": budget(9, 31800, 1, 3920)})]
        return events

    if stage == "approval":
        for i in range(4):
            step(i)
        t.add("select-5", "select_step", output=f"Next: {STEPS[4][1]}", meta={"step_id": "s5"})
        t.add("gate-5", "gate", status="waiting", output=f"Asking the operator (medium risk): {STEPS[4][1]}", meta={"step_id": "s5"})
        statuses = ["completed", "completed", "completed", "completed", "pending"]
        approval = {
            "id": "approval-1",
            "kind": "step",
            "title": STEPS[4][1],
            "reasons": ["Writes a new file to disk", "Planned at medium risk; the threshold asks from medium up"],
            "level": "medium",
            "step_id": "s5",
            "expires_at": (datetime.now(UTC) + timedelta(seconds=118)).isoformat(),
        }
        return t.events + [("activity", {"activity": activity(statuses, None, "Waiting for the operator"), "budget": budget(11, 38400, 1, 3920)}), ("approval.requested", approval)]

    # finished
    for i in range(5):
        step(i)
    t.add("finalize", "finalize", output="completed: Saved tomorrow's Istanbul forecast to weather.txt on the Desktop.")
    statuses = ["completed"] * 5
    act = activity(statuses, None, None)
    report = (
        "Saved tomorrow's Istanbul forecast to **weather.txt** on the Desktop.\n\n"
        "✓ High 17°C, low 11°C\n✓ 40% chance of rain in the afternoon\n✓ North-east wind, 18 km/h\n\n"
        "All 5 steps were checked on screen."
    )
    finished = {"status": "completed", "error": None, "output": {"messages": [{"type": "ai", "content": report}]}, "activity": act}
    return t.events + [("activity", {"activity": act, "budget": budget(14, 47210, 2, 5480, 1, 2210)}), ("run.finished", finished)]


def sse(events: list[tuple[str, dict]]) -> str:
    return "".join(f"id: {n}\r\nevent: {kind}\r\ndata: {json.dumps(data)}\r\n\r\n" for n, (kind, data) in enumerate(events, 1))


# --- settings, connections and memory ---------------------------------------------

CONNECTIONS = [
    {"id": "fara", "name": "FARA on Colab", "kind": "computer_use", "interface": "langchain_openai:ChatOpenAI", "params": {"model": "microsoft/Fara-7B", "base_url": "https://fara-demo.trycloudflare.com/v1", "temperature": "0.0", "max_tokens": "1024"}, "secrets_set": []},
    {"id": "gemini", "name": "Gemini Flash", "kind": "vlm", "interface": "langchain_google_genai:ChatGoogleGenerativeAI", "params": {"model": "gemini-2.5-flash", "temperature": "0.0"}, "secrets_set": ["google_api_key"]},
    {"id": "claude", "name": "Claude", "kind": "vlm", "interface": "langchain_anthropic:ChatAnthropic", "params": {"model": "claude-sonnet-4-5", "temperature": "0.0"}, "secrets_set": ["api_key"]},
    {"id": "ollama", "name": "Local Llama", "kind": "llm", "interface": "langchain_ollama:ChatOllama", "params": {"model": "llama3.1:8b", "base_url": "http://localhost:11434"}, "secrets_set": []},
    {"id": "embed", "name": "Nomic embeddings", "kind": "embedding", "interface": "langchain_ollama:OllamaEmbeddings", "params": {"model": "nomic-embed-text", "base_url": "http://localhost:11434"}, "secrets_set": []},
]


def model_roles() -> dict:
    return {
        "roles": {"planner": {"connection": "gemini"}, "supervisor": {"connection": "planner"}, "vision": {"connection": "fara"}, "embeddings": {"connection": "embed"}},
        "role_kinds": {"planner": ["llm", "vlm"], "supervisor": ["vlm"], "vision": ["computer_use"], "embeddings": ["embedding"]},
        "kinds": [{"id": kind, "label": connections.KIND_LABELS[kind]} for kind in connections.KINDS],
        "connections": CONNECTIONS,
        "interfaces": [{"path": i.path, "label": i.label, "kinds": list(i.kinds), "params": [dataclasses.asdict(p) for p in i.params]} for i in connections.INTERFACES],
        "where": {
            "planner": {"name": "gemini", "label": "Gemini Flash", "model": "gemini-2.5-flash", "where": "langchain_google_genai", "sees_images": True},
            "supervisor": {"name": "gemini", "label": "Gemini Flash", "model": "gemini-2.5-flash", "where": "langchain_google_genai", "sees_images": True},
            "vision": {"name": "fara", "label": "FARA on Colab", "model": "microsoft/Fara-7B", "where": "fara-demo.trycloudflare.com", "sees_images": True},
            "embeddings": {"name": "embed", "label": "Nomic embeddings", "model": "nomic-embed-text", "where": "localhost:11434", "sees_images": False},
        },
    }


def episode(n: int, task: str, apps: list[str], domains: list[str], outline: list[str], rating: str | None, uses: int, days: float) -> dict:
    return {
        "id": f"ep{n}",
        "run_id": f"operator-run-{n}",
        "created_at": iso(days * 86400),
        "task_text": task,
        "approach": "plan",
        "apps": apps,
        "domains": domains,
        "plan_outline": [{"instruction": text, "kind": "vision", "environment": "browser" if domains else "desktop"} for text in outline],
        "rating": rating,
        "uses": uses,
        "verified": 1,
        "embedded": True,
    }


EPISODES = [
    episode(1, 'open "sweet disposition" song on youtube', ["msedge"], ["youtube.com"], ["Open YouTube in the browser", 'Search for "sweet disposition"', "Open the first song result"], "good", 6, 0.4),
    episode(2, "open downloads folder", ["explorer"], [], ["Open File Explorer", "Go to the Downloads folder"], "good", 3, 1.2),
    episode(3, "Rename every screenshot on the Desktop to include today's date", ["explorer"], [], ["Open the Desktop in File Explorer", "Select the screenshots", "Rename each with today's date"], None, 1, 2.5),
    episode(4, "Find the release notes of the latest Python version", ["msedge"], ["python.org"], ["Open python.org", "Go to Downloads", "Open the latest release notes"], None, 0, 4.0),
    episode(5, "Mute system sounds from Settings", ["SystemSettings"], [], ["Open Settings", "Go to System → Sound", "Turn off system sounds"], "bad", 0, 6.0),
]


def lesson(n: int, key_type: str, key: str, text: str, source: str, hits: int, uses: int) -> dict:
    return {"id": f"ls{n}", "key_type": key_type, "key": key, "text": text, "source": source, "hits": hits, "disabled": 0, "uses": uses, "updated_at": iso(n * 3600), "embedded": True}


LESSONS = [
    lesson(1, "app", "notepad", "Notepad's Save dialog opens in Documents; type the full Desktop path into the file name box.", "operator", 4, 4),
    lesson(2, "domain", "youtube.com", "Dismiss the sign-in prompt before clicking a video; it swallows the first click.", "auto", 3, 5),
    lesson(3, "general", "*", "Search pages show a cookie banner first; accept it before reading the results.", "auto", 2, 3),
    lesson(4, "app", "explorer", "Use the address bar to jump to a folder instead of clicking through the tree.", "auto", 2, 2),
]

LOGS = {
    "agent": "\n".join(
        [
            "2026-11-16 14:02:11 INFO  run operator-run-demo started · approach=plan · mode=hybrid",
            "2026-11-16 14:02:11 INFO  recall: 1 similar run(s), 2 lesson(s)",
            "2026-11-16 14:02:14 INFO  planner: 5 steps (gemini-2.5-flash, 1.9k tok)",
            "2026-11-16 14:02:15 INFO  step s1 · Open the browser and search for tomorrow's weather in Istanbul",
            "2026-11-16 14:02:18 INFO  fara #1 browser_action web_search",
            "2026-11-16 14:02:22 INFO  fara #2 click (1184, 642)",
            "2026-11-16 14:02:25 INFO  fara #3 finish",
            "2026-11-16 14:02:26 INFO  verify s1: passed",
            "2026-11-16 14:02:27 INFO  step s2 · Read the forecast: high and low temperature, chance of rain and wind",
            "2026-11-16 14:02:31 INFO  fara #1 scroll down 3",
            "2026-11-16 14:02:34 INFO  verify s2: passed",
            "2026-11-16 14:02:35 INFO  step s3 · Open Notepad",
            "2026-11-16 14:02:37 INFO  fara #1 launch_app Notepad",
            "2026-11-16 14:02:40 INFO  verify s3: passed",
            "2026-11-16 14:02:41 INFO  step s4 · Type the weather summary into Notepad",
            "2026-11-16 14:02:43 INFO  fara #2 type_text «typed text»",
            "2026-11-16 14:02:49 INFO  verify s4: passed",
            "2026-11-16 14:02:50 INFO  gate s5: medium risk, asking the operator",
            "2026-11-16 14:02:58 INFO  gate s5: approved (once)",
            "2026-11-16 14:03:07 INFO  verify s5: passed",
            "2026-11-16 14:03:08 INFO  run operator-run-demo completed in 57s · vlm 14 req · llm 3 req",
            "2026-11-16 14:03:08 INFO  memory: episode saved, 1 lesson reinforced",
        ]
    ),
    "gateway": "INFO  gateway listening on 127.0.0.1:52100\nINFO  GET /api/computer-use/status 200\nINFO  POST /api/computer-use/runs 200\nINFO  GET /api/computer-use/runs/operator-run-demo/events 200",
    "ui": "",
}

BROWSER = {
    "use_profile": True,
    "channel": "msedge",
    "user_data_dir": "C:\\Users\\demo\\AppData\\Local\\Microsoft\\Edge\\User Data",
    "profile_directory": "Default",
    "browsers": [
        {"channel": "msedge", "label": "Microsoft Edge", "user_data_dir": "C:\\Users\\demo\\AppData\\Local\\Microsoft\\Edge\\User Data"},
        {"channel": "chrome", "label": "Google Chrome", "user_data_dir": "C:\\Users\\demo\\AppData\\Local\\Google\\Chrome\\User Data"},
    ],
    "profiles": [{"directory": "Default", "name": "Personal"}, {"directory": "Profile 1", "name": "Work"}],
}


# --- the fake gateway ---------------------------------------------------------------


class Gateway:
    """Answers the UI's API calls for one scenario."""

    def __init__(self, stage: str | None) -> None:
        self.stage = stage  # None: no run yet
        self.served_events = False

    def handle(self, route: Route) -> None:
        url = urlparse(route.request.url)
        path = url.path.removeprefix(API)
        method = route.request.method

        if path.endswith("/events"):
            if self.served_events:
                route.fulfill(status=200, headers={"Content-Type": "text/event-stream"}, body=": keep-alive\r\n\r\n")
            else:
                self.served_events = True
                route.fulfill(status=200, headers={"Content-Type": "text/event-stream"}, body=sse(run_events(self.stage or "acting")))
            return
        if path.startswith("/logs/"):
            route.fulfill(status=200, content_type="text/plain", body=LOGS.get(path.rsplit("/", 1)[-1], ""))
            return
        route.fulfill(status=200, content_type="application/json", body=json.dumps(self.answer(method, path)))

    def answer(self, method: str, path: str) -> object:
        if path == "/status":
            return {"enabled": True, "approach": "plan", "execution_mode": "hybrid", "platform": "win32", "budgets": {"vision": 120, "planner": 20}}
        if path == "/models":
            return model_roles()
        if path.startswith("/models/") and path.endswith("/check"):
            return {"role": "vision", "name": "FARA on Colab", "model": "microsoft/Fara-7B", "where": "fara-demo.trycloudflare.com", "ok": True, "latency_ms": 842, "detail": "Answered a test screenshot with a click."}
        if path == "/approach":
            return {"approach": "plan", "options": ["plan", "brief", "direct"]}
        if path == "/execution-mode":
            return {"execution_mode": "hybrid", "options": ["hybrid", "vision_only"]}
        if path == "/vision/recovery":
            return {"enabled": True}
        if path == "/approvals/threshold":
            return {"threshold": "medium", "options": ["off", "critical", "high", "medium"]}
        if path == "/memory/enabled":
            return {"enabled": True}
        if path == "/vision/verifier":
            return {"verifier": "fara", "options": ["fara", "supervisor", "none"], "supervisor_sees_images": True}
        if path == "/memory/episodes":
            return EPISODES
        if path == "/memory/lessons":
            return LESSONS
        if path.startswith("/memory/search"):
            return {"episodes": EPISODES[:2], "lessons": LESSONS[:2]}
        if path.endswith("/memory") and path.startswith("/runs/"):
            return {"episodes": EPISODES[:1], "lessons": LESSONS[:1] + LESSONS[2:3]}
        if path == "/browser":
            return BROWSER
        if path.startswith("/runs") and method == "GET" and "?" not in path and path != "/runs":
            return {"run_id": RUN_ID, "thread_id": "operator-thread", "prompt": PROMPT, "status": "running", "started_at": iso(90), "activity": activity(["pending"] * 5, None, None)}
        if path == "/runs":
            if self.stage is None:
                return []
            return [{"run_id": RUN_ID, "thread_id": "operator-thread", "prompt": PROMPT, "status": "running", "started_at": iso(90)}]
        return {}


def serve_ui() -> http.server.ThreadingHTTPServer:
    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args: object) -> None:
            pass

    handler = functools.partial(Quiet, directory=str(OUT))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


# --- the screenshots ----------------------------------------------------------------


def open_page(browser, base: str, stage: str | None, *, draft: str = "") -> Page:
    context = browser.new_context(viewport=VIEWPORT, device_scale_factor=2, color_scheme="dark")
    # No popup or picture-in-picture window: the mini HUD falls back to its in-view overlay.
    context.add_init_script(
        f"""
        window.open = () => null;
        try {{ delete window.documentPictureInPicture; }} catch (e) {{}}
        localStorage.setItem('vilagent.operator.draft.v2', JSON.stringify({{thread_id: 'operator-thread', task_prompt: {json.dumps(draft)}}}));
        """
    )
    page = context.new_page()
    gateway = Gateway(stage)
    page.route(f"**{API}/**", gateway.handle)
    page.goto(f"{base}/index.html")
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(1500)
    return page


def shot(page: Page, name: str, settle: int = 900) -> None:
    page.wait_for_timeout(settle)
    path = SHOTS / f"{name}.png"
    page.screenshot(path=str(path))
    print("wrote", path.relative_to(ROOT))


def restore_full_view(page: Page) -> None:
    button = page.get_by_title("Back to the full view")
    if button.count():
        button.first.click()


def main() -> None:
    if not (OUT / "index.html").exists():
        sys.exit("Build the UI first: cd desktop && corepack pnpm build")
    SHOTS.mkdir(parents=True, exist_ok=True)
    server = serve_ui()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    with sync_playwright() as p:
        browser = p.chromium.launch()

        # 1. Ready for a task.
        page = open_page(browser, base, None, draft=PROMPT)
        shot(page, "01-home")
        page.context.close()

        # 2. A run in progress, with the mini HUD overlay that shows while FARA acts.
        page = open_page(browser, base, "acting")
        shot(page, "02-run-live", settle=2200)
        mini = page.locator("div.fixed.right-4.top-16")
        if mini.count():
            mini.first.screenshot(path=str(SHOTS / "07-mini-hud.png"))
            print("wrote docs/screenshots/07-mini-hud.png")
        restore_full_view(page)
        shot(page, "03-run-graph", settle=1500)
        page.context.close()

        # 4. A risky step waits for the operator.
        page = open_page(browser, base, "approval")
        restore_full_view(page)
        shot(page, "04-approval", settle=2000)
        page.context.close()

        # 5. The run finished: the report, the memories it used, the rating.
        page = open_page(browser, base, "finished")
        page.wait_for_timeout(2500)
        page.get_by_title("Show the report").click()
        shot(page, "05-run-finished", settle=1500)

        # 6. The options drawer.
        page.get_by_title("Options").click()
        shot(page, "06-options")

        # 8-12. Settings and its windows.
        page.get_by_role("button", name="Settings").click()
        shot(page, "08-settings")
        # Each window opens from the settings popup; hide the popup and the options drawer behind
        # it for the shot so only the window shows over the run.
        layers = "document.querySelectorAll('[role=dialog][aria-label=Settings], aside[aria-hidden=false]')"
        hide = f"{layers}.forEach(el => {{ const box = el.matches('[role=dialog]') ? el.parentElement : el; box.dataset.shotHidden = '1'; box.style.visibility = 'hidden'; }})"
        unhide = "document.querySelectorAll('[data-shot-hidden]').forEach(el => { el.style.visibility = ''; delete el.dataset.shotHidden; })"
        for tile, name in (("Connections", "09-connections"), ("Models", "10-models"), ("Memory", "11-memory"), ("Logs", "12-logs"), ("Browser", "13-browser")):
            page.get_by_role("dialog", name="Settings").get_by_role("button", name=tile).click()
            page.wait_for_timeout(600)
            page.evaluate(hide)
            if tile == "Models":
                page.get_by_role("button", name="Check", exact=True).nth(2).click()
            shot(page, name)
            page.evaluate(unhide)
            page.get_by_title("Close").last.click()
            page.wait_for_timeout(300)
        page.context.close()
        browser.close()
    server.shutdown()


if __name__ == "__main__":
    main()
