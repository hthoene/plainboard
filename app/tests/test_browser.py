import asyncio

import pytest
from conftest import wait_for
from starlette.websockets import WebSocketDisconnect


def test_browser_window_is_passed_on_to_the_runner_for_the_signed_in_user(client, project, monkeypatch):
    from plainboard.orchestration import runners as runners_module

    sent, asked = [], []

    class Upstream:
        """The runner's side. It ends by itself once it got what the test sends, so the relay is
        over before the test leaves the socket: leaving a relay that still runs races with the test
        client's own shutdown (seen as a CancelledError now and then)."""

        def __init__(self):
            self.got_input = asyncio.Event()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def send(self, message):
            sent.append(message)
            self.got_input.set()

        async def __aiter__(self):
            yield '{"t": "state", "url": ""}'
            yield b"\xff\xd8frame"
            await self.got_input.wait()

    async def connect(url, **_):
        asked.append(url)
        return Upstream()

    monkeypatch.setattr(runners_module, "connect", connect)
    with client.websocket_connect(f"/api/projects/{project['id']}/browser") as ws:
        assert ws.receive_json() == {"t": "state", "url": ""}
        assert ws.receive_bytes() == b"\xff\xd8frame"
        ws.send_text('{"t": "nav", "url": "5173"}')
        wait_for(lambda: sent == ['{"t": "nav", "url": "5173"}'])
    assert asked == [f"ws://runner/browser/project-{project['id']}"]  # one browser per project

    with pytest.raises(WebSocketDisconnect) as gone:
        with client.websocket_connect("/api/projects/999/browser"):
            pass
    assert gone.value.code == 4404

    # It is a browser inside the agents' environment: never without the login, shared previews or not.
    client.cookies.clear()
    with pytest.raises(WebSocketDisconnect) as refused:
        with client.websocket_connect(f"/api/projects/{project['id']}/browser"):
            pass
    assert refused.value.code == 4401 and len(asked) == 1
