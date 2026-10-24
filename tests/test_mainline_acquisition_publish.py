"""OPT-05 / F01: SYNTHETIC offline publication counterexamples.

Only temporary files and real test threads are used. No DB, network or SDK.
The concurrency assertions concern the winner's bytes/hash, not rename calls.
"""

from __future__ import annotations

import builtins
from concurrent.futures import ThreadPoolExecutor
import errno
import hashlib
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace

import pytest

from ak_trading.acquisition import artifacts as art


PAYLOAD = b"SYNTHETIC publication\x00\xff\n" + bytes(range(256)) * 1024
OTHER_PAYLOAD = b"SYNTHETIC competing publisher\n" * 4096


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _assert_complete(path: Path, payload: bytes, digest: str) -> None:
    assert path.read_bytes() == payload
    assert digest == _sha(payload) == _sha(path.read_bytes())
    assert art.verify_published_file(path, digest) == len(payload)


class _FileProbe:
    """Wrap the real exclusive temp file, retaining actual writes and fsync."""

    def __init__(self, handle, write=None, flush=None):
        self.handle = handle
        self.write_probe = write
        self.flush_probe = flush

    def __enter__(self):
        self.handle.__enter__()
        return self

    def __exit__(self, *args):
        return self.handle.__exit__(*args)

    def write(self, payload):
        if self.write_probe is not None:
            return self.write_probe(self.handle, payload)
        return self.handle.write(payload)

    def flush(self):
        if self.flush_probe is not None:
            return self.flush_probe(self.handle)
        return self.handle.flush()

    def fileno(self):
        return self.handle.fileno()


def test_candidate_import_identity():
    root = Path(__file__).resolve().parents[1]
    assert Path(art.__file__).resolve() == root / "src/ak_trading/acquisition/artifacts.py"


@pytest.mark.parametrize("payload", [b"", PAYLOAD], ids=["empty", "binary"])
def test_success_returns_complete_bytes_and_sha(tmp_path, payload):
    path, digest = art.atomic_publish_bytes(tmp_path, "result.bin", payload)
    assert path == tmp_path / "result.bin"
    _assert_complete(path, payload, digest)
    assert set(tmp_path.iterdir()) == {path}


def test_exclusive_write_flush_fsync_precede_visible_complete_publication(tmp_path, monkeypatch):
    destination = tmp_path / "ordered.bin"
    events = []
    temp_paths = []
    real_fsync = art.os.fsync
    real_link = art.os.link

    def write(handle, payload):
        assert not destination.exists()
        events.append("write")
        return handle.write(payload)

    def flush(handle):
        assert not destination.exists()
        events.append("flush")
        return handle.flush()

    def open_temp(path, mode):
        assert mode == "xb"
        assert Path(path).parent == destination.parent
        temp_paths.append(Path(path))
        events.append("exclusive-open")
        return _FileProbe(builtins.open(path, mode), write=write, flush=flush)

    def fsync(fd):
        assert events == ["exclusive-open", "write", "flush"]
        assert not destination.exists()
        assert temp_paths[0].read_bytes() == PAYLOAD
        real_fsync(fd)
        events.append("fsync")

    def link(source, target):
        assert events == ["exclusive-open", "write", "flush", "fsync"]
        assert Path(source) == temp_paths[0]
        assert Path(target) == destination
        assert not destination.exists()
        assert Path(source).read_bytes() == PAYLOAD
        real_link(source, target)
        _assert_complete(destination, PAYLOAD, _sha(PAYLOAD))
        events.append("complete-publication")

    monkeypatch.setattr(art, "open", open_temp, raising=False)
    monkeypatch.setattr(art.os, "fsync", fsync)
    monkeypatch.setattr(art.os, "link", link)
    path, digest = art.atomic_publish_bytes(tmp_path, destination.name, PAYLOAD)
    assert events == ["exclusive-open", "write", "flush", "fsync", "complete-publication"]
    _assert_complete(path, PAYLOAD, digest)
    assert set(tmp_path.iterdir()) == {destination}


