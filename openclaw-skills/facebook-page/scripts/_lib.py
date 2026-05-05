"""Shared helpers for facebook-page skill scripts."""

import os
from pathlib import Path


def _skill_dir() -> Path:
    """Resolve the skill directory regardless of where the script is invoked from."""
    return Path(__file__).resolve().parent.parent


def load_env() -> dict:
    """Load `.env` next to this skill into a plain dict.

    The .env format is `key=value` per line; lines starting with `#` are ignored.
    Values may be quoted with single or double quotes.
    """
    env_path = _skill_dir() / ".env"
    out: dict[str, str] = {}
    if not env_path.exists():
        raise FileNotFoundError(
            f"facebook-page .env not found at {env_path}. "
            "Hoàng đã cấu hình sẵn — kiểm tra lại file."
        )
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if (v.startswith('"') and v.endswith('"')) or (
            v.startswith("'") and v.endswith("'")
        ):
            v = v[1:-1]
        out[k.strip()] = v
    # Allow env-var overrides
    for k in list(out.keys()):
        if k in os.environ and os.environ[k]:
            out[k] = os.environ[k]
    return out


def require(env: dict, key: str) -> str:
    val = env.get(key, "").strip()
    if not val:
        raise KeyError(f"missing `{key}` in facebook-page/.env")
    return val


GRAPH = "https://graph.facebook.com/v21.0"
GRAPH_VIDEO = "https://graph-video.facebook.com/v21.0"
