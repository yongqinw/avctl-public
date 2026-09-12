"""Roon projections share one capability-driven Core and preserve sources."""

from __future__ import annotations

import threading
import time

import pytest

from api import agentlink, musiclink
from devices import registry
from devices.amp.roon import RoonAmp
from devices.dac.roon import RoonDac
from devices.music.roon import RoonMusic
from devices.music.virtual_library import VirtualMusicLibrary
from devices.roon.contracts import RoonCapabilityError, RoonError
from devices.roon.factory import controller_from_config, reset
from devices.roon.live import PyRoonController, _extension_id
from devices.roon.mock import MockRoonController


@pytest.fixture(autouse=True)
def fresh_roon():
    reset()
    registry.reset()
    yield
    reset()
    registry.reset()


def test_roon_extension_identity_can_be_migrated_without_source_identity(
    monkeypatch, tmp_path,
):
    identity = tmp_path / "roon-extension-id"
    identity.write_text("org.example.existing-roon\n", encoding="utf-8")
    monkeypatch.delenv("AVCTL_ROON_EXTENSION_ID", raising=False)
    monkeypatch.setenv("AVCTL_ROON_EXTENSION_ID_FILE", str(identity))
    assert _extension_id() == "org.example.existing-roon"

    monkeypatch.setenv("AVCTL_ROON_EXTENSION_ID", "org.example.environment")
    assert _extension_id() == "org.example.environment"


def test_mock_core_models_library_qobuz_and_a_long_queue():
    core = MockRoonController()

    assert {item.source for item in core.search("Mock Track")} == {
        "library", "qobuz",
    }
    assert len(core.queue("living-room")) == 36
    assert all(item.source == "library" for item in core.library())


def test_roon_music_queues_qobuz_without_mutating_library():
    core = MockRoonController()
    music = RoonMusic(core, "living-room")
    qobuz = [item for item in core.search("Qobuz Artist")][:3]
    library_before = [item.id for item in core.library()]

    music.play_catalog([item.id for item in qobuz])

    assert [item.id for item in core.queue("living-room")] == [
        item.id for item in qobuz
    ]
    assert [item.id for item in core.library()] == library_before
    assert music.now_playing()["source"] == "qobuz"


def test_roon_music_implements_library_service_and_explore_contracts():
    core = MockRoonController()
    music = RoonMusic(core, "living-room", "roon-ready")

    assert music.service_info()["name"] == "Roon / Qobuz"
    assert music.service_info()["can_stream_service"] is True
    assert music.service_info()["can_add_to_library"] is False
    assert music.search_library("Mock Track 1")
    recent = music.recently_added()
    assert recent
    assert all(row["album"] != "" for row in recent)
    assert music.playlists()[0]["name"] == "Happy Mix"

    albums = music.search_service_albums("Qobuz Artist")
    assert albums and albums[0]["id"].startswith("mock:album:")
    detail = music.service_album(albums[0]["id"])
    assert detail["tracks"]
    assert all(row.get("catalog_id") for row in detail["tracks"])
    assert music.service_tracks("album", albums[0]["id"])
    assert music.explore_service()["sections"]


def test_generic_queue_can_project_mixed_library_and_service_into_roon(
        monkeypatch, tmp_path):
    core = MockRoonController()
    music = RoonMusic(core, "living-room", "roon-ready")
    local = core.library()[0]
    service = next(row for row in core.search("Qobuz Artist")
                   if row.source == "qobuz")
    queue = musiclink.QueueController(tmp_path / "queue.json")
    monkeypatch.setattr(musiclink, "_MUSIC", music)
    monkeypatch.setattr(musiclink, "_QUEUE", queue)
    monkeypatch.setattr(musiclink, "_start_pump_locked", lambda _revision: None)

    musiclink._dispatch([
        local.panel_dict(),
        {"catalog_id": service.id, "name": service.title,
         "artist": service.artist, "album": service.album},
    ], replace=True, play=True)
    with queue.lock:
        assert musiclink._materialize_locked(queue, 10) is True

    assert [row.id for row in core.queue("living-room")] == [
        local.id, service.id,
    ]
    assert queue.materialized == 2

    queue.observe(music.now_playing())
    musiclink.next_track({})
    assert music.now_playing()["pid"] == service.id


