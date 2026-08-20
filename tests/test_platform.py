"""Tests for the host adapter.

The behavioural tests here start a real server and talk to the real clipboard
rather than mocking them: the entire point of this module is that it drives
the actual host, so a mocked version would prove nothing about the port.
"""

import asyncio

import pytest

from core import platform as host
from conftest import mac_only


def test_exactly_one_host_is_detected():
    assert sum([host.IS_MAC, host.IS_WSL, host.IS_LINUX]) == 1
    assert host.NAME in {"macOS", "WSL2", "Linux"}


# ── prompt text ──────────────────────────────────────────────────
# The most dangerous failure mode of this port is not a crash: it is the model
# being told, in prose, that it is on the other machine. It then emits fluent,
# well-formed, completely unrunnable commands.

@mac_only
@pytest.mark.parametrize("blob", [
    "ENVIRONMENT", "SHELL_RULES", "SEARCH_EXAMPLE",
    "DEV_SERVER_EXAMPLE", "PATH_STYLE", "FILE_MANAGER", "HOST_DESCRIPTION",
])
def test_mac_prompt_text_never_mentions_windows(blob):
    text = getattr(host, blob).lower()
    for leak in ("powershell", "wsl", "/mnt/", "explorer", "c:\\", "d:\\", ".exe"):
        assert leak not in text, f"{blob} leaks {leak!r} into a macOS prompt"


@mac_only
def test_mac_prompt_text_describes_mac_tooling():
    combined = (host.ENVIRONMENT + host.SHELL_RULES).lower()
    assert "/users" in combined
    assert "macos" in combined


def test_prompt_constants_are_non_empty():
    for blob in ("ENVIRONMENT", "SHELL_RULES", "SEARCH_EXAMPLE",
                 "DEV_SERVER_EXAMPLE", "PATH_STYLE", "FILE_MANAGER"):
        assert getattr(host, blob).strip()


# ── real host operations ─────────────────────────────────────────

@mac_only
async def test_clipboard_round_trip():
    text = "mimir-clipboard-check-∑"
    rc, _, err = await host.copy_to_clipboard(text)
    assert rc == 0, err
    rc, out, _ = await host._exec(["pbpaste"], timeout=5)
    assert out == text


@mac_only
async def test_gpu_summary_reports_something():
    assert (await host.gpu_summary()).strip()


async def _serve(port: int, content_type: str, body: bytes):
    async def handle(reader, writer):
        await reader.read(4096)
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: %s\r\nContent-Length: %d\r\n\r\n%s"
            % (content_type.encode(), len(body), body)
        )
        await writer.drain()
        writer.close()
    return await asyncio.start_server(handle, "127.0.0.1", port)


@mac_only
async def test_finds_a_dev_server_serving_html(tmp_path):
    srv = await _serve(5173, "text/html", b"<html><body>vite</body></html>")
    try:
        assert await host.find_dev_server(tmp_path) == "http://localhost:5173"
    finally:
        srv.close()
        await srv.wait_closed()


@mac_only
async def test_excluded_ports_are_not_reported(tmp_path):
    """A server that was already up before the task started is not the one we
    just launched, so OPENDEV must not point the user at it."""
    srv = await _serve(5173, "text/html", b"<html>old</html>")
    try:
        assert await host.find_dev_server(tmp_path, {5173}) is None
    finally:
        srv.close()
        await srv.wait_closed()


@mac_only
async def test_json_api_is_not_mistaken_for_a_dev_server(tmp_path):
    """Databases and bare APIs squat on dev ports; only HTML counts."""
    srv = await _serve(8001, "application/json", b'{"ok":true}')
    try:
        found = await host.find_dev_server(tmp_path)
        assert found != "http://localhost:8001"
    finally:
        srv.close()
        await srv.wait_closed()


@mac_only
async def test_scan_open_ports_sees_a_listening_port(tmp_path):
    srv = await _serve(4000, "text/html", b"<html>x</html>")
    try:
        assert 4000 in await host.scan_open_ports(tmp_path)
    finally:
        srv.close()
        await srv.wait_closed()


async def test_missing_binary_is_reported_not_raised():
    """Executors must return errors as text -- the agent loop feeds them back
    to the model, which cannot recover from an exception."""
    rc, _, err = await host._exec(["definitely-not-a-real-binary"], timeout=5)
    assert rc == -1
    assert "not found" in err.lower()


async def test_timeout_is_reported_not_raised():
    rc, _, err = await host._exec(["sleep", "5"], timeout=1)
    assert rc == -1
    assert "timed out" in err.lower()
