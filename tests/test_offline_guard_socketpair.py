"""H3 guard regressions: socketpair, owned self-loopback listeners, denials.

Every socket probe executes through the real offline launcher so the guard is
installed exactly as in production test runs. The allowances (socketpair on
its Windows loopback emulation, live listeners owned by this process) are
asserted together with their lifetime revocation and the unchanged
rejections (dead ports, listeners owned by another process, external
destinations, database ports), so the in-process listener rule cannot
silently widen the guard.
"""
from __future__ import annotations

import socket
import subprocess
import sys
import textwrap
import weakref
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "test_offline.py"


def _probe(tmp_path: Path, content: str, timeout: int = 120):
    test_file = tmp_path / "test_probe.py"
    test_file.write_text(textwrap.dedent(content), encoding="utf-8")
    return subprocess.run(
        [sys.executable, "-B", str(SCRIPT), "-q", str(test_file)],
        cwd=ROOT, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout, check=False,
    )


def test_socketpair_owned_listener_roundtrip_and_revocation(tmp_path):
    result = _probe(tmp_path, '''
        import socket
        import sys
        import pytest
        from ak_trading.testing.offline_guard import OfflineViolation

        def test_socketpair_roundtrip():
            one, two = socket.socketpair()
            with one, two:
                one.sendall(b"ping")
                assert two.recv(4) == b"ping"
                two.sendall(b"pong")
                assert one.recv(4) == b"pong"
            if sys.platform == "win32":
                # The Windows emulation binds a loopback listener per pair.
                six_a, six_b = socket.socketpair(socket.AF_INET6)
                with six_a, six_b:
                    six_a.sendall(b"v6-ok")
                    assert six_b.recv(5) == b"v6-ok"

        def test_owned_listener_and_revocation():
            listener = socket.socket()
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            port = listener.getsockname()[1]
            client = socket.create_connection(("127.0.0.1", port), timeout=5)
            accepted, _ = listener.accept()
            with client, accepted:
                client.sendall(b"request")
                assert accepted.recv(7) == b"request"
                accepted.sendall(b"answer")
                assert client.recv(6) == b"answer"
            listener.close()
            with pytest.raises(OfflineViolation):
                socket.create_connection(("127.0.0.1", port), timeout=1)

        def test_dead_port_and_datagram_rules():
            reserved = socket.socket()
            reserved.bind(("127.0.0.1", 0))
            dead_port = reserved.getsockname()[1]
            reserved.close()
            for method in ("connect", "connect_ex"):
                with socket.socket() as connection:
                    with pytest.raises(OfflineViolation):
                        getattr(connection, method)(("127.0.0.1", dead_port))
            udp_listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            udp_listener.bind(("127.0.0.1", 0))
            udp_port = udp_listener.getsockname()[1]
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
                sender.sendto(b"payload", ("127.0.0.1", udp_port))
                payload, _ = udp_listener.recvfrom(16)
                assert payload == b"payload"
            udp_listener.close()
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
                with pytest.raises(OfflineViolation):
                    sender.sendto(b"after-close", ("127.0.0.1", udp_port))
                with pytest.raises(OfflineViolation):
                    sender.sendto(b"dead", ("127.0.0.1", dead_port))
    ''')
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(sys.platform != "win32",
                    reason="Windows asyncio proactor loopback path (H3)")
def test_windows_asyncio_loopback_then_listener_revocation(tmp_path):
    result = _probe(tmp_path, '''
        import asyncio
        import sys
        import socket
        import pytest
        from ak_trading.testing.offline_guard import OfflineViolation

        def test_asyncio_loopback():
            assert sys.platform == "win32"

            async def scenario():
                async def echo(reader, writer):
                    data = await reader.readexactly(5)
                    writer.write(data[::-1])
                    await writer.drain()
                    writer.close()
                    await writer.wait_closed()
                server = await asyncio.start_server(echo, "127.0.0.1", 0)
                port = server.sockets[0].getsockname()[1]
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.write(b"hello")
                await writer.drain()
                assert await reader.readexactly(5) == b"olleh"
                writer.close()
                await writer.wait_closed()
                server.close()
                await server.wait_closed()
                return port

            port = asyncio.run(scenario())
            with pytest.raises(OfflineViolation):
                socket.create_connection(("127.0.0.1", port), timeout=1)
    ''')
    assert result.returncode == 0, result.stdout + result.stderr


