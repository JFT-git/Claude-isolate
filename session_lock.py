"""One launcher per disk, including direct CLI invocations and paused VMs."""
from contextlib import contextmanager
import os


@contextmanager
def exclusive(path):
    with open(path, 'a+b') as lock:
        if os.name == 'nt':
            import msvcrt
            if not lock.tell():
                lock.write(b'0')
                lock.flush()
            lock.seek(0)
            try:
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as error:
                raise RuntimeError('Environment is already running or stopping') from error
        else:
            import fcntl
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                raise RuntimeError('Environment is already running or stopping') from error
        try:
            yield
        finally:
            if os.name == 'nt':
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)