def test_generic_music_commands_play_and_queue_roon_service_items(
        monkeypatch, tmp_path):
    core = MockRoonController()
    music = RoonMusic(core, "living-room", "roon-ready")
    songs = music.search_service("Qobuz Artist", ["songs"], 3)
    albums = music.search_service_albums("Qobuz Artist", 3)
    queue = musiclink.QueueController(tmp_path / "queue.json")
    monkeypatch.setattr(musiclink, "_MUSIC", music)
    monkeypatch.setattr(musiclink, "_QUEUE", queue)
    monkeypatch.setattr(musiclink, "_start_pump_locked", lambda _revision: None)

    played = musiclink.play_service({
        "kind": "song", "id": songs[0]["id"],
        "name": songs[0]["name"], "artist": songs[0]["artist"],
    })
    queued = musiclink.queue_service({
        "kind": "album", "id": albums[0]["id"],
        "name": albums[0]["album"],
    })

    assert played["message"].startswith("playing ")
    assert queued["message"].startswith("queued ")
    assert core.queue("living-room")[0].source == "qobuz"
    assert len(core.queue("living-room")) > 1


def test_queue_reconciles_roon_ephemeral_now_playing_id_by_metadata(tmp_path):
    queue = musiclink.QueueController(tmp_path / "queue.json")
    items = musiclink._queue_items([
        {"pid": "0000000000000001", "name": "So What",
         "artist": "Miles Davis", "album": "Kind of Blue"},
        {"pid": "0000000000000002", "name": "Freddie Freeloader",
         "artist": "Miles Davis", "album": "Kind of Blue"},
    ])
    with queue.lock:
        queue.replace_locked(items, 2)

    queue.observe({"state": "playing", "pid": "roon-now:zone:8",
                   "track": "So What", "artist": "Miles Davis",
                   "album": "Kind of Blue"})

    assert queue.current == 0
    assert queue.playback_ids({
        "state": "playing", "pid": "roon-now:zone:9",
        "track": "So What", "artist": "Miles Davis",
        "album": "Kind of Blue",
    }) == ("0000000000000001", None)


def test_music_transport_and_shuffle_change_one_zone_revision():
    core = MockRoonController()
    music = RoonMusic(core, "living-room")
    before = music.now_playing()["revision"]

    music.next_track()
    music.set_shuffle(True)

    state = music.now_playing()
    assert state["name"] == "Mock Track 2"
    assert state["track"] == "Mock Track 2"
    assert state["shuffle"] is True
    assert state["revision"] == before + 2


def test_roon_playlist_from_position_drops_earlier_tracks():
    core = MockRoonController()
    music = RoonMusic(core, "living-room")
    playlist = core.playlist("mock-playlist-happy")
    assert playlist is not None

    music.queue_playlist_bulk(
        "mock-playlist-happy", 3, replace=True, play=True)

    expected = [row["pid"] for row in playlist["tracks"]][2:]
    assert [row.id for row in core.queue("living-room")] == expected
    assert music.now_playing()["pid"] == expected[0]


def test_roon_amp_maps_confirmed_output_state_and_controls():
    core = MockRoonController()
    amp = RoonAmp(core, "roon-ready")

    assert amp.state() == {"PWR": 1, "VOL": 42, "MUT": 0, "INP": 1}
    assert amp.set_volume(55) == 55
    assert amp.toggle_mute() is True
    assert amp.set_input(2) is True
    assert amp.power_off() is True
    assert amp.state() == {"PWR": 0, "VOL": 55, "MUT": 1, "INP": 2}


def test_fixed_and_incremental_outputs_refuse_unsafe_absolute_volume():
    core = MockRoonController()

    with pytest.raises(RoonCapabilityError, match="volume.set"):
        core.set_volume("usb-dac", 50)
    with pytest.raises(RoonCapabilityError, match="readback"):
        core.step_volume("incremental", 1)


