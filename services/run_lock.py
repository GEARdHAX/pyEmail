"""Cross-process run lock (one pyEmail job that touches Gmail/Sheets/DB at a time).

Uses an operating-system file lock (msvcrt on Windows, flock elsewhere) on the first byte of a lock
file. Unlike a "pid file" this needs no stale-lock logic: the OS drops the lock the instant the
holding process exits or crashes, so a dead job can never block the next one.

    with RunLock('data/pyemail.lock', timeout=120):   # waits up to 120 s, else raises RunBusy
        ...
    lock = RunLock(path); if lock.acquire(): ...      # non-blocking try

Diagnostic text (pid + start time, never anything secret) is written after the locked byte.
"""
import json
import os
import time
from datetime import datetime, timezone

try:  # Windows
    import msvcrt
except ImportError:  # pragma: no cover
    msvcrt = None
try:  # POSIX
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None


class RunBusy(RuntimeError):
    """Another pyEmail job holds the lock."""


class RunLock:
    def __init__(self, path, timeout=0.0, poll=0.25):
        self.path, self.timeout, self.poll = path, timeout, poll
        self._fd = None

    # -- low level ---------------------------------------------------------
    def _try_lock(self, fd):
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            if msvcrt:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def acquire(self, timeout=None):
        if self._fd is not None:
            return True
        timeout = self.timeout if timeout is None else timeout
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT)
            if self._try_lock(fd):
                self._fd = fd
                info = json.dumps({'pid': os.getpid(), 'since': datetime.now(timezone.utc).isoformat(timespec='seconds')})
                os.lseek(fd, 1, os.SEEK_SET)
                os.write(fd, info.encode())
                return True
            os.close(fd)
            if time.monotonic() >= deadline:
                return False
            time.sleep(self.poll)

    def release(self):
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            if msvcrt:
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            os.close(fd)

    def holder(self):
        """Best-effort description of the current holder (pid + since); never raises."""
        try:
            with open(self.path, 'rb') as f:
                f.seek(1)
                data = json.loads(f.read().decode() or '{}')
            return {'pid': data.get('pid'), 'since': data.get('since')}
        except Exception:
            return {}

    # -- context manager ---------------------------------------------------
    def __enter__(self):
        if not self.acquire():
            raise RunBusy(f"another pyEmail job is running (lock {os.path.basename(self.path)})")
        return self

    def __exit__(self, *exc):
        self.release()
        return False
