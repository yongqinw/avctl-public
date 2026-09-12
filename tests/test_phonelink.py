"""The island feeder's decisions, with APNs stubbed out.

What matters: an island is raised exactly when silence becomes music, fed
exactly when something the island shows has changed, ended exactly when the
music stops -- and a quiet rack costs zero pushes, because the progress
dates alone moving is not news (the island advances those itself).
"""

from __future__ import annotations

import pytest

from api import phonelink


def snap(state="playing", track="Aja", artist="Steely Dan", album="Aja",
         position=10.0, duration=480.0, volume=44, muted=False,
         pid="00DEC0DEDEADBEEF"):
    # volume/muted ride in the music fields: the island shows and nudges the
    # mini's system output, not the amp's knob.
    return {"devices": {
        "music": {"fields": {"state": state, "track": track, "artist": artist,
                             "album": album, "position": position,
                             "duration": duration, "volume": volume,
                             "muted": muted, "pid": pid}},
        "amp": {"fields": {"volume": 42, "muted": False}},
    }}


@pytest.fixture
def apns(monkeypatch, tmp_path):
    """Stubbed transport + a scratch token file. Yields the sent log."""
    sent: list[tuple[str, dict]] = []
    monkeypatch.setattr(phonelink, "_send",
                        lambda token, payload: (sent.append((token, payload)), 200)[1])
    monkeypatch.setattr(phonelink, "TOKENS_FILE", tmp_path / "phone.json")
    monkeypatch.setattr(phonelink, "PUSH_LOG", tmp_path / "push.log")
    monkeypatch.setattr(phonelink, "_last_ts", 0)
    monkeypatch.setattr(phonelink, "_config", lambda: {
        "team_id": "T", "key_id": "K", "key_file": "k.p8",
        "bundle_id": "org.avctl.test", "server": "https://mini"})
    return sent


def test_silence_becoming_music_starts_an_island(apns):
    phonelink.register("push_to_start", "aa" * 16)
    cs = phonelink.handle_snapshot(snap(), last_cs=None, now=1000.0)
    assert [p["aps"]["event"] for _, p in apns] == ["start"]
    aps = apns[0][1]["aps"]
    assert aps["attributes-type"] == "NowPlayingAttributes"
    assert aps["attributes"] == {"server": "https://mini"}
    assert cs["track"] == "Aja"
    # The mini's output, not the amp's 42.
    assert aps["content-state"]["volume"] == 44
    # The cover key rides along; the phone decides whether it can draw it.
    assert aps["content-state"]["artworkKey"] == "00DEC0DEDEADBEEF"
    # The island's volume-control style rides the state (config default c+).
    assert aps["content-state"]["volumeStyle"] == "c"
    # The two-clock rule: aps timestamp is unix, content-state dates are
    # Swift reference-epoch (unix minus 31 years).
    assert aps["timestamp"] == 1000
    assert aps["content-state"]["trackStart"] == pytest.approx(
        1000.0 - 10.0 - phonelink.REFERENCE_EPOCH)


def test_a_change_updates_a_live_island(apns):
    phonelink.register("activity", "bb" * 16, activity_id="act1")
    last = phonelink.content_state(snap())
    phonelink.handle_snapshot(snap(track="Peg", position=0.0), last_cs=last,
                              now=1000.0)
    assert [p["aps"]["event"] for _, p in apns] == ["update"]
    assert apns[0][1]["aps"]["content-state"]["track"] == "Peg"


def test_progress_alone_is_not_news(apns):
    phonelink.register("activity", "bb" * 16, activity_id="act1")
    last = phonelink.content_state(snap(position=10.0))
    phonelink.handle_snapshot(snap(position=15.0), last_cs=last, now=1000.0)
    assert apns == []


def test_stopping_ends_and_forgets_the_activity(apns):
    phonelink.register("activity", "bb" * 16, activity_id="act1")
    last = phonelink.content_state(snap())
    result = phonelink.handle_snapshot(snap(state="stopped"), last_cs=last,
                                       now=1000.0)
    assert [p["aps"]["event"] for _, p in apns] == ["end"]
    assert apns[0][1]["aps"]["dismissal-date"] == 1030
    assert result is None
    # The activity token is gone: a fresh play must push-to-start, not feed
    # a corpse.
    assert phonelink._load_tokens()["activities"] == {}


def test_an_island_already_up_is_not_restarted(apns):
    phonelink.register("push_to_start", "aa" * 16)
    phonelink.register("activity", "bb" * 16, activity_id="act1")
    last = phonelink.content_state(snap())
    phonelink.handle_snapshot(snap(), last_cs=last, now=1000.0)
    assert apns == []   # nothing changed, activity exists: total silence


def test_dead_activity_token_is_dropped(apns, monkeypatch):
    phonelink.register("activity", "bb" * 16, activity_id="act1")
    monkeypatch.setattr(phonelink, "_send", lambda token, payload: 410)
    last = phonelink.content_state(snap())
    phonelink.handle_snapshot(snap(track="Peg", position=0.0), last_cs=last,
                              now=1000.0)
    assert phonelink._load_tokens()["activities"] == {}