def test_roon_dac_only_projects_capabilities_the_output_reports():
    core = MockRoonController()
    dac = RoonDac(core, "roon-ready")

    assert dac.inputs == ["roon", "usb", "optical"]
    assert dac.select("usb") == 1
    assert dac.current == "usb"
    assert dac.power_to(False) is True
    assert dac.power is False

    fixed = RoonDac(core, "usb-dac")
    assert fixed.inputs == [] and fixed.power is None
    with pytest.raises(RoonCapabilityError, match="input.select"):
        fixed.select("usb")


def test_all_roon_driver_projections_share_one_configured_core():
    config = {
        "roon": {"mode": "mock", "zone_id": "living-room",
                 "output_id": "roon-ready"},
        "music": {"driver": "RoonMusic"},
        "dac": {"driver": "RoonDac"},
        "amp": {"driver": "RoonAmp"},
    }
    music = RoonMusic.from_config(config)
    dac = RoonDac.from_config(config)
    amp = RoonAmp.from_config(config)

    assert music.controller is dac.controller is amp.controller
    assert music.controller is controller_from_config(config)


def test_roon_disconnect_grace_is_configurable():
    config = {
        "roon": {"mode": "mock", "zone_id": "living-room",
                 "output_id": "roon-ready"},
        "music": {"driver": "RoonMusic",
                  "RoonMusic": {"disconnect_grace_seconds": 23}},
    }

    music = RoonMusic.from_config(config)

    assert music.stopped_queue_rescue_delay() == 23


def test_roon_disconnect_grace_covers_one_socket_reconnect_cycle():
    music = RoonMusic(MockRoonController(), "living-room")

    assert music.stopped_queue_rescue_delay() == 30


def test_roon_music_can_scope_an_idle_output_before_a_zone_exists():
    config = {
        "roon": {"mode": "mock", "output_id": "roon-ready"},
        "music": {"driver": "RoonMusic"},
    }

    music = RoonMusic.from_config(config)

    assert music.zone_id == "roon-ready"
    assert music.output_id == "roon-ready"
    assert music.now_playing()["state"] == "playing"
    music.next_track()
    assert music.now_playing()["track"] == "Mock Track 2"


def test_registry_discovers_roon_compatibility_drivers(monkeypatch):
    config = {
        "roon": {"mode": "mock", "zone_id": "living-room",
                 "output_id": "roon-ready"},
        "music": {"driver": "RoonMusic"},
        "dac": {"driver": "RoonDac"},
        "amp": {"driver": "RoonAmp"},
    }
    from devices import config as device_config
    monkeypatch.setattr(device_config, "load_config", lambda: config)

    assert registry.driver_class("music") is RoonMusic
    assert registry.driver_class("dac") is RoonDac
    assert registry.driver_class("amp") is RoonAmp


class FakeDiscovery:
    def __init__(self, core_id):
        self.core_id = core_id

    def all(self):
        return [("roon.test", "9330")]

    def stop(self):
        pass


