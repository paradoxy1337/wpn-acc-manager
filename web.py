"""FastAPI web server: REST API + WebSocket live logs + static dashboard."""
import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from storage import Storage
from wpn_client import WPNClient
from account_manager import AccountManager

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

# ── global app state (populated by main.py before uvicorn) ─────
state: dict = {}


def init_app(
    storage: Storage,
    manager: AccountManager,
    daemon_loop=None,
) -> FastAPI:
    """Build FastAPI app with all routes wired to the manager."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        # shutdown handled by main.py

    app = FastAPI(title="WPN Manager", lifespan=lifespan)
    app.state.manager = manager
    app.state.storage = storage
    app.state.daemon_loop = daemon_loop

    # WebSocket connections for live logs
    clients: set[WebSocket] = set()

    def broadcast_log(level: str, message: str, email: str | None):
        """Push new log line to all connected WS clients (fire-and-forget)."""
        payload = {
            "type": "log",
            "level": level,
            "message": message,
            "email": email,
        }
        for ws in list(clients):
            try:
                asyncio.create_task(ws.send_json(payload))
            except Exception:
                clients.discard(ws)

    storage.on_log = broadcast_log

    # ── REST: accounts ─────────────────────────────────────────

    @app.get("/api/accounts")
    async def list_accounts():
        accounts = storage.get_all_accounts()
        return _sanitize_accounts(accounts)

    @app.get("/api/accounts/active")
    async def get_active():
        acct = storage.get_active_account()
        if not acct:
            return {"account": None}
        return {"account": _sanitize_account(acct)}

    @app.get("/api/accounts/active/subscription-url")
    async def get_active_url():
        """Convenience: return just the import URL of the active account."""
        acct = storage.get_active_account()
        url = acct.get("subscription_url") if acct else ""
        if not url:
            return JSONResponse({"url": "", "error": "Нет активной подписки"}, status_code=404)
        return {"url": url}

    @app.get("/api/logs")
    async def recent_logs(limit: int = 100):
        return storage.get_recent_logs(limit)

    # ── REST: daemon status ────────────────────────────────────

    @app.get("/api/daemon")
    async def daemon_status():
        loop = app.state.daemon_loop
        running = loop.running if loop else False
        return {"running": running}

    @app.post("/api/daemon/stop")
    async def daemon_stop():
        loop = app.state.daemon_loop
        if loop:
            loop.request_stop()
            storage.log("Демон остановлен", "warning")
        return {"running": False}

    @app.post("/api/daemon/start")
    async def daemon_start():
        loop = app.state.daemon_loop
        if loop:
            loop.request_start()
            storage.log("Демон запущен", "info")
        return {"running": loop.running if loop else False}

    # ── REST: actions (run in threadpool — they block) ─────────

    @app.post("/api/accounts/register")
    async def register_new():
        """Force-register a new account using next free email."""
        result = await asyncio.to_thread(manager.register_new)
        if result:
            return {"ok": True, "account": _sanitize_account(result)}
        return JSONResponse(
            {"ok": False, "error": "Регистрация не удалась — нет свободных почт или ошибка"},
            status_code=400,
        )

    @app.post("/api/accounts/rotate")
    async def rotate():
        """Force rotation: demote current, register new."""
        result = await asyncio.to_thread(manager.rotate)
        if result:
            return {"ok": True, "account": _sanitize_account(result)}
        return JSONResponse(
            {"ok": False, "error": "Ротация не удалась — нет свободных почт"},
            status_code=400,
        )

    @app.post("/api/accounts/check")
    async def check_now():
        """Trigger immediate subscription check."""
        acct = await asyncio.to_thread(manager.check_active)
        if acct:
            return {"ok": True, "account": _sanitize_account(acct)}
        return {"ok": False, "error": "Нет активного аккаунта"}

    # ── WebSocket: live logs ───────────────────────────────────

    @app.websocket("/ws/logs")
    async def ws_logs(ws: WebSocket):
        await ws.accept()
        clients.add(ws)
        # send recent history on connect
        for entry in reversed(storage.get_recent_logs(30)):
            await ws.send_json(
                {
                    "type": "log",
                    "level": entry["level"],
                    "message": entry["message"],
                    "email": entry["account_email"],
                    "timestamp": entry["timestamp"],
                }
            )
        try:
            while True:
                # keep connection open; client may send pings
                await ws.receive_text()
        except WebSocketDisconnect:
            clients.discard(ws)
        except Exception:
            clients.discard(ws)

    # ── static + SPA fallback ──────────────────────────────────

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/")
    async def index():
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/{full_path:path}")
    async def spa_fallback(full_path: str):
        candidate = STATIC_DIR / full_path
        if candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(STATIC_DIR / "index.html")

    return app


# ── helpers ────────────────────────────────────────────────────


def _sanitize_account(acct: dict) -> dict:
    """Strip secrets before sending to browser. Keeps subscription_url intact."""
    safe = {**acct}
    for key in ("wpn_access_token", "wpn_refresh_token", "wpn_session_id"):
        if safe.get(key):
            safe[key] = safe[key][:8] + "…"
    return safe


def _sanitize_accounts(accounts: list[dict]) -> list[dict]:
    return [_sanitize_account(a) for a in accounts]