def test_two_pushes_in_one_second_get_distinct_timestamps(apns):
    """ActivityKit discards an update whose timestamp is not newer than the
    last applied -- whole-second stamps collide when a pause chases a volume
    nudge inside one second, and the pause silently vanishes."""
    phonelink.register("activity", "bb" * 16, activity_id="act1")
    last = phonelink.content_state(snap(), now=1000.0)
    last = phonelink.handle_snapshot(snap(volume=50), last_cs=last, now=1000.0)
    phonelink.handle_snapshot(snap(volume=50, state="paused"), last_cs=last,
                              now=1000.0)
    stamps = [p["aps"]["timestamp"] for _, p in apns]
    assert stamps == [1000, 1001]


def test_unknown_kind_is_refused():
    with pytest.raises(ValueError):
        phonelink.register("mystery", "aa")


def _age_activity(activity_id, hours, now=1000.0):
    # Absolute, on the TEST's clock: handle_snapshot receives now=1000.0,
    # so the birth time must be hours before THAT, not before wall time.
    tokens = phonelink._load_tokens()
    tokens["activities"][activity_id]["since"] = now - hours * 3600
    phonelink._save_tokens(tokens)


def test_old_activity_is_reissued_not_lost(apns):
    """#89: Apple evicts the island at ~8h; the feeder must end the old
    activity and push-to-start a fresh one while the music still plays."""
    phonelink.register("push_to_start", "aa" * 16)
    phonelink.register("activity", "bb" * 16, activity_id="act1")
    _age_activity("act1", hours=8)
    last = phonelink.content_state(snap())
    result = phonelink.handle_snapshot(snap(), last_cs=last, now=1000.0)
    assert [p["aps"]["event"] for _, p in apns] == ["end", "start"]
    assert apns[0][0] == "bb" * 16      # the old activity got the end
    assert apns[1][0] == "aa" * 16      # the fresh island rises by push
    assert result is not None           # the music, of course, plays on
    assert phonelink._load_tokens()["activities"] == {}


def test_young_activity_is_left_alone(apns):
    phonelink.register("push_to_start", "aa" * 16)
    phonelink.register("activity", "bb" * 16, activity_id="act1")
    _age_activity("act1", hours=2)
    last = phonelink.content_state(snap())
    phonelink.handle_snapshot(snap(), last_cs=last, now=1000.0)
    assert apns == []                   # nothing changed, nothing expired


def test_legacy_token_strings_migrate(apns):
    # A phone.json from before the birth-time field: bare token strings
    # must load as records stamped "born now", never crash or blink.
    phonelink._save_tokens({"push_to_start": [],
                            "activities": {"act1": "bb" * 16}})
    tokens = phonelink._load_tokens()
    assert tokens["activities"]["act1"]["token"] == "bb" * 16
    assert tokens["activities"]["act1"]["since"] > 0


def test_malformed_token_file_is_safely_rebuilt(apns):
    phonelink.TOKENS_FILE.write_text("[]", encoding="utf-8")

    phonelink.register("widget", "cc" * 16)

    assert phonelink._load_tokens() == {
        "push_to_start": [], "activities": {}, "widgets": ["cc" * 16],
    }


def test_widget_nudges_on_what_the_widget_shows(apns, monkeypatch):
    """#126: reloads are budgeted, so only changes the widget renders spend
    one -- track, play state, the rack strip. Volume nudges never do."""
    nudged: list[str] = []
    monkeypatch.setattr(phonelink, "_send_widget_reload",
                        lambda token: (nudged.append(token), 200)[1])
    monkeypatch.setattr(phonelink, "_widget_sig", None)
    phonelink.register("widget", "cc" * 16)
    cs = phonelink.handle_snapshot(snap(), last_cs=None, now=1000.0)
    assert len(nudged) == 1     # first look after boot paints once
    cs = phonelink.handle_snapshot(snap(volume=50), last_cs=cs, now=1010.0)
    assert len(nudged) == 1     # a volume nudge is not widget news
    cs = phonelink.handle_snapshot(snap(track="Peg", pid="AA" * 8),
                                   last_cs=cs, now=1020.0)
    assert len(nudged) == 2     # a track change is
    phonelink.handle_snapshot(snap(state="stopped"), last_cs=cs, now=1030.0)
    assert len(nudged) == 3     # so is the music stopping


def test_widget_410_prunes_the_token(apns, monkeypatch):
    monkeypatch.setattr(phonelink, "_send_widget_reload", lambda token: 410)
    monkeypatch.setattr(phonelink, "_widget_sig", None)
    phonelink.register("widget", "dd" * 16)
    phonelink.handle_snapshot(snap(), last_cs=None, now=1000.0)
    with phonelink._lock:
        assert phonelink._load_tokens()["widgets"] == []