def test_live_listener_owned_by_another_process_is_denied(tmp_path):
    result = _probe(tmp_path, '''
        import socket
        import subprocess
        import sys
        import pytest
        from ak_trading.testing.offline_guard import OfflineViolation

        CHILD = """
        import socket, time
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        probe = socket.create_connection(("127.0.0.1", port), timeout=5)
        accepted, _ = listener.accept()
        probe.close()
        accepted.close()
        print(port, flush=True)
        time.sleep(30)
        """

        def test_foreign_live_listener_denied():
            child = subprocess.Popen(
                [sys.executable, "-B", "-c", CHILD],
                stdout=subprocess.PIPE, text=True, encoding="utf-8",
            )
            try:
                port = int(child.stdout.readline().strip())
                # The child proved its listener live by connecting to it; this
                # process never registered that listener, so the guard refuses
                # the connect before any OS I/O touches the child's socket.
                for method in ("connect", "connect_ex"):
                    with socket.socket() as connection:
                        with pytest.raises(OfflineViolation):
                            getattr(connection, method)(("127.0.0.1", port))
                with pytest.raises(OfflineViolation):
                    socket.create_connection(("127.0.0.1", port), timeout=1)
            finally:
                child.kill()
                child.wait(timeout=10)
    ''')
    assert result.returncode == 0, result.stdout + result.stderr


def test_external_destinations_and_database_ports_stay_denied(tmp_path):
    result = _probe(tmp_path, '''
        import socket
        import pytest
        from ak_trading.testing.offline_guard import OfflineViolation

        def test_denials():
            with pytest.raises(OfflineViolation):
                socket.getaddrinfo("offline-probe.invalid", 443)
            for address in (("203.0.113.1", 443), ("198.51.100.7", 80),
                            ("127.0.0.1", 5432), ("127.0.0.1", 65432)):
                family = socket.AF_INET if "." in address[0] else socket.AF_INET6
                for method in ("connect", "connect_ex"):
                    with socket.socket(family) as connection:
                        with pytest.raises(OfflineViolation):
                            getattr(connection, method)(address)
                with pytest.raises(OfflineViolation):
                    socket.create_connection(address, timeout=0.2)
            if socket.has_ipv6:
                with socket.socket(socket.AF_INET6) as connection:
                    with pytest.raises(OfflineViolation):
                        connection.connect(("::1", 5432))
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
                with pytest.raises(OfflineViolation):
                    udp.sendto(b"blocked", ("203.0.113.9", 53))
    ''')
    assert result.returncode == 0, result.stdout + result.stderr


def test_listener_permission_is_tied_to_socket_identity_and_lifetime(monkeypatch):
    """Pure unit check of the guard predicate (no OS connection is made)."""
    from ak_trading.testing import offline_guard

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    monkeypatch.setattr(offline_guard, "_loopback_listeners", weakref.WeakSet([listener]))
    offline_guard._check_connection(("127.0.0.1", port))  # live owner: allowed
    with pytest.raises(offline_guard.OfflineViolation):
        offline_guard._check_connection(("127.0.0.1", port + 1))  # different port
    with pytest.raises(offline_guard.OfflineViolation):
        offline_guard._check_connection(("203.0.113.1", port))  # external host
    listener.close()
    with pytest.raises(offline_guard.OfflineViolation):
        offline_guard._check_connection(("127.0.0.1", port))  # closed: revoked
    with pytest.raises(offline_guard.OfflineViolation):
        offline_guard._check_connection(("127.0.0.1", 5432))  # database port
    with pytest.raises(offline_guard.OfflineViolation):
        offline_guard._check_connection("not-a-tuple-address")
