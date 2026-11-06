"""
main.py — Free Fire Like API v2.0
Railway-ready Flask application.

Endpoints:
  GET  /              -> API info
  GET  /health        -> Health check (Railway uses this)
  GET  /like          -> Send likes  (requires API key if set)
  GET  /status        -> Token pool status (requires API key)
  POST /reload-tokens -> Reload tokens without restart (requires API key)
"""

import asyncio
import json
import logging
import threading
import time
import warnings
from functools import wraps
from typing import Optional

import aiohttp
import requests as sync_requests
from flask import Flask, jsonify, request
from google.protobuf.json_format import MessageToJson

from config import Config
from protobuf_handler import decode_like_count, encode_for_like, encode_for_uid
from token_manager import TokenManager

# ── SSL warning suppression (verify=False is intentional here) ────────────────
warnings.filterwarnings("ignore", message="Unverified HTTPS request")

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ── App & singletons ──────────────────────────────────────────────────────────
app           = Flask(__name__)
token_manager = TokenManager()

# ── Duplicate-request guard ────────────────────────────────────────────────────
_recent_uids: dict = {}
_uid_lock = threading.Lock()


def _is_duplicate(uid: str) -> bool:
    """Return True if the same UID was processed within DUPLICATE_WINDOW seconds."""
    with _uid_lock:
        now     = time.time()
        expired = [k for k, v in _recent_uids.items()
                   if now - v > Config.DUPLICATE_WINDOW]
        for k in expired:
            del _recent_uids[k]
        if uid in _recent_uids:
            return True
        _recent_uids[uid] = now
        return False


# ── Helpers ───────────────────────────────────────────────────────────────────

def _server_url(server_name: str, endpoint: str) -> str:
    if server_name == "IND":
        base = "https://client.ind.freefiremobile.com"
    elif server_name in {"BR", "US", "SAC", "NA"}:
        base = "https://client.us.freefiremobile.com"
    else:
        base = "https://clientbp.ppmainecoonghj.com"
    return f"{base}/{endpoint}"


def _headers(token: str) -> dict:
    return {
        "User-Agent":      "Dalvik/2.1.0 (Linux; U; Android 9; ASUS_Z01QD Build/PI)",
        "Connection":      "Keep-Alive",
        "Accept-Encoding": "gzip",
        "Authorization":   f"Bearer {token}",
        "Content-Type":    "application/x-www-form-urlencoded",
        "Expect":          "100-continue",
        "X-Unity-Version": "2018.4.11f1",
        "X-GA":            "v1 1",
        "ReleaseVersion":  "OB55",
    }


