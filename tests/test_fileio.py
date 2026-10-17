import os
import stat

from talkarr.fileio import DEFAULT_MODE, atomic_write_text, replace_preserving_metadata


def mode(p):
    return stat.S_IMODE(os.stat(p).st_mode)


def test_atomic_write_creates_file_with_default_mode(tmp_path):
    p = tmp_path / "new.srt"
    atomic_write_text(str(p), "hello")
    assert p.read_text() == "hello"
    assert mode(p) == DEFAULT_MODE


def test_atomic_write_keeps_existing_mode(tmp_path):
    p = tmp_path / "x.srt"
    p.write_text("old")
    os.chmod(p, 0o640)
    atomic_write_text(str(p), "new")
    assert p.read_text() == "new"
    assert mode(p) == 0o640


def test_atomic_write_keeps_newlines_verbatim(tmp_path):
    p = tmp_path / "x.srt"
    atomic_write_text(str(p), "a\r\nb\n")
    assert p.read_bytes() == b"a\r\nb\n"


def test_replace_from_0600_tempfile(tmp_path):
    dst = tmp_path / "dst.srt"
    dst.write_text("old")
    os.chmod(dst, 0o644)
    src = tmp_path / "src.tmp"
    src.write_text("new")
    os.chmod(src, 0o600)

    replace_preserving_metadata(str(src), str(dst))

    assert dst.read_text() == "new"
    assert mode(dst) == 0o644
    assert not src.exists()


def test_replace_across_directories(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    src = tmp_path / "a" / "s"
    src.write_text("x")
    dst = tmp_path / "b" / "d"
    replace_preserving_metadata(str(src), str(dst))
    assert dst.read_text() == "x"
    assert mode(dst) == DEFAULT_MODE