def test_controlled_interleaving_preserves_first_committed_winner(tmp_path, monkeypatch):
    destination = tmp_path / "interleaved.bin"
    real_fsync = art.os.fsync
    injected = False
    winner = []

    def let_other_publisher_commit(fd):
        nonlocal injected
        real_fsync(fd)
        if not injected:
            injected = True
            assert not destination.exists()
            winner.append(art.atomic_publish_bytes(tmp_path, destination.name, OTHER_PAYLOAD))
            _assert_complete(winner[0][0], OTHER_PAYLOAD, winner[0][1])

    monkeypatch.setattr(art.os, "fsync", let_other_publisher_commit)
    with pytest.raises(art.ArtifactError, match="refusing to overwrite"):
        art.atomic_publish_bytes(tmp_path, destination.name, PAYLOAD)
    assert len(winner) == 1
    _assert_complete(destination, OTHER_PAYLOAD, winner[0][1])
    assert set(tmp_path.iterdir()) == {destination}


@pytest.mark.parametrize("round_number", range(3))
def test_two_real_threads_have_exactly_one_winner(tmp_path, monkeypatch, round_number):
    destination = tmp_path / f"thread-race-{round_number}.bin"
    ready = Barrier(2)
    real_fsync = art.os.fsync

    def synchronize_complete_temps(fd):
        real_fsync(fd)
        # Neither thread can publish before both have finished their fsync.
        assert not destination.exists()
        ready.wait(timeout=10)

    def publish(payload):
        try:
            result = art.atomic_publish_bytes(tmp_path, destination.name, payload)
        except art.ArtifactError as exc:
            return "refused", payload, exc
        return "published", payload, result

    monkeypatch.setattr(art.os, "fsync", synchronize_complete_temps)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(publish, payload) for payload in (PAYLOAD, OTHER_PAYLOAD)]
        outcomes = [future.result(timeout=20) for future in futures]
    winners = [item for item in outcomes if item[0] == "published"]
    losers = [item for item in outcomes if item[0] == "refused"]
    assert len(winners) == len(losers) == 1, outcomes
    _, winning_payload, (path, digest) = winners[0]
    assert path == destination
    _assert_complete(path, winning_payload, digest)
    assert "refusing to overwrite" in str(losers[0][2])
    assert set(tmp_path.iterdir()) == {destination}


@pytest.mark.parametrize("existing", [b"", OTHER_PAYLOAD], ids=["empty", "nonempty"])
def test_preexisting_destination_remains_byte_identical(tmp_path, existing):
    destination = tmp_path / "preexisting.bin"
    destination.write_bytes(existing)
    before = destination.stat()
    with pytest.raises(art.ArtifactError, match="refusing to overwrite"):
        art.atomic_publish_bytes(tmp_path, destination.name, PAYLOAD)
    _assert_complete(destination, existing, _sha(existing))
    after = destination.stat()
    assert (after.st_ino, after.st_mtime_ns, after.st_size) == (
        before.st_ino, before.st_mtime_ns, before.st_size
    )
    assert set(tmp_path.iterdir()) == {destination}


def test_temp_name_collision_neither_truncates_nor_deletes_foreign_file(tmp_path, monkeypatch):
    monkeypatch.setattr(art.uuid, "uuid4", lambda: SimpleNamespace(hex="SYNTHETICcollision"))
    foreign = tmp_path / ".tmp-SYNTHETICcollision-target.bin"
    foreign.write_bytes(OTHER_PAYLOAD)
    with pytest.raises(FileExistsError):
        art.atomic_publish_bytes(tmp_path, "target.bin", PAYLOAD)
    _assert_complete(foreign, OTHER_PAYLOAD, _sha(OTHER_PAYLOAD))
    assert set(tmp_path.iterdir()) == {foreign}


