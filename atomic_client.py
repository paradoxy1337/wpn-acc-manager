"""Atomic Mail client — drives the Node/Playwright headless reader.

Atomic Mail uses browser-based E2E crypto login; there is no email+password
HTTP API. So we delegate to atomic_reader.mjs (headless Chromium, persistent
session cache per mailbox) to log in and read the inbox.

Two-phase flow (orchestrated by account_manager):
  1. baseline()        — snapshot current matching message hrefs
  2. (caller triggers the verification email)
  3. read_code(exclude) — poll until a NEW matching email arrives, return its code

This guarantees we read the FRESH code, not a stale one from a previous run.
"""
import json
import logging
import os
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

READER = str(Path(__file__).parent / "atomic_reader.mjs")
PROFILE_DIR = str(Path(__file__).parent / ".atomic_profiles")


class AtomicMailError(Exception):
    pass


class AtomicMailClient:
    """Read verification codes from Atomic Mail via the headless reader."""

    def __init__(
        self,
        email: str,
        password: str,
        sender_hint: str = "wpn",
        subject_hint: str = "",
        reader_bin: str = "node",
    ):
        self.email = email
        self.password = password
        self.sender_hint = sender_hint
        self.subject_hint = subject_hint
        self.reader_bin = reader_bin

    def _env(self, **extra) -> dict:
        env = {
            **os.environ,
            "ATOMIC_EMAIL": self.email,
            "ATOMIC_PASSWORD": self.password,
            "SENDER_HINT": self.sender_hint,
            "SUBJECT_HINT": self.subject_hint,
            "PROFILE_DIR": PROFILE_DIR,
        }
        env.update(extra)
        return env

    def _run(self, env: dict, timeout: int) -> str:
        """Run reader, return stdout. Raises AtomicMailError on failure."""
        try:
            proc = subprocess.run(
                [self.reader_bin, READER],
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as e:
            raise AtomicMailError(f"reader timed out after {timeout}s") from e
        if proc.returncode != 0:
            tail = (proc.stderr or "").strip().splitlines()[-1:]
            raise AtomicMailError(
                f"reader failed (exit {proc.returncode}): {(tail[0] if tail else proc.stderr.strip()[:200])}"
            )
        return proc.stdout.strip()

    # ── phase 1: baseline ──────────────────────────────────────

    def baseline(self) -> list[str]:
        """Snapshot current matching message hrefs (to exclude later)."""
        env = self._env(MODE="list")
        out = self._run(env, timeout=60)
        try:
            return json.loads(out) if out else []
        except json.JSONDecodeError as e:
            raise AtomicMailError(f"bad baseline JSON: {out[:200]}") from e

    # ── phase 3: read fresh code ───────────────────────────────

    def read_code(
        self, exclude_hrefs: list[str] = None, timeout: int = 90, poll: int = 5
    ) -> str:
        """Poll inbox until a NEW matching email arrives. Returns 6-digit code.

        Args:
            exclude_hrefs: hrefs from baseline() to ignore (stale emails).
            timeout: max seconds to wait.
            poll: reader poll interval (informational).
        """
        env = self._env(
            MODE="read",
            TIMEOUT_SEC=str(timeout),
            POLL_SEC=str(poll),
            EXCLUDE_HREFS=",".join(exclude_hrefs or []),
        )
        code = self._run(env, timeout=timeout + 60)
        if not code:
            raise AtomicMailError("reader returned empty code")
        log.info("Code %s read from Atomic Mail for %s", code, self.email)
        return code