def _run_async(coro):
    """Run an async coroutine safely from a sync Flask route."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _get_player_info(uid: str, server_name: str, token: str) -> Optional[dict]:
    """Call GetPlayerPersonalShow, return parsed AccountInfo dict or None."""
    try:
        enc = encode_for_uid(uid)
        if enc is None:
            return None
        url  = _server_url(server_name, "GetPlayerPersonalShow")
        resp = sync_requests.post(
            url,
            data=bytes.fromhex(enc),
            headers=_headers(token),
            verify=False,
            timeout=Config.REQUEST_TIMEOUT,
        )
        decoded = decode_like_count(resp.content)
        if decoded is None:
            return None
        return json.loads(MessageToJson(decoded))
    except Exception as e:
        logger.error(f"[PlayerInfo] UID={uid} server={server_name}: {e}")
        return None


# ── Async like sender ─────────────────────────────────────────────────────────

async def _send_one_like(
    session:   aiohttp.ClientSession,
    enc_hex:   str,
    token:     str,
    url:       str,
    semaphore: asyncio.Semaphore,
) -> str:
    """Returns 'ok' | 'expired' | 'fail' | 'error'"""
    async with semaphore:
        try:
            async with session.post(
                url,
                data=bytes.fromhex(enc_hex),
                headers=_headers(token),
                timeout=aiohttp.ClientTimeout(total=Config.REQUEST_TIMEOUT),
            ) as resp:
                if resp.status == 200:
                    return "ok"
                if resp.status in (401, 403):
                    return "expired"
                return "fail"
        except asyncio.TimeoutError:
            return "error"
        except Exception as e:
            logger.debug(f"[LikeRequest] {e}")
            return "error"


async def _send_all_likes(uid: str, server_name: str, tokens: list) -> int:
    """Dispatch like requests concurrently, update token health, return ok count."""
    enc = encode_for_like(uid, server_name)
    if enc is None:
        return 0

    url       = _server_url(server_name, "LikeProfile")
    semaphore = asyncio.Semaphore(Config.CONCURRENCY_LIMIT)

    connector = aiohttp.TCPConnector(limit=50, ssl=False)
    async with aiohttp.ClientSession(connector=connector) as session:
        tasks   = [_send_one_like(session, enc, t["token"], url, semaphore)
                   for t in tokens]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    expired_list = [
        tokens[i]["token"] for i, r in enumerate(results) if r == "expired"
    ]
    if expired_list:
        token_manager.mark_expired(expired_list)

    for i, r in enumerate(results):
        if isinstance(r, Exception) or r in ("fail", "error"):
            token_manager.mark_failed(tokens[i]["token"])

    return sum(1 for r in results if r == "ok")


# ── API-key decorator ─────────────────────────────────────────────────────────

def require_api_key(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if Config.API_KEY:
            provided = (
                request.headers.get("X-API-Key")
                or request.args.get("api_key")
            )
            if provided != Config.API_KEY:
                return jsonify({
                    "error":   "Unauthorized",
                    "message": "Missing or invalid API key",
                    "code":    401,
                }), 401
        return f(*args, **kwargs)
    return wrapper


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/", methods=["GET"])
def index():
    return jsonify({
        "api":     "Free Fire Like API",
        "version": "2.0",
        "status":  "online",
        "endpoints": {
            "like":          "GET /like?uid=UID&server_name=BD[&api_key=KEY]",
            "health":        "GET /health",
            "token_status":  "GET /status  (API key required)",
            "reload_tokens": "POST /reload-tokens[?region=BD]  (API key required)",
        },
    })


@app.route("/health", methods=["GET"])
def health():
    """Railway health-check — must always return 200."""
    return jsonify({"status": "ok", "timestamp": int(time.time())}), 200


@app.route("/like", methods=["GET"])
@require_api_key
def like_route():
    # ── Validate inputs ─────────────────────────────────────────────────────
    uid         = request.args.get("uid", "").strip()
    server_name = request.args.get("server_name", "").upper().strip()

    if not uid or not server_name:
        return jsonify({"error": "uid and server_name are required"}), 400

    if not uid.isdigit():
        return jsonify({"error": "uid must be a numeric string"}), 400

    valid_servers = {"BD", "IND", "BR", "US", "SAC", "NA"}
    if server_name not in valid_servers:
        return jsonify({
            "error": "Invalid server_name",
            "valid": sorted(valid_servers),
        }), 400

    # ── Duplicate guard ─────────────────────────────────────────────────────
    if _is_duplicate(uid):
        return jsonify({
            "error":   "Duplicate request",
            "message": (
                f"UID {uid} was recently processed. "
                f"Please wait {Config.DUPLICATE_WINDOW} seconds."
            ),
            "code": 429,
        }), 429

    # ── Token pool (max 220) ────────────────────────────────────────────────
    tokens = token_manager.get_tokens(server_name, limit=Config.MAX_LIKES_PER_REQUEST)
    if not tokens:
        return jsonify({
            "error":  "No valid tokens available for this region",
            "region": server_name,
            "code":   503,
        }), 503

    first_token       = tokens[0]["token"]
    tokens_dispatched = len(tokens)

    # ── Snapshot BEFORE ──────────────────────────────────────────────────────
    before_data = _get_player_info(uid, server_name, first_token)
    if before_data is None:
        return jsonify({
            "error": "Failed to retrieve player info. Check UID and server_name.",
        }), 400

    account      = before_data.get("AccountInfo", {})
    before_likes = int(account.get("Likes",          0))
    nickname     = str(account.get("PlayerNickname", "Unknown"))
    player_uid   = int(account.get("UID",             uid))

    # ── Send likes ───────────────────────────────────────────────────────────
    _run_async(_send_all_likes(uid, server_name, tokens))

    # ── Snapshot AFTER ───────────────────────────────────────────────────────
    after_data = _get_player_info(uid, server_name, first_token)
    if after_data is None:
        return jsonify({
            "error":              "Likes dispatched but could not verify after-count",
            "LikesbeforeCommand": before_likes,
            "TokensDispatched":   tokens_dispatched,
        }), 500

    after_likes = int(after_data.get("AccountInfo", {}).get("Likes", 0))
    likes_given = after_likes - before_likes

    return jsonify({
        "UID":                player_uid,
        "PlayerNickname":     nickname,
        "LikesbeforeCommand": before_likes,
        "LikesafterCommand":  after_likes,
        "LikesGivenByAPI":    likes_given,
        "TokensDispatched":   tokens_dispatched,
        "MaxLikesAllowed":    Config.MAX_LIKES_PER_REQUEST,
        "Region":             server_name,
        "status":             1 if likes_given > 0 else 2,
    })


@app.route("/status", methods=["GET"])
@require_api_key
def status_route():
    return jsonify({
        "token_status": token_manager.get_status(),
        "config": {
            "max_likes_per_request": Config.MAX_LIKES_PER_REQUEST,
            "concurrency_limit":     Config.CONCURRENCY_LIMIT,
            "duplicate_window_secs": Config.DUPLICATE_WINDOW,
            "request_timeout_secs":  Config.REQUEST_TIMEOUT,
        },
        "timestamp": int(time.time()),
    })


@app.route("/reload-tokens", methods=["POST"])
@require_api_key
def reload_tokens():
    region = request.args.get("region")
    token_manager.reload(region.upper() if region else None)
    return jsonify({
        "message":      "Tokens reloaded successfully",
        "token_status": token_manager.get_status(),
    })


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=Config.PORT, debug=False, use_reloader=False)