@pytest.mark.parametrize("stage", ["write", "interrupt", "short-write", "flush", "fsync"])
def test_precommit_io_failure_has_no_partial_target_and_cleans_only_own_temp(
    tmp_path, monkeypatch, stage
):
    destination = tmp_path / "incomplete.bin"
    foreign = tmp_path / ".tmp-SYNTHETIC-another-owner"
    foreign.write_bytes(OTHER_PAYLOAD)
    visited = []

    def write(handle, payload):
        assert not destination.exists()
        visited.append("write")
        if stage in {"write", "interrupt", "short-write"}:
            count = handle.write(payload[:17])
            handle.flush()
            assert count == 17
            if stage == "short-write":
                return count
            if stage == "interrupt":
                raise KeyboardInterrupt("SYNTHETIC interrupted write")
            raise OSError(errno.EIO, "SYNTHETIC write failure")
        return handle.write(payload)

    def flush(handle):
        assert not destination.exists()
        visited.append("flush")
        if stage == "flush":
            raise OSError(errno.EIO, "SYNTHETIC flush failure")
        return handle.flush()

    def fail_fsync(fd):
        assert not destination.exists()
        visited.append("fsync")
        raise OSError(errno.EIO, "SYNTHETIC fsync failure")

    def open_temp(path, mode):
        return _FileProbe(builtins.open(path, mode), write=write, flush=flush)

    monkeypatch.setattr(art, "open", open_temp, raising=False)
    if stage == "fsync":
        monkeypatch.setattr(art.os, "fsync", fail_fsync)
    error = KeyboardInterrupt if stage == "interrupt" else OSError
    with pytest.raises(error):
        art.atomic_publish_bytes(tmp_path, destination.name, PAYLOAD)
    assert visited
    if stage == "fsync":
        assert visited == ["write", "flush", "fsync"]
    assert not destination.exists()
    assert set(tmp_path.iterdir()) == {foreign}
    _assert_complete(foreign, OTHER_PAYLOAD, _sha(OTHER_PAYLOAD))


@pytest.mark.parametrize("failure", ["io", "unsupported", "cross-volume", "unimplemented", "missing-api"])
def test_link_failure_or_unsupported_primitive_fails_closed(tmp_path, monkeypatch, failure):
    destination = tmp_path / "unsupported.bin"
    foreign = tmp_path / ".tmp-SYNTHETIC-foreign"
    foreign.write_bytes(OTHER_PAYLOAD)
    attempted = []

    def fail_link(source, target):
        attempted.append(Path(source))
        assert Path(target) == destination
        assert not destination.exists()
        _assert_complete(Path(source), PAYLOAD, _sha(PAYLOAD))
        if failure == "unimplemented":
            raise NotImplementedError("SYNTHETIC link unavailable")
        code = {"io": errno.EIO, "unsupported": errno.EOPNOTSUPP, "cross-volume": errno.EXDEV}[failure]
        raise OSError(code, "SYNTHETIC link failure")

    if failure == "missing-api":
        monkeypatch.delattr(art.os, "link")
    else:
        monkeypatch.setattr(art.os, "link", fail_link)
    with pytest.raises(art.ArtifactError, match="publish"):
        art.atomic_publish_bytes(tmp_path, destination.name, PAYLOAD)
    if failure != "missing-api":
        assert len(attempted) == 1
    assert not destination.exists()
    assert set(tmp_path.iterdir()) == {foreign}
    _assert_complete(foreign, OTHER_PAYLOAD, _sha(OTHER_PAYLOAD))


