"""Persistance des decisions du user sur les warnings (cosmetique vs dangereux).

Stockage : /data/security-decisions.json (volume Docker partage).
Cle = SHA256(message[:100])[:12] — stable malgre les ajouts/suppressions de warnings.
"""
import json
import os
import hashlib
import tempfile
from datetime import datetime, timezone
from threading import Lock

DECISIONS_PATH = os.environ.get("DECISIONS_PATH", "/data/security-decisions.json")
_lock = Lock()


def warning_key(message: str) -> str:
    """Cle stable basee sur le message (premier 100 chars)."""
    return hashlib.sha256(message[:100].encode("utf-8", errors="ignore")).hexdigest()[:12]


def load_decisions() -> dict:
    """Retourne {key: {'is_danger': bool, 'message_preview': str, 'updated_at': iso}}"""
    with _lock:
        try:
            with open(DECISIONS_PATH) as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return {}


def save_decision(key: str, is_danger: bool, message_preview: str = "") -> None:
    """Ecrit la decision du user.
    is_danger=True signifie 'vrai danger' (par defaut, pas relu).
    is_tolerated=True signifie 'user a relu et accepte le warning'.
    Les deux champs sont stockes pour clarte (mutuellement exclusifs)."""
    with _lock:
        try:
            with open(DECISIONS_PATH) as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            data = {}
        data[key] = {
            "is_danger": bool(is_danger),
            "is_tolerated": not bool(is_danger),
            "message_preview": message_preview[:80],
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        # Atomic write
        cache_dir = os.path.dirname(DECISIONS_PATH) or "."
        os.makedirs(cache_dir, exist_ok=True)
        tmp_fd, tmp_path = tempfile.mkstemp(dir=cache_dir, suffix=".tmp")
        try:
            with os.fdopen(tmp_fd, "w") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp_path, DECISIONS_PATH)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise


def get_decision(key: str) -> dict | None:
    """Retourne la decision pour une cle, ou None si pas de decision enregistree."""
    return load_decisions().get(key)
