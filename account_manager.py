"""Account manager — orchestrate registration, rotation, subscription monitoring."""
import logging
from datetime import datetime

from storage import Storage
from wpn_client import WPNClient, WPNError, build_subscription_url, ACTIVE
from atomic_client import AtomicMailClient, AtomicMailError

log = logging.getLogger(__name__)

# how the verification email sender/subject looks — used to locate the right mail
WPN_SENDER_HINT = "wpn"
WPN_SUBJECT_HINT = ""


class AccountManager:
    def __init__(
        self,
        storage: Storage,
        wpn_client: WPNClient,
        mail_pool: list[dict],
        rotation_threshold_days: int = 1,
    ):
        """mail_pool: list of {email, password}."""
        self.storage = storage
        self.wpn = wpn_client
        self.mail_pool = mail_pool
        self.threshold = rotation_threshold_days

    # ── pick next unused email ─────────────────────────────────

    def _next_mail(self) -> dict | None:
        used = self.storage.get_used_emails()
        for entry in self.mail_pool:
            if entry["email"] not in used:
                return entry
        return None

    # ── register one account ───────────────────────────────────

    def register_account(self, mail: dict) -> dict | None:
        """Full registration flow for one email. Returns account dict or None."""
        email = mail["email"]
        self.storage.log(f"Начинаю регистрацию аккаунта для {email}", account_email=email)

        atomic = AtomicMailClient(
            email=email,
            password=mail["password"],
            sender_hint=WPN_SENDER_HINT,
        )

        # 1. snapshot current WPN emails so we can ignore them later (stale codes)
        try:
            exclude = atomic.baseline()
        except AtomicMailError as e:
            self.storage.log(f"Не удалось прочитать inbox (baseline): {e}", "error", email)
            return None

        # 2. request code from WPN (triggers a fresh email)
        try:
            challenge_id = self.wpn.request_code(email)
        except WPNError as e:
            self.storage.log(f"Не удалось запросить код: {e}", "error", email)
            return None

        # 3. read the FRESH code (excludes stale emails from baseline)
        try:
            code = atomic.read_code(exclude_hrefs=exclude, timeout=90, poll=5)
        except AtomicMailError as e:
            self.storage.log(f"Код подтверждения не получен: {e}", "error", email)
            return None

        # 4. exchange code for tokens
        try:
            tokens = self.wpn.exchange_code(challenge_id, code)
        except WPNError as e:
            self.storage.log(f"Не удалось обменять код на токены: {e}", "error", email)
            return None

        access_token = tokens.get("access_token")
        refresh_token = tokens.get("refresh_token")
        session_id = tokens.get("session_id")

        # 4. fetch subscription status
        sub_info = self._fetch_subscription(access_token, email)
        days_left = sub_info.get("days_left", 0)
        valid_until = sub_info.get("valid_until", "")

        account = {
            "email": email,
            "wpn_access_token": access_token,
            "wpn_refresh_token": refresh_token,
            "wpn_session_id": session_id,
            "subscription_id": sub_info.get("subscription_id", ""),
            "subscription_days_left": days_left,
            "subscription_valid_until": valid_until,
            "subscription_url": sub_info.get("subscription_url", ""),
            "status": "active",
        }
        self.storage.save_account(account)
        self.storage.log(
            f"Аккаунт зарегистрирован. Подписка: {days_left} дн., до {valid_until or '—'}",
            "info",
            email,
        )
        if sub_info.get("subscription_url"):
            self.storage.log(
                f"URL подписки: {sub_info['subscription_url']}", "info", email
            )
        return account

    # ── fetch + parse subscription ─────────────────────────────

    def _fetch_subscription(self, access_token: str, email: str) -> dict:
        """GET /v1/subscription. Normalise to {days_left, valid_until, subscription_id, subscription_url, active}."""
        try:
            data = self.wpn.get_subscription(access_token)
        except WPNError as e:
            self.storage.log(f"Не удалось получить подписку: {e}", "error", email)
            return {"days_left": 0, "valid_until": "", "subscription_id": "", "subscription_url": "", "active": False}

        # API field names may vary — try common shapes
        days_left = (
            data.get("daysLeft")
            or data.get("days_left")
            or data.get("daysRemaining")
            or 0
        )
        valid_until = (
            data.get("validUntil")
            or data.get("valid_until")
            or data.get("expiresAt")
            or data.get("expireAt")
            or ""
        )
        # compute days_left from date if missing
        if not days_left and valid_until:
            try:
                expire = datetime.fromisoformat(valid_until.replace("Z", "+00:00"))
                now = datetime.now(expire.tzinfo)
                days_left = max(0, (expire - now).days)
            except (ValueError, TypeError):
                pass

        status = data.get("status", "")
        active = status == ACTIVE
        subscription_id = data.get("subscription_id") or data.get("subscriptionId") or ""
        subscription_url = build_subscription_url(subscription_id, active)

        return {
            "days_left": int(days_left),
            "valid_until": str(valid_until),
            "subscription_id": subscription_id,
            "subscription_url": subscription_url,
            "active": active,
        }

    # ── refresh access token ───────────────────────────────────

    def _ensure_valid_token(self, account: dict) -> str | None:
        """Try to use current token; on failure, refresh. Return working token or None."""
        token = account.get("wpn_access_token")
        refresh = account.get("wpn_refresh_token")
        if not token:
            return None
        # quick probe
        try:
            self.wpn.get_subscription(token)
            return token
        except WPNError:
            log.info("Token invalid, refreshing…")
        if not refresh:
            return None
        try:
            tokens = self.wpn.refresh_token(refresh)
        except WPNError as e:
            self.storage.log(
                f"Refresh токена не удался: {e}", "error", account["email"]
            )
            return None
        new_access = tokens.get("access_token", token)
        new_refresh = tokens.get("refresh_token", refresh)
        self.storage.update_account_tokens(
            account["email"], new_access, new_refresh
        )
        return new_access

    # ── check active account ───────────────────────────────────

    def check_active(self) -> dict | None:
        """Refresh subscription status of the active account. Returns it, or None."""
        account = self.storage.get_active_account()
        if not account:
            return None

        token = self._ensure_valid_token(account)
        if not token:
            self.storage.log(
                "Нет валидного токена у активного аккаунта, ротация",
                "warning",
                account["email"],
            )
            self.storage.update_account_status(account["email"], "expired")
            return None

        sub = self._fetch_subscription(token, account["email"])
        self.storage.update_account_subscription(
            account["email"], sub["days_left"], sub["valid_until"],
            subscription_id=sub.get("subscription_id") or None,
            subscription_url=sub.get("subscription_url") or None,
        )
        account["subscription_days_left"] = sub["days_left"]
        account["subscription_valid_until"] = sub["valid_until"]
        account["subscription_id"] = sub.get("subscription_id", "")
        account["subscription_url"] = sub.get("subscription_url", "")

        self.storage.log(
            f"Подписка проверена: {sub['days_left']} дн. осталось", account_email=account["email"]
        )
        return account

    # ── rotation ───────────────────────────────────────────────

    def needs_rotation(self, account: dict | None) -> bool:
        if not account:
            return True
        return account.get("subscription_days_left", 0) <= self.threshold

    def rotate(self) -> dict | None:
        """Demote current active account, register new one."""
        active = self.storage.get_active_account()
        if active:
            self.storage.update_account_status(active["email"], "expiring")
            self.storage.log(
                f"Старый аккаунт {active['email']} помечен expiring", "warning"
            )

        mail = self._next_mail()
        if not mail:
            self.storage.log("Нет свободных почт для ротации!", "error")
            return None
        return self.register_account(mail)

    # ── public actions (called by web API) ─────────────────────

    def register_new(self) -> dict | None:
        """Force-register a new account using next free email."""
        mail = self._next_mail()
        if not mail:
            self.storage.log("Нет свободных почт для регистрации!", "error")
            return None
        return self.register_account(mail)
