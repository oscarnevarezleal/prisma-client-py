"""Opt-in sharing of the query-engine process between clients in one process.

The query engine is a separate Rust binary speaking HTTP on localhost. Today
every connected client — including a sync and an async client for the *same*
schema and datasource in the same process — spawns its own engine (~20+ MB RSS
and a spawn/healthcheck round on every connect).

With ``PRISMA_PY_SHARED_ENGINE=1``, engines register their spawned process here
keyed by (schema path, datasource overrides); a later ``connect()`` for the
same key attaches to the existing process over HTTP instead of spawning. The
registry owns the shared processes: refcounted release, killed when the last
user disconnects (and swept at interpreter exit as a backstop).

Scope and caveats:
- per-process only; never shared across OS processes
- both clients must resolve the same datasource (the key guarantees this)
- transactions are engine-scoped, which is exactly why sharing is safe: the
  engine already multiplexes concurrent requests/transactions over HTTP
"""

from __future__ import annotations

import os
import atexit
import logging
import threading
import subprocess
from typing import Any, Dict, Tuple, Optional
from dataclasses import dataclass

log: logging.Logger = logging.getLogger(__name__)

_ENV_FLAG = 'PRISMA_PY_SHARED_ENGINE'

SharedKey = Tuple[str, str]


def enabled() -> bool:
    return os.environ.get(_ENV_FLAG, '') not in ('', '0', 'false', 'False')


def make_key(dml_path: object, datasources: object) -> SharedKey:
    return (str(dml_path), repr(datasources))


@dataclass
class _Entry:
    url: str
    process: subprocess.Popen  # type: ignore[type-arg]
    refcount: int = 1


class _Registry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: Dict[SharedKey, _Entry] = {}
        atexit.register(self._sweep)

    def attach(self, key: SharedKey) -> Optional[str]:
        """Return the URL of a live shared engine for `key`, bumping its refcount."""
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            if entry.process.poll() is not None:
                # died behind our back; drop the stale entry
                del self._entries[key]
                return None
            entry.refcount += 1
            log.debug('attached to shared engine %s (refcount=%d)', entry.url, entry.refcount)
            return entry.url

    def register(self, key: SharedKey, url: str, process: subprocess.Popen) -> None:  # type: ignore[type-arg]
        """Take ownership of a freshly spawned engine process."""
        with self._lock:
            self._entries[key] = _Entry(url=url, process=process)
            log.debug('registered shared engine %s', url)

    def release(self, key: SharedKey, *, kill: Any) -> None:
        """Drop one reference; kill the process when nobody is left.

        `kill` is a callable(process) so the engine's own (timeout-aware)
        termination logic is reused rather than duplicated here.
        """
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return
            entry.refcount -= 1
            log.debug('released shared engine %s (refcount=%d)', entry.url, entry.refcount)
            if entry.refcount > 0:
                return
            del self._entries[key]
            process = entry.process
        # kill outside the lock; it can block on process.wait()
        try:
            kill(process)
        except Exception:
            log.debug('error terminating shared engine', exc_info=True)

    def _sweep(self) -> None:
        with self._lock:
            entries = list(self._entries.values())
            self._entries.clear()
        for entry in entries:
            try:
                if entry.process.poll() is None:
                    entry.process.kill()
            except Exception:
                pass


registry: _Registry = _Registry()