class FakeRoonApi:
    def __init__(self, appinfo, token, host, port, blocking_init=False):
        assert token == "synthetic-token"
        assert (host, port, blocking_init) == ("roon.test", 9330, False)
        self.ready = True
        self.zones = {}
        self.outputs = {
            "system": {
                "output_id": "system",
                "zone_id": "idle-zone",
                "display_name": "System Output",
                "volume": {
                    "type": "number", "min": 0, "max": 100,
                    "value": 100, "step": 1, "is_muted": False,
                    "hard_limit_min": 0, "hard_limit_max": 100,
                    "soft_limit": 90,
                },
                "source_controls": [{
                    "control_key": "1", "display_name": "System Output",
                    "supports_standby": False, "status": "indeterminate",
                }],
            }
        }
        self.volume_calls = []
        self.transport_calls = []
        self.action_calls = []
        self._context = "root"
        self._query = ""
        self.queue_callback = None
        self.queue_registrations = 0
        self.search_inputs = 0
        self.browse_calls = 0
        self.stopped = False

    def register_state_callback(self, callback, **kwargs):
        self.callback = callback

    def register_queue_callback(self, callback, target):
        self.queue_callback = callback
        self.queue_target = target
        self.queue_registrations += 1

    def playback_control(self, target, control):
        self.transport_calls.append((target, control))
        return {"ok": True}

    def set_volume_percent(self, output_id, value):
        self.volume_calls.append((output_id, value))
        self.outputs[output_id]["volume"]["value"] = value
        return {"ok": True}

    def mute(self, output_id, muted):
        self.outputs[output_id]["volume"]["is_muted"] = muted
        return {"ok": True}

    def browse_browse(self, opts):
        self.browse_calls += 1
        if opts.get("pop_all"):
            self._context = "root"
        elif opts.get("pop_levels"):
            self._context = "search-results"
        elif opts.get("input") is not None:
            requested = str(opts["input"])
            # Model the real Roon failure that caused Ask to relabel an old
            # Miles Davis result as a new Chinese query: submitting a query
            # from the previous result context can leave that list unchanged
            # when the new query has no matches. A fresh Search context must
            # be opened before every input.
            if not (requested == "No Match" and
                    self._context == "search-results"):
                self._query = requested
                self._context = "search-results"
            self.search_inputs += 1
        else:
            key = opts.get("item_key")
            transitions = {
                "library": "library", "search": "search",
                "tracks": "tracks", "albums": "albums",
                "playlists": "playlists",
                "track-1": "track-1-actions",
                "track-2": "track-2-actions",
                "album-1": "album-1-tracks",
            }
            if key in {"play-now", "queue"}:
                self.action_calls.append(key)
            elif key in transitions:
                self._context = transitions[key]
        return {"list": {"count": len(self._current_rows())}}

    def _current_rows(self):
        if self._context == "root":
            return [{"title": "Library", "hint": "list",
                     "item_key": "library"}]
        if self._context == "library":
            return [
                {"title": "Search", "hint": "list", "item_key": "search"},
                {"title": "Tracks", "hint": "list", "item_key": "tracks"},
                {"title": "Albums", "hint": "list", "item_key": "albums"},
            ]
        if self._context == "search":
            return [{"title": "No Results"}]
        if self._context == "search-results":
            if self._query == "No Match":
                return [{"title": "No Results"}]
            return [
                {"title": "Tracks", "subtitle": "2 Results",
                 "hint": "list", "item_key": "tracks"},
                {"title": "Albums", "subtitle": "0 Results",
                 "hint": "list", "item_key": "albums"},
                {"title": "Playlists", "subtitle": "1 Result",
                 "hint": "list", "item_key": "playlists"},
            ]
        if self._context == "tracks":
            if self._query == "Jay Chou":
                return [
                    {"title": "Rice Field", "subtitle": "Jay Chou",
                     "hint": "action_list", "item_key": "track-1"},
                ]
            return [
                {"title": "So What", "subtitle": "Miles Davis",
                 "hint": "action_list", "item_key": "track-1",
                 "image_key": "image-1"},
                {"title": "Blue in Green", "subtitle": "Miles Davis",
                 "hint": "action_list", "item_key": "track-2"},
            ]
        if self._context == "albums":
            return [{"title": "Kind of Blue", "subtitle": "Miles Davis",
                     "hint": "list", "item_key": "album-1"}]
        if self._context == "playlists":
            return [{"title": "Jazz Classics", "subtitle": "Qobuz",
                     "hint": "list", "item_key": "playlist-1"}]
        if self._context == "album-1-tracks":
            return [
                {"title": "So What", "subtitle": "Miles Davis",
                 "hint": "action_list", "item_key": "track-1",
                 "image_key": "image-1"},
                {"title": "Blue in Green", "subtitle": "Miles Davis",
                 "hint": "action_list", "item_key": "track-2"},
            ]
        if self._context.startswith("track-"):
            return [
                {"title": "Play Now", "hint": "action",
                 "item_key": "play-now"},
                {"title": "Queue", "hint": "action", "item_key": "queue"},
            ]
        return []

    def browse_load(self, opts):
        rows = self._current_rows()
        offset = int(opts.get("offset") or 0)
        count = int(opts.get("count") or len(rows))
        return {"items": rows[offset:offset + count], "offset": offset,
                "list": {"count": len(rows)}}

    def shuffle(self, target, enabled):
        return {"ok": True}

    def repeat(self, target, mode):
        return {"ok": True}

    def get_image(self, image_key):
        return f"https://example.test/{image_key}"

    def stop(self):
        self.stopped = True


