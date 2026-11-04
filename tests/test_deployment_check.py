import json
from io import BytesIO
from urllib.error import URLError

import pytest

from app.main import healthz
from scripts import check_deployment as check


@pytest.mark.asyncio
async def test_health_exposes_render_revision_without_bot_configuration(monkeypatch):
    monkeypatch.setenv("RENDER_GIT_COMMIT", "merged-sha")
    assert await healthz() == {"status": "ok", "commit": "merged-sha"}
    monkeypatch.delenv("RENDER_GIT_COMMIT")
    assert await healthz() == {"status": "ok", "commit": "unknown"}


class Response(BytesIO):
    status = 200


@pytest.fixture
def http(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(check.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(check.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))
    calls = []

    def install(replies):
        sequence = iter(replies)

        def urlopen(request, timeout):
            calls.append((request, timeout))
            result = next(sequence)
            if isinstance(result, Exception):
                raise result
            if isinstance(result, dict):
                result = json.dumps(result).encode()
            return Response(result)

        monkeypatch.setattr(check, "urlopen", urlopen)
        return calls

    return install


@pytest.mark.parametrize("first", [
    {"status": "ok", "commit": "old"},
    {"status": "error", "commit": "new"},
    {"status": "ok"}, b"not json", b"[]", b"x" * 8193,
    URLError("temporary outage"), TimeoutError("cold start"),
])
def test_retries_until_expected_revision_is_healthy(http, first):
    calls = http([first, {"status": "ok", "commit": "new"}])
    check.wait_for_deployment("https://bot.example/", "new", timeout=40, interval=20)
    assert len(calls) == 2
    assert calls[0][0].full_url == "https://bot.example/healthz"
    assert calls[0][1] <= 15


def test_old_deployment_never_passes_and_timeout_is_bounded(http):
    calls = http([{"status": "ok", "commit": "old"}] * 2)
    with pytest.raises(TimeoutError, match="new"):
        check.wait_for_deployment("https://bot.example", "new", timeout=30, interval=20)
    assert len(calls) == 2


@pytest.mark.parametrize("url", ["", "http://bot.example", "https://user:password@bot.example", "https://bot.example/path", "https://bot.example?token=secret"])
def test_invalid_configuration_fails_before_http(http, url):
    calls = http([])
    with pytest.raises(ValueError, match="BOT_SERVICE_URL"):
        check.wait_for_deployment(url, "new")
    assert calls == []


def test_cli_reports_failure_and_nonzero_exit(monkeypatch, capsys):
    monkeypatch.setenv("BOT_SERVICE_URL", "")
    monkeypatch.setenv("EXPECTED_COMMIT", "new")
    assert check.main() == 1
    assert "::error::" in capsys.readouterr().err
