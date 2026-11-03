import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

from .config import CLOCK_SKEW_SECONDS, SESSION_TTL_SECONDS


@dataclass
class Session:
    id: str
    key: bytes
    expires_at: float
    used_ivs: dict[str, float] = field(default_factory=dict)


class SessionStore:
    """In-memory AES session keys; nothing survives a restart and nothing is persisted."""

    def __init__(
        self,
        *,
        ttl_seconds: int = SESSION_TTL_SECONDS,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._ttl_seconds = ttl_seconds
        self._clock = clock if clock is not None else time.time
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    def create(self, key: bytes) -> Session:
        with self._lock:
            now = self._clock()
            for session_id, existing in list(self._sessions.items()):
                if existing.expires_at <= now:
                    self._sessions.pop(session_id, None)
            session = Session(
                id=uuid.uuid4().hex,
                key=key,
                expires_at=now + self._ttl_seconds,
            )
            self._sessions[session.id] = session
            return session

    def get(self, session_id: str) -> Session | None:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return None
            if session.expires_at <= self._clock():
                self._sessions.pop(session_id, None)
                return None
            return session

    def register_iv(self, session: Session, iv: str) -> bool:
        """Record a request IV, refusing one this session already used in the window."""
        with self._lock:
            now = self._clock()
            seen_at = session.used_ivs.get(iv)
            if seen_at is not None and now - seen_at < CLOCK_SKEW_SECONDS:
                return False
            session.used_ivs[iv] = now
            for stored_iv, stored_at in list(session.used_ivs.items()):
                if now - stored_at >= CLOCK_SKEW_SECONDS:
                    session.used_ivs.pop(stored_iv, None)
            return True
