"""
protobuf_handler.py

AES-CBC encryption + Protobuf serialisation/deserialisation helpers.
Cryptographic parameters come from Config so they can be overridden
via environment variables without touching the code.
"""

import binascii
import logging
from typing import Optional

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
from google.protobuf.message import DecodeError

import like_pb2
import like_count_pb2
import uid_generator_pb2
from config import Config

logger = logging.getLogger(__name__)


# ── Encryption ────────────────────────────────────────────────────────────────

def encrypt_message(plaintext: bytes) -> Optional[str]:
    """AES-128-CBC encrypt bytes, return hex-encoded ciphertext string."""
    try:
        cipher = AES.new(Config.AES_KEY, AES.MODE_CBC, Config.AES_IV)
        padded = pad(plaintext, AES.block_size)
        return binascii.hexlify(cipher.encrypt(padded)).decode("utf-8")
    except Exception as e:
        logger.error(f"[Crypto] encrypt_message failed: {e}")
        return None


# ── Encode helpers (protobuf → encrypt → hex string) ─────────────────────────

def encode_for_like(uid: str, region: str) -> Optional[str]:
    """Build and encrypt the LikeProfile protobuf for `uid` / `region`."""
    try:
        msg        = like_pb2.like()
        msg.uid    = int(uid)
        msg.region = region
        return encrypt_message(msg.SerializeToString())
    except Exception as e:
        logger.error(f"[Protobuf] encode_for_like failed: {e}")
        return None


def encode_for_uid(uid: str) -> Optional[str]:
    """Build and encrypt the GetPlayerPersonalShow protobuf for `uid`."""
    try:
        msg         = uid_generator_pb2.uid_generator()
        msg.saturn_ = int(uid)
        msg.garena  = 1
        return encrypt_message(msg.SerializeToString())
    except Exception as e:
        logger.error(f"[Protobuf] encode_for_uid failed: {e}")
        return None


# ── Decoder ───────────────────────────────────────────────────────────────────

def decode_like_count(binary_data: bytes) -> Optional[like_count_pb2.Info]:
    """Parse a raw binary response body into a like_count_pb2.Info object."""
    try:
        info = like_count_pb2.Info()
        info.ParseFromString(binary_data)
        return info
    except DecodeError as e:
        logger.error(f"[Protobuf] DecodeError: {e}")
        return None
    except Exception as e:
        logger.error(f"[Protobuf] Unexpected decode error: {e}")
        return None
