"""
TokenManager — Thread-safe singleton for JWT token lifecycle.

Responsibilities:
  - Load tokens from Config at startup
  - Serve valid tokens (up to a limit) for a given region
  - Track expired & repeatedly-failing tokens
  - Reload tokens on demand without restarting the server
  - Never expose raw token values in status/log output
"""

import threading
import time
import logging
from config import Config

logger = logging.getLogger(__name__)

# Maps every accepted server_name to its internal token group key
SERVER_TO_REGION = {
    "BD":  "BD",
    "IND": "IND",
    "BR":  "BR",
    "US":  "BR",
    "SAC": "BR",
    "NA":  "BR",
}

MAX_FAILURES_BEFORE_DISABLE = 3


class TokenManager:
    """
    Singleton — one instance per process.
    Tokens are loaded once at startup and can be reloaded via reload().
    """

    _instance = None
    _creation_lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._creation_lock:
                if cls._instance is None:
                    obj = super().__new__(cls)
                    obj._initialized = False
                    cls._instance = obj
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True

        self._rw_lock   = threading.RLock()          # protects all mutable state
        self._tokens    = {}                          # region_key -> [{token: str}, ...]
        self._expired   = set()                       # expired/invalid token strings
        self._fail_count = {}                         # token string -> int
        self._load_start_time = None

        self._load_all()

    # ── Internal ────────────────────────────────────────────────────────────

    def _load_all(self):
        """Load tokens for every region group at startup (or on full reload)."""
        self._load_start_time = time.time()
        for region in ("BD", "IND", "BR"):
            tokens = Config.load_tokens(region)
            self._tokens[region] = tokens
            logger.info(f"[TokenManager] {region}: {len(tokens)} tokens loaded")

    def _mask(self, token: str) -> str:
        """Return a masked token string safe for logs."""
        return f"...{token[-8:]}" if len(token) > 8 else "***"

    def _is_valid(self, token_str: str) -> bool:
        return (
            token_str not in self._expired
            and self._fail_count.get(token_str, 0) < MAX_FAILURES_BEFORE_DISABLE
        )

    # ── Public API ───────────────────────────────────────────────────────────

    def get_tokens(self, server_name: str, limit: int = 220) -> list:
        """
        Return up to `limit` valid tokens for the given server region.
        Returns list of {"token": "..."} dicts — never the full pool.
        """
        region_key = SERVER_TO_REGION.get(server_name.upper(), "BD")

        with self._rw_lock:
            pool  = self._tokens.get(region_key, [])
            valid = [t for t in pool if self._is_valid(t["token"])]

        result = valid[:limit]
        logger.debug(
            f"[TokenManager] get_tokens({server_name}): "
            f"{len(result)} returned / {len(valid)} valid / {len(pool)} total"
        )
        return result

    def mark_expired(self, token_strings: list):
        """
        Permanently disable a list of token strings (401/403 responses).
        """
        with self._rw_lock:
            for t in token_strings:
                if t not in self._expired:
                    self._expired.add(t)
                    logger.warning(f"[TokenManager] Token expired: {self._mask(t)}")

    def mark_failed(self, token_string: str):
        """
        Increment failure counter for a token.
        Token is auto-disabled after MAX_FAILURES_BEFORE_DISABLE failures.
        """
        with self._rw_lock:
            count = self._fail_count.get(token_string, 0) + 1
            self._fail_count[token_string] = count
            if count >= MAX_FAILURES_BEFORE_DISABLE:
                logger.warning(
                    f"[TokenManager] Token disabled ({count} failures): {self._mask(token_string)}"
                )

    def reload(self, region: str = None):
        """
        Reload tokens from source files / env vars.
        Pass region='BD'|'IND'|'BR' to reload one group, or None for all.
        Clears expired/failed state for the reloaded group(s).
        """
        with self._rw_lock:
            targets = [region.upper()] if region else ["BD", "IND", "BR"]
            for r in targets:
                new_tokens = Config.load_tokens(r)
                old_strings = {t["token"] for t in self._tokens.get(r, [])}
                new_strings = {t["token"] for t in new_tokens}

                # Clear tracking for the refreshed group only
                self._expired    -= old_strings
                for t in old_strings:
                    self._fail_count.pop(t, None)

                self._tokens[r] = new_tokens
                added   = len(new_strings - old_strings)
                removed = len(old_strings - new_strings)
                logger.info(
                    f"[TokenManager] Reloaded {r}: {len(new_tokens)} tokens "
                    f"(+{added} new, -{removed} removed)"
                )

    def get_status(self) -> dict:
        """
        Return a safe status summary — no raw token values exposed.
        """
        with self._rw_lock:
            summary = {}
            for region, pool in self._tokens.items():
                total   = len(pool)
                expired = sum(1 for t in pool if t["token"] in self._expired)
                failed  = sum(
                    1 for t in pool
                    if self._fail_count.get(t["token"], 0) >= MAX_FAILURES_BEFORE_DISABLE
                )
                valid = max(0, total - expired - failed)
                summary[region] = {
                    "total":   total,
                    "valid":   valid,
                    "expired": expired,
                    "failed":  failed,
                }

        return {
            "regions": summary,
            "uptime_seconds": int(time.time() - self._load_start_time)
            if self._load_start_time else 0,
        }
