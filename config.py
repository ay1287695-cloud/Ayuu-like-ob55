import os
import json
import logging

logger = logging.getLogger(__name__)


class Config:
    # ── Security ───────────────────────────────────────────────────────────
    API_KEY: str = os.environ.get("API_KEY", "")

    # ── Like System ────────────────────────────────────────────────────────
    MAX_LIKES_PER_REQUEST: int = int(os.environ.get("MAX_LIKES", "220"))
    CONCURRENCY_LIMIT: int     = int(os.environ.get("CONCURRENCY", "10"))
    REQUEST_TIMEOUT: int       = int(os.environ.get("REQUEST_TIMEOUT", "15"))
    DUPLICATE_WINDOW: int      = int(os.environ.get("DUPLICATE_WINDOW", "60"))

    # ── Server ─────────────────────────────────────────────────────────────
    PORT: int = int(os.environ.get("PORT", "5000"))

    # ── AES Keys (keep these in env on Railway) ────────────────────────────
    AES_KEY: bytes = os.environ.get("AES_KEY", "Yg&tc%DEuh6%Zc^8").encode()
    AES_IV:  bytes = os.environ.get("AES_IV",  "6oyZDr22E3ychjM%").encode()

    # ── Token File Paths ───────────────────────────────────────────────────
    TOKEN_PATH_BD:  str = os.environ.get("TOKEN_PATH_BD",  "token_bd.json")
    TOKEN_PATH_IND: str = os.environ.get("TOKEN_PATH_IND", "token_ind.json")
    TOKEN_PATH_BR:  str = os.environ.get("TOKEN_PATH_BR",  "token_br.json")

    # Region → (env-var name, file path)
    _REGION_MAP = {
        "BD":  ("TOKENS_BD",  TOKEN_PATH_BD),
        "IND": ("TOKENS_IND", TOKEN_PATH_IND),
        "BR":  ("TOKENS_BR",  TOKEN_PATH_BR),
        "US":  ("TOKENS_BR",  TOKEN_PATH_BR),
        "SAC": ("TOKENS_BR",  TOKEN_PATH_BR),
        "NA":  ("TOKENS_BR",  TOKEN_PATH_BR),
    }

    @classmethod
    def load_tokens(cls, region: str) -> list:
        """
        Load token list for a region.
        Priority: env-var JSON string → JSON file.
        Returns list of {"token": "..."} dicts.
        """
        region = region.upper()
        env_key, file_path = cls._REGION_MAP.get(region, ("TOKENS_BD", cls.TOKEN_PATH_BD))

        # 1. Try environment variable (JSON array string)
        raw = os.environ.get(env_key)
        if raw:
            try:
                tokens = json.loads(raw)
                logger.info(f"[Config] Loaded {len(tokens)} tokens for {region} from env var")
                return tokens
            except json.JSONDecodeError as e:
                logger.warning(f"[Config] Failed to parse env var {env_key}: {e}")

        # 2. Fall back to JSON file
        try:
            with open(file_path, "r") as f:
                tokens = json.load(f)
            logger.info(f"[Config] Loaded {len(tokens)} tokens for {region} from {file_path}")
            return tokens
        except FileNotFoundError:
            logger.warning(f"[Config] Token file not found: {file_path}")
            return []
        except Exception as e:
            logger.error(f"[Config] Error loading {file_path}: {e}")
            return []
