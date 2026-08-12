"""Entry point: daemon loop + uvicorn web server in one process."""
import argparse
import logging
import signal
import sys
import threading
import time

import yaml

from storage import Storage
from wpn_client import WPNClient
from account_manager import AccountManager
from web import init_app


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def setup_logging(cfg: dict):
    level = getattr(logging, cfg.get("logging", {}).get("level", "INFO").upper())
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    log_file = cfg.get("logging", {}).get("file")
    if log_file:
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(level=level, format=fmt, handlers=handlers)


class DaemonLoop:
    """Background thread: monitor subscription, rotate when needed."""

    def __init__(self, manager: AccountManager, check_interval: int, threshold: int):
        self.manager = manager
        self.check_interval = check_interval  # seconds
        self.threshold = threshold
        self._running = True
        self._stop_flag = False
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._running and not self._stop_flag

    def request_stop(self):
        self._stop_flag = True

    def request_start(self):
        self._stop_flag = False

    def start(self):
        self._thread = threading.Thread(target=self._loop, daemon=True, name="daemon")
        self._thread.start()

    def _loop(self):
        log = logging.getLogger("daemon")
        # initial: ensure there's an active account
        self._cycle()
        while not self._stop_flag:
            # sleep in small slices so stop is responsive
            slept = 0
            while slept < self.check_interval and not self._stop_flag:
                time.sleep(5)
                slept += 5
            if self._stop_flag:
                break
            try:
                self._cycle()
            except Exception as e:
                log.exception("Daemon cycle error: %s", e)
        self._running = False
        log.info("Daemon loop exited")

    def _cycle(self):
        log = logging.getLogger("daemon")
        active = self.manager.check_active()
        if self.manager.needs_rotation(active):
            log.info("Rotation triggered")
            self.manager.rotate()
        else:
            log.debug(
                "No rotation needed, %s days left",
                active.get("subscription_days_left") if active else 0,
            )


def main():
    parser = argparse.ArgumentParser(description="WPN Account Manager")
    parser.add_argument(
        "-c", "--config", default="config.yaml", help="Path to config.yaml"
    )
    parser.add_argument(
        "--host", default="127.0.0.1", help="Web server host"
    )
    parser.add_argument(
        "-p", "--port", type=int, default=8000, help="Web server port"
    )
    args = parser.parse_args()

    cfg = load_config(args.config)
    setup_logging(cfg)
    log = logging.getLogger("main")

    # ── wire components ────────────────────────────────────────
    storage = Storage(cfg["storage"]["db_path"])

    wpn = WPNClient(base_url=cfg["wpn"]["base_url"])
    manager = AccountManager(
        storage=storage,
        wpn_client=wpn,
        mail_pool=cfg["atomic_mails"],
        rotation_threshold_days=cfg["wpn"].get("rotation_threshold_days", 1),
    )

    check_interval = cfg["wpn"].get("check_interval_minutes", 30) * 60
    threshold = cfg["wpn"].get("rotation_threshold_days", 1)
    daemon = DaemonLoop(manager, check_interval, threshold)
    daemon.start()
    log.info("Daemon started (check every %ds, threshold %dd)", check_interval, threshold)

    # ── graceful shutdown ──────────────────────────────────────
    def shutdown(*_):
        log.info("Shutting down…")
        daemon.request_stop()
        if daemon._thread:
            daemon._thread.join(timeout=10)
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # ── run web server (blocks) ────────────────────────────────
    import uvicorn

    app = init_app(storage, manager, daemon)
    log.info("Web server: http://%s:%d", args.host, args.port)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