@pytest.mark.parametrize("stage", ["fsync", "link", "destination-exists"])
def test_precommit_cleanup_failure_preserves_primary_error_and_foreign_evidence(
    tmp_path, monkeypatch, stage
):
    destination = tmp_path / "precommit.bin"
    foreign = tmp_path / ".tmp-SYNTHETIC-foreign"
    foreign.write_bytes(OTHER_PAYLOAD)
    real_fsync = art.os.fsync
    real_unlink = Path.unlink
    failed_cleanup = []

    def fsync(fd):
        if stage == "fsync":
            raise OSError(errno.EIO, "SYNTHETIC fsync failure")
        real_fsync(fd)
        if stage == "destination-exists":
            destination.write_bytes(OTHER_PAYLOAD)

    def fail_link(source, target):
        raise OSError(errno.EIO, "SYNTHETIC link failure")

    def deny_own_temp_cleanup(path, *args, **kwargs):
        if path.parent == tmp_path and path.name.startswith(".tmp-"):
            assert path != foreign
            failed_cleanup.append(path)
            raise PermissionError("SYNTHETIC temp cleanup denied")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(art.os, "fsync", fsync)
    monkeypatch.setattr(Path, "unlink", deny_own_temp_cleanup)
    if stage == "link":
        monkeypatch.setattr(art.os, "link", fail_link)
    expected = OSError if stage == "fsync" else art.ArtifactError
    with pytest.raises(expected) as captured:
        art.atomic_publish_bytes(tmp_path, destination.name, PAYLOAD)
    assert not isinstance(captured.value, PermissionError)
    assert any("cleanup" in note for note in getattr(captured.value, "__notes__", []))
    assert len(failed_cleanup) == 1
    _assert_complete(failed_cleanup[0], PAYLOAD, _sha(PAYLOAD))
    _assert_complete(foreign, OTHER_PAYLOAD, _sha(OTHER_PAYLOAD))
    expected_files = {foreign, failed_cleanup[0]}
    if stage == "destination-exists":
        assert "refusing to overwrite" in str(captured.value)
        _assert_complete(destination, OTHER_PAYLOAD, _sha(OTHER_PAYLOAD))
        expected_files.add(destination)
    else:
        assert not destination.exists()
    assert set(tmp_path.iterdir()) == expected_files


def test_postcommit_cleanup_failure_is_explicit_and_retry_cannot_overwrite(tmp_path, monkeypatch):
    destination = tmp_path / "committed.bin"
    real_unlink = Path.unlink
    failed_cleanup = []

    def deny_temp_cleanup(path, *args, **kwargs):
        if path.parent == tmp_path and path.name.startswith(".tmp-"):
            failed_cleanup.append(path)
            raise PermissionError("SYNTHETIC cleanup denied after commit")
        return real_unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", deny_temp_cleanup)
        with pytest.raises(art.ArtifactError, match="already committed") as captured:
            art.atomic_publish_bytes(tmp_path, destination.name, PAYLOAD)
    error = captured.value
    assert error.committed is True
    assert error.path == destination
    assert error.sha256 == _sha(PAYLOAD)
    assert "do not retry" in str(error)
    assert failed_cleanup == [error.temp_path]
    _assert_complete(destination, PAYLOAD, error.sha256)
    _assert_complete(error.temp_path, PAYLOAD, error.sha256)
    with pytest.raises(art.ArtifactError, match="refusing to overwrite"):
        art.atomic_publish_bytes(tmp_path, destination.name, OTHER_PAYLOAD)
    _assert_complete(destination, PAYLOAD, error.sha256)
    assert set(tmp_path.iterdir()) == {destination, error.temp_path}


@pytest.mark.parametrize("kind", ["traversal", "absolute"])
def test_direct_publish_refuses_filename_escape(tmp_path, kind):
    root = tmp_path / "root"
    root.mkdir()
    filename = "../escape.bin" if kind == "traversal" else str(tmp_path / "escape.bin")
    with pytest.raises(art.ArtifactError):
        art.atomic_publish_bytes(root, filename, PAYLOAD)
    assert list(root.iterdir()) == []
    assert not (tmp_path / "escape.bin").exists()


@pytest.mark.parametrize("kind", ["junction", "symlink"])
def test_direct_publish_refuses_reparse_directory_chain(tmp_path, monkeypatch, kind):
    link = tmp_path / "SYNTHETIC-reparse"
    link.mkdir()
    if kind == "junction":
        monkeypatch.setattr(art.os.path, "isjunction", lambda path: Path(path) == link, raising=False)
    else:
        real_is_symlink = Path.is_symlink
        monkeypatch.setattr(Path, "is_symlink", lambda path: path == link or real_is_symlink(path))
    with pytest.raises(art.ArtifactError, match="reparse"):
        art.atomic_publish_bytes(link, "escape.bin", PAYLOAD)
    assert list(link.iterdir()) == []
