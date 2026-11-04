"""Thread and process serialization for a dataset's read/merge/atomic-write cycle."""
from contextlib import contextmanager
import errno
import os
from pathlib import Path
import sys
import threading
import time

_guard = threading.Lock()
_locks: dict[str, threading.RLock] = {}
_held = threading.local()


@contextmanager
def _outer_lock(path: Path):
    key = os.path.normcase(str(path.resolve()))
    with _guard:
        lock = _locks.setdefault(key, threading.RLock())
    with lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(str(path) + '.lock', 'a+b') as handle:
            if sys.platform == 'win32':
                import msvcrt
                if os.fstat(handle.fileno()).st_size == 0:
                    handle.write(b'\0')
                    handle.flush()
                while True:
                    try:
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError as exc:
                        if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                            raise
                        time.sleep(.02)
                try:
                    yield
                finally:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle, fcntl.LOCK_UN)


@contextmanager
def dataset_lock(path: Path):
    """Re-entrant for a thread; only the outer scope acquires the OS lock."""
    key = os.path.normcase(str(path.resolve()))
    keys = getattr(_held, 'keys', None)
    if keys is None:
        keys = _held.keys = set()
    if key in keys:
        yield
        return
    with _outer_lock(path):
        keys.add(key)
        try:
            yield
        finally:
            keys.remove(key)
