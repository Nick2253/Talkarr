"""Library scans in the background, one at a time.

Walking a large library over a network share takes minutes, so the web request only
starts the scan and returns; the Library page shows its progress. Subtitles are
queued as they're found, so checking starts before the walk finishes.
"""

import logging
import os
import threading
import time

log = logging.getLogger(__name__)

_lock = threading.Lock()
_state = {"running": False}
_thread = None


class ScanBusy(Exception):
    pass


def start(directory, sub_filter="all", requeue=False):
    """Start a scan (validates the folder first). Raises ScanBusy if one is running."""
    from .app import check_scan_target, queue_scan

    check_scan_target(directory)
    global _thread
    with _lock:
        if _state.get("running"):
            raise ScanBusy(f"A scan of {_state['label']} is already running.")
        _state.clear()
        _state.update(
            running=True,
            directory=directory,
            label=_label(directory),
            filter=sub_filter,
            folders=0,
            seen=0,
            queued=0,
            started=time.time(),
            finished=None,
            error=None,
        )

    def progress(folders, seen, queued):
        _state.update(folders=folders, seen=seen, queued=queued)

    def run():
        try:
            queue_scan(directory, sub_filter, requeue=requeue, progress=progress)
        except Exception as e:  # noqa: BLE001 - shown on the Library page
            log.exception("Scan of %s failed", directory)
            _state["error"] = str(e)
        finally:
            _state.update(running=False, finished=time.time())

    _thread = threading.Thread(target=run, name="talkarr-scan", daemon=True)
    _thread.start()
    return state()


def state():
    return dict(_state)


def wait(timeout=None):
    """For tests and shutdown: block until the current scan finishes."""
    if _thread is not None:
        _thread.join(timeout)


def _label(directory):
    from .config import Config

    root = os.path.realpath(Config.MEDIA_PATH)
    real = os.path.realpath(directory)
    if real == root:
        return "the whole library"
    return os.path.relpath(real, root) if real.startswith(root + os.sep) else directory
