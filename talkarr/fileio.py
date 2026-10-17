"""Safe in-place replacement of files in the media library.

Every write goes to a temp file in the same directory and is renamed over the
target, so readers never see a half-written subtitle. The target's permission
bits (and owner/group, when we're allowed to set them) are carried over:
``tempfile.mkstemp`` creates files as 0600, which would otherwise make the
subtitle unreadable to a media server running as a different user.
"""

import contextlib
import os
import shutil
import tempfile

DEFAULT_MODE = 0o644


def atomic_write_text(path, text, encoding="utf-8"):
    """Atomically replace ``path`` with ``text``."""
    fd, tmp = tempfile.mkstemp(prefix=".talkarr-", suffix=".tmp", dir=os.path.dirname(path) or ".")
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        replace_preserving_metadata(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def replace_preserving_metadata(src, dst):
    """Move ``src`` over ``dst``, giving it ``dst``'s mode and ownership.

    If ``dst`` doesn't exist, ``src`` gets ``DEFAULT_MODE``.
    """
    try:
        st = os.stat(dst)
    except FileNotFoundError:
        os.chmod(src, DEFAULT_MODE)
    else:
        os.chmod(src, st.st_mode & 0o7777)
        # Only root can change ownership; as a regular user this is a no-op
        # when we already own the file and fails harmlessly otherwise.
        with contextlib.suppress(PermissionError):
            os.chown(src, st.st_uid, st.st_gid)

    if os.path.dirname(os.path.abspath(src)) == os.path.dirname(os.path.abspath(dst)):
        os.replace(src, dst)
    else:
        shutil.move(src, dst)