@pytest.fixture
def live_controller(tmp_path):
    token = tmp_path / "roon-token"
    token.write_text("synthetic-token", encoding="utf-8")
    controller = PyRoonController(
        token_file=token,
        selected_output="system",
        max_volume=70,
        api_factory=FakeRoonApi,
        discovery_factory=FakeDiscovery,
    )
    yield controller
    controller.close()


def test_live_adapter_normalizes_idle_output_and_enforces_stricter_cap(
        live_controller):
    output = live_controller.output("system")

    assert output.volume.value == 100
    assert output.volume.safety_max == 70
    assert output.standby is None
    assert "power.standby" not in output.capabilities
    assert live_controller.set_volume("system", 99) == 70
    assert live_controller.output("system").volume.value == 70


def test_live_adapter_synthesizes_stopped_zone_for_idle_output(live_controller):
    zone = live_controller.zone("system")

    assert zone.state == "stopped"
    assert zone.output_ids == ("system",)
    live_controller.transport("system", "play")


def test_live_search_replays_browse_context_and_returns_opaque_refs(
        live_controller):
    rows = live_controller.search("Miles Davis")

    assert [(row.title, row.artist) for row in rows] == [
        ("So What", "Miles Davis"),
        ("Blue in Green", "Miles Davis"),
    ]
    assert all(row.id.startswith("roon-track:") for row in rows)


def test_live_library_albums_preserve_real_album_metadata(live_controller):
    rows = live_controller.library_albums()

    assert [(row.title, row.artist, row.kind) for row in rows] == [
        ("Kind of Blue", "Miles Davis", "album"),
    ]


def test_live_library_snapshot_survives_restart_without_browsing(tmp_path):
    token = tmp_path / "roon-token"
    token.write_text("synthetic-token", encoding="utf-8")
    snapshot = tmp_path / "roon-snapshot.json"
    first = PyRoonController(
        token_file=token, selected_output="system", snapshot_file=snapshot,
        api_factory=FakeRoonApi, discovery_factory=FakeDiscovery,
    )
    try:
        assert first.library_albums()[0].title == "Kind of Blue"
        assert [row.title for row in first.album("Kind of Blue")] == [
            "So What", "Blue in Green",
        ]
        assert snapshot.exists()
    finally:
        first.close()

    second = PyRoonController(
        token_file=token, selected_output="system", snapshot_file=snapshot,
        api_factory=FakeRoonApi, discovery_factory=FakeDiscovery,
    )
    try:
        api = second._invoke("test.inspect", lambda client: client)
        assert api.browse_calls == 0
        assert second.library_albums()[0].title == "Kind of Blue"
        assert [row.title for row in second.album("Kind of Blue")] == [
            "So What", "Blue in Green",
        ]
        assert api.browse_calls == 0
    finally:
        second.close()


def test_slow_artwork_download_does_not_occupy_roon_worker(
        live_controller, monkeypatch, tmp_path):
    downloading = threading.Event()
    release = threading.Event()

    class Response:
        content = b"image"

        @staticmethod
        def raise_for_status():
            return None

    def slow_get(_url, timeout):
        assert timeout == live_controller.timeout
        downloading.set()
        assert release.wait(2)
        return Response()

    monkeypatch.setattr("devices.roon.live.requests.get", slow_get)
    worker = threading.Thread(
        target=live_controller.artwork,
        args=("image-1", tmp_path / "cover.img"),
    )
    worker.start()
    assert downloading.wait(1)
    started = time.monotonic()
    live_controller.transport("system", "next")
    elapsed = time.monotonic() - started
    release.set()
    worker.join(timeout=2)

    assert elapsed < 0.5
    assert not worker.is_alive()


