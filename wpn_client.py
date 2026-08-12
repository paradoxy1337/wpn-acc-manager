"""WPN API client — register via email+code, check subscription, refresh tokens."""
import logging

import requests

log = logging.getLogger(__name__)


class WPNError(Exception):
    pass


# Base for VPN-client subscription/import URL.
# Matches wpnaccount.com frontend: `${CONNECT_BASE}/${subscription_id}`
CONNECT_BASE = "https://wconnect.click/c"
ACTIVE = "USER_SUBSCRIPTION_STATUS_ACTIVE"


def build_subscription_url(subscription_id: str | None, active: bool) -> str:
    """Build the VPN-client import URL from a subscription_id.

    Mirrors the frontend `s(subscriptionId)` helper. Empty when not active.
    """
    if not subscription_id or not active:
        return ""
    return f"{CONNECT_BASE}/{subscription_id}"


class WPNClient:
    """Talk to wpnaccount.com REST API (proxied via /api/proxy)."""

    def __init__(self, base_url: str = "https://wpnaccount.com"):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Origin": self.base_url,
                "Referer": f"{self.base_url}/",
            }
        )

    # ── auth: request code ─────────────────────────────────────

    def request_code(self, email: str) -> str:
        """POST /api/proxy/v1/auth/email/code/request → challenge_id."""
        resp = self.session.post(
            f"{self.base_url}/api/proxy/v1/auth/email/code/request",
            json={"email": email},
            timeout=30,
        )
        data = self._parse(resp)
        challenge_id = data.get("challenge_id")
        if not challenge_id:
            raise WPNError(f"No challenge_id in response: {data}")
        log.info("Code requested for %s, challenge_id=%s", email, challenge_id[:8])
        return challenge_id

    # ── auth: exchange code ────────────────────────────────────

    def exchange_code(self, challenge_id: str, code: str) -> dict:
        """POST /api/proxy/v1/auth/email/code/exchange → token_pair."""
        resp = self.session.post(
            f"{self.base_url}/api/proxy/v1/auth/email/code/exchange",
            json={"challenge_id": challenge_id, "code": code},
            timeout=30,
        )
        data = self._parse(resp)
        tokens = data.get("token_pair") or data
        if not tokens.get("access_token"):
            raise WPNError(f"No access_token in exchange response: {data}")
        log.info("Token pair received")
        return tokens

    # ── refresh ────────────────────────────────────────────────

    def refresh_token(self, refresh_token: str) -> dict:
        """POST /api/proxy/v1/auth/refresh → new token_pair."""
        resp = self.session.post(
            f"{self.base_url}/api/proxy/v1/auth/refresh",
            json={"refresh_token": refresh_token},
            timeout=30,
        )
        data = self._parse(resp)
        tokens = data.get("token_pair") or data
        if not tokens.get("access_token"):
            raise WPNError(f"Refresh failed: {data}")
        log.info("Tokens refreshed")
        return tokens

    # ── subscription ───────────────────────────────────────────

    def get_subscription(self, access_token: str) -> dict:
        """GET /api/proxy/v1/subscription → subscription status."""
        resp = self.session.get(
            f"{self.base_url}/api/proxy/v1/subscription",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=30,
        )
        return self._parse(resp)

    # ── profile ────────────────────────────────────────────────

    def get_profile(self, access_token: str) -> dict:
        """GET /api/proxy/v1/auth/profile → user profile."""
        resp = self.session.get(
            f"{self.base_url}/api/proxy/v1/auth/profile",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=30,
        )
        return self._parse(resp)

    # ── helpers ────────────────────────────────────────────────

    @staticmethod
    def _parse(resp: requests.Response) -> dict:
        if resp.status_code >= 400:
            raise WPNError(
                f"HTTP {resp.status_code}: {resp.text[:300]}"
            )
        try:
            return resp.json()
        except ValueError:
            raise WPNError(f"Non-JSON response: {resp.text[:300]}")
