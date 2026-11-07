"""Providers without audio versions get no SUB/DUB selector (cue-plugins tests the anime one)."""

from __future__ import annotations

import httpx

from cue.app import CueApp
from cue.config import Config


async def test_generic_provider_has_no_audio_selector(media_dir, store, tmp_path):
    app = CueApp(
        Config(providers={"local": {"path": str(media_dir)}}),
        store=store,
        http=httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(404))),
        dropin_dir=tmp_path,
    )
    async with app.run_test() as pilot:
        provider = app.providers["local"]
        show = (await provider.search("Frieren"))[0]
        app.open_show("local", show)
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert not list(app.screen.query("#audio-mode"))
        assert app.audio_mode("local") is None
        assert app._playback_provider("local") is provider
        assert app.screen.check_action("toggle_audio", ()) is None