def test_transport_jumps_between_long_queue_placements(
        live_controller, monkeypatch):
    rows = live_controller.search("Miles Davis")
    api = live_controller._invoke("test.inspect", lambda client: client)
    timeline = []
    first_action = threading.Event()
    release = threading.Event()
    original_action = live_controller._action
    original_transport = api.playback_control

    def blocking_action(client, locator, wanted):
        if not first_action.is_set():
            first_action.set()
            assert release.wait(2)
        original_action(client, locator, wanted)
        timeline.append("play" if "Play Now" in wanted else "queue")

    def record_transport(target, control):
        timeline.append(control)
        return original_transport(target, control)

    monkeypatch.setattr(live_controller, "_action", blocking_action)
    monkeypatch.setattr(api, "playback_control", record_transport)
    placing = threading.Thread(
        target=live_controller.replace_queue,
        args=("system", [row.id for row in rows]),
    )
    placing.start()
    assert first_action.wait(1)
    skipping = threading.Thread(
        target=live_controller.transport, args=("system", "next"))
    skipping.start()
    deadline = time.monotonic() + 1
    while live_controller._commands.qsize() < 1 and time.monotonic() < deadline:
        time.sleep(0.01)
    release.set()
    placing.join(timeout=3)
    skipping.join(timeout=3)

    assert not placing.is_alive() and not skipping.is_alive()
    assert timeline == ["play", "next", "queue"]


def test_live_now_playing_id_changes_with_media_not_state_revision(
        live_controller):
    api = live_controller._invoke("test.inspect", lambda client: client)
    api.outputs["system"]["zone_id"] = "zone-1"
    api.zones["zone-1"] = {
        "zone_id": "zone-1",
        "display_name": "Office",
        "state": "playing",
        "outputs": [{"output_id": "system"}],
        "now_playing": {
            "three_line": {
                "line1": "So What", "line2": "Miles Davis",
                "line3": "Kind of Blue",
            },
            "length": 545,
            "image_key": "image-kind-of-blue",
        },
    }
    live_controller._capture(api)
    first = live_controller.zone("system").now_playing
    assert first is not None

    live_controller._capture(api)
    second = live_controller.zone("system").now_playing
    assert second is not None
    assert second.id == first.id

    api.zones["zone-1"]["now_playing"]["three_line"]["line1"] = "All Blues"
    live_controller._capture(api)
    third = live_controller.zone("system").now_playing
    assert third is not None
    assert third.id != first.id


def test_live_mixed_service_search_preserves_kind_diversity(live_controller):
    rows = live_controller.search_service(
        "Miles Davis", ["songs", "albums", "playlists"], 3)

    assert [row.kind for row in rows] == ["track", "album", "playlist"]
    api = live_controller._invoke("test.inspect", lambda client: client)
    assert api.search_inputs == 1


def test_live_service_search_and_album_details_use_short_bounded_caches(
        live_controller):
    albums = live_controller.search_service("Miles Davis", ["albums"], 3)
    api = live_controller._invoke("test.inspect", lambda client: client)
    inputs = api.search_inputs

    repeated = live_controller.search_service("Miles Davis", ["albums"], 3)
    assert [row.id for row in repeated] == [row.id for row in albums]
    assert api.search_inputs == inputs

    detail = live_controller.service_album(albums[0].id)
    assert [row["name"] for row in detail["tracks"]] == [
        "So What", "Blue in Green",
    ]
    calls = api.browse_calls
    repeated_detail = live_controller.service_album(albums[0].id)
    assert repeated_detail == detail
    assert api.browse_calls == calls


def test_live_service_search_many_deduplicates_and_caches_terms(
        live_controller):
    rows = live_controller.search_service_many(
        ["Miles Davis", "John Coltrane", "Miles Davis"], ["songs"], 2)
    api = live_controller._invoke("test.inspect", lambda client: client)

    assert [[item.title for item in group] for group in rows] == [
        ["So What", "Blue in Green"],
        ["So What", "Blue in Green"],
        ["So What", "Blue in Green"],
    ]
    assert api.search_inputs == 2
    live_controller.search_service_many(
        ["Miles Davis", "John Coltrane"], ["songs"], 2)
    assert api.search_inputs == 2


def test_live_service_search_many_never_leaks_previous_query_results(
        live_controller):
    rows = live_controller.search_service_many(
        ["Miles Davis", "No Match", "Jay Chou"], ["songs"], 2)

    assert [[item.title for item in group] for group in rows] == [
        ["So What", "Blue in Green"],
        [],
        ["Rice Field"],
    ]


