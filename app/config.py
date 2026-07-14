from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _read_int(name: str, default: int, minimum: int = 1) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if value < minimum:
        raise RuntimeError(f"{name} must be at least {minimum}")
    return value


@dataclass(frozen=True, slots=True)
class Settings:
    session_root: Path
    cache_root: Path
    token_sha256: str
    max_timeout_seconds: int
    default_timeout_seconds: int
    max_output_bytes: int
    max_input_bytes: int
    max_file_output_bytes: int
    max_stream_file_output_bytes: int
    max_output_files: int
    max_concurrent_jobs: int

    @classmethod
    def from_env(cls) -> "Settings":
        max_timeout = _read_int("MAX_TIMEOUT_SECONDS", 300)
        default_timeout = min(
            _read_int("DEFAULT_TIMEOUT_SECONDS", 120), max_timeout
        )
        token_hash = os.getenv("SANDBOX_TOKEN_SHA256", "").strip().lower()
        if token_hash and (
            len(token_hash) != 64
            or any(character not in "0123456789abcdef" for character in token_hash)
        ):
            raise RuntimeError("SANDBOX_TOKEN_SHA256 must be a SHA-256 hex digest")
        return cls(
            session_root=Path(
                os.getenv("SANDBOX_SESSION_ROOT", "/tmp/sandbox-sessions")
            ),
            cache_root=Path(os.getenv("SANDBOX_CACHE_ROOT", "/tmp/sandbox-cache")),
            token_sha256=token_hash,
            max_timeout_seconds=max_timeout,
            default_timeout_seconds=default_timeout,
            max_output_bytes=_read_int("MAX_OUTPUT_BYTES", 2_000_000, 1024),
            max_input_bytes=_read_int("MAX_INPUT_BYTES", 20_000_000, 1024),
            max_file_output_bytes=_read_int(
                "MAX_FILE_OUTPUT_BYTES", 20_000_000, 1024
            ),
            max_stream_file_output_bytes=_read_int(
                "MAX_STREAM_FILE_OUTPUT_BYTES", 64_000_000, 1024
            ),
            max_output_files=_read_int("MAX_OUTPUT_FILES", 8),
            max_concurrent_jobs=_read_int("MAX_CONCURRENT_JOBS", 2),
        )