def test_roon_ask_curation_uses_one_batched_provider_call(monkeypatch):
    calls = []
    monkeypatch.setattr(musiclink, "recent_songs", lambda *args, **kwargs: [])
    monkeypatch.setattr(musiclink, "service_info", lambda: {
        "name": "Roon / Qobuz", "source": "roon", "batched_search": True,
    })
    monkeypatch.setattr(musiclink, "search_catalog", lambda *_args, **_kwargs:
                        (_ for _ in ()).throw(AssertionError("single search used")))

    def search_many(terms, kinds, limit):
        calls.append((terms, kinds, limit))
        return [[{
            "id": f"qobuz:{index}", "name": title,
            "artist": artist, "album": "Test Album", "source": "roon",
        }] for index, (title, artist) in enumerate(
            (("So What", "Miles Davis"),
             ("Blue Train", "John Coltrane")))]

    monkeypatch.setattr(musiclink, "search_catalog_many", search_many)

    result = agentlink._curate_music({
        "source": "service", "mode": "inspect", "exclude_played": False,
        "description": "Jazz", "candidates": [
            {"title": "So What", "artist": "Miles Davis"},
            {"title": "Blue Train", "artist": "John Coltrane"},
        ],
    })

    assert calls == [(["So What Miles Davis", "Blue Train John Coltrane"],
                      ["songs"], 3)]
    assert [row["name"] for row in result["matches"]] == [
        "So What", "Blue Train",
    ]


def test_roon_ask_curation_groups_titles_by_artist(monkeypatch):
    calls = []
    monkeypatch.setattr(musiclink, "recent_songs", lambda *args, **kwargs: [])
    monkeypatch.setattr(musiclink, "service_info", lambda: {
        "name": "Roon / Qobuz", "source": "roon", "batched_search": True,
    })

    def search_many(terms, kinds, limit):
        calls.append((terms, kinds, limit))
        assert terms == ["Jay Chou"]
        return [[
            {"id": "qobuz:1", "name": "晴天", "artist": "Jay Chou"},
            {"id": "qobuz:2", "name": "七里香", "artist": "Jay Chou"},
            {"id": "qobuz:3", "name": "青花瓷", "artist": "Jay Chou"},
        ]]

    monkeypatch.setattr(musiclink, "search_catalog_many", search_many)

    result = agentlink._curate_music({
        "source": "service", "mode": "inspect", "exclude_played": False,
        "description": "Jay Chou", "candidates": [
            {"title": "晴天", "artist": "Jay Chou"},
            {"title": "七里香", "artist": "Jay Chou"},
            {"title": "青花瓷", "artist": "Jay Chou"},
        ],
    })

    assert calls == [(["Jay Chou"], ["songs"], 30)]
    assert [row["name"] for row in result["matches"]] == [
        "晴天", "七里香", "青花瓷",
    ]


def test_roon_capabilities_are_explicit_and_prompt_is_provider_neutral(
        tmp_path):
    info = RoonMusic(
        MockRoonController(), "living-room", "roon-ready",
        VirtualMusicLibrary(tmp_path / "library.sqlite3"),
    ).service_info()

    assert info["can_edit_playlists"] is True
    assert info["supports_date_added"] == "virtual_library"
    assert info["supports_play_history"] == "observed_playback"
    assert info["batched_search"] is True
    assert "Music.app dateAdded" not in agentlink.STATIC_SYSTEM_PROMPT
    assert "Music.app user playlist" not in agentlink.STATIC_SYSTEM_PROMPT


def test_roon_observes_external_playback_without_adding_to_library(tmp_path):
    core = MockRoonController()
    library = VirtualMusicLibrary(tmp_path / "history.sqlite3")
    music = RoonMusic(core, "living-room", "roon-ready", library)

    music.now_playing()
    core.transport("living-room", "next")
    music.now_playing()

    history = music.personal_history()
    assert [row["name"] for row in history] == ["Mock Track 2", "Mock Track 1"]
    assert history[0]["catalog_id"] == "mock:qobuz:01"
    assert history[1]["pid"] == "mock:library:00"
    assert all(row["plays"] == 1 for row in history)
    assert library.tracks() == []
    assert library.albums() == []


def test_live_adapter_replays_refs_for_replace_and_append(live_controller):
    rows = live_controller.search("Miles Davis")

    live_controller.replace_queue("system", [row.id for row in rows])
    live_controller.append_queue("system", [rows[0].id])

    api = live_controller._invoke("test.inspect", lambda client: client)
    assert api.action_calls == ["play-now", "queue", "queue"]


def test_live_queue_subscription_is_projected_and_clear_is_honest(
        live_controller):
    api = live_controller._invoke("test.inspect", lambda client: client)
    api.queue_callback({"items": [{
        "queue_item_id": 9,
        "three_line": {"line1": "So What", "line2": "Miles Davis",
                       "line3": "Kind of Blue"},
    }]})

    assert [row.title for row in live_controller.queue("system")] == ["So What"]
    assert live_controller.clear_queue("system") == 1
    assert live_controller.queue("system") == []
    assert ("system", "stop") in api.transport_calls


def test_timed_out_command_does_not_kill_the_serial_worker(live_controller):
    live_controller.timeout = 0.5

    with pytest.raises(RoonError, match="timed out"):
        live_controller._invoke("slow", lambda _api: time.sleep(0.7))

    time.sleep(0.3)
    assert live_controller._invoke("after-timeout", lambda _api: "alive") == "alive"


def test_queue_subscription_is_restored_after_pyroon_reconnect(live_controller):
    api = live_controller._invoke("test.inspect", lambda client: client)
    assert api.queue_registrations == 1

    api.ready = False
    time.sleep(0.35)
    api.ready = True
    deadline = time.monotonic() + 2
    while api.queue_registrations < 2 and time.monotonic() < deadline:
        time.sleep(0.05)

    assert api.queue_registrations == 2


def test_commands_wait_for_reconnected_socket_not_stale_ready_flag(
        live_controller):
    api = live_controller._invoke("test.inspect", lambda client: client)
    api._roonsocket = type("Socket", (), {"connected": False})()
    timer = threading.Timer(
        0.2, lambda: setattr(api._roonsocket, "connected", True))
    timer.start()
    try:
        started = time.monotonic()
        answer = live_controller._invoke(
            "during-reconnect", lambda _client: "connected")
    finally:
        timer.cancel()

    assert answer == "connected"
    assert time.monotonic() - started >= 0.15


def test_live_adapter_rebuilds_client_after_hard_disconnect(tmp_path):
    token = tmp_path / "roon-token"
    token.write_text("synthetic-token", encoding="utf-8")
    clients = []

    def factory(*args, **kwargs):
        client = FakeRoonApi(*args, **kwargs)
        clients.append(client)
        return client

    controller = PyRoonController(
        token_file=token,
        selected_output="system",
        timeout=2,
        reconnect_after=0.1,
        api_factory=factory,
        discovery_factory=FakeDiscovery,
    )
    try:
        first = controller._invoke("inspect.first", lambda client: client)
        first.ready = False

        assert controller._invoke(
            "after-hard-disconnect", lambda client: client) is clients[1]
        assert len(clients) == 2
        assert first.stopped is True
        assert clients[1].queue_registrations == 1
    finally:
        controller.close()


def test_live_adapter_retries_a_failed_client_rebuild(tmp_path):
    token = tmp_path / "roon-token"
    token.write_text("synthetic-token", encoding="utf-8")
    clients = []
    attempts = 0

    def factory(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            raise OSError("Core is still restarting")
        client = FakeRoonApi(*args, **kwargs)
        clients.append(client)
        return client

    controller = PyRoonController(
        token_file=token,
        selected_output="system",
        timeout=3,
        reconnect_after=0.1,
        api_factory=factory,
        discovery_factory=FakeDiscovery,
    )
    try:
        first = controller._invoke("inspect.first", lambda client: client)
        first.ready = False

        recovered = controller._invoke(
            "after-retry", lambda client: client)

        assert attempts == 3
        assert recovered is clients[1]
        assert controller._thread.is_alive()
    finally:
        controller.close()
