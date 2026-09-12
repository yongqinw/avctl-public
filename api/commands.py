"""Every button in the app, in one list.

The UI is being built before the device modules exist, so almost nothing here
does anything yet. That is deliberate: the buttons are laid out first, and each
one becomes real by attaching a handler *here* -- no HTML changes, no new
routes. Until then `POST /api/cmd` answers 501 and the phone says "coming soon".

Two rules keep the two halves honest with each other:

  * views.py builds every button through `key()`, which raises if the id is not
    in this table. A button that cannot be dispatched cannot be rendered.
  * `implemented()` is sent to the browser, which dims everything not in it. So
    the "coming soon" marks are derived from the code, not maintained by hand,
    and a button stops being dimmed the moment its handler lands.

Ids are `device.group.action`, because the first segment is what decides which
module will eventually own the call.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable

from . import amplink, daclink, musiclink, tvlink

Handler = Callable[[dict[str, Any]], dict[str, Any]]


class SceneBusyError(RuntimeError):
    """A scene is already driving the rack. The route answers 409."""


# Scenes are whole-rack transactions: Off arriving in the middle of a Music
# wake powers the amp down while the TV is still coming up, and the rack
# lands half-lit claiming both succeeded. So exactly one scene runs at a
# time, and a second press is TOLD to wait rather than silently queued --
# queued, the user would let go of the room believing Off happened, when it
# is actually parked behind up to 45s of TV wake.
_SCENE_LOCK = threading.Lock()


def _one_scene_at_a_time(handler: Handler) -> Handler:
    def serialized(args: dict[str, Any]) -> dict[str, Any]:
        if not _SCENE_LOCK.acquire(blocking=False):
            raise SceneBusyError(
                "a scene is already running -- give it a moment to finish")
        try:
            return handler(args)
        finally:
            _SCENE_LOCK.release()
    return serialized


# --- handlers that are not simple tvlink calls ---------------------------


def _rack(*jobs: tuple[str, Handler], stagger: float = 0.0) -> dict[str, Any]:
    """Run device handlers side by side and answer for all of them at once.

    Concurrent because the slowest member sets the wait -- a TV wake is up to
    45s of WoL polling, and the amp should not queue behind it. Failures do
    not stop the others: half a scene is better than none, but the phone is
    told exactly which half, via the 502 detail.

    `stagger` spaces the START of each job without waiting for the previous
    one to finish. That distinction is the point: three devices striking
    their mains draw in the same instant is a surge worth avoiding, but the
    surge happens when a job begins, not when it completes -- so serialising
    would buy nothing and cost the TV's whole 45s wake window.
    """
    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        futures = []
        for index, (name, handler) in enumerate(jobs):
            if index and stagger:
                time.sleep(stagger)
            futures.append((name, pool.submit(handler, {})))
        done, failed = [], []
        for name, future in futures:
            try:
                message = future.result().get("message") or f"{name} ok"
            except Exception as exc:  # noqa: BLE001 - collected, not dropped
                failed.append(f"{name}: {exc}")
            else:
                done.append(message)
    if failed:
        raise RuntimeError("; ".join(failed + done))
    return {"message": "; ".join(done)}


def _tv_to_mac(args: dict[str, Any]) -> dict[str, Any]:
    """The TV side of the music scene: wake, then over to the Mac's HDMI.

    Sequential by necessity -- set_input refuses a TV in standby rather than
    implicitly waking it, so the wake must have finished first. The input id
    comes from config (tv.inputs.macmini), the same place the buttons get it.
    """
    from devices import config as device_config

    woke = tvlink.power_on(args)
    hdmi = str(device_config.load_config()
               .get("tv", {}).get("inputs", {}).get("macmini") or "HDMI_2")
    switched = tvlink.set_input(hdmi)(args)
    return {"message": ", ".join(
        m for m in (woke.get("message"), switched.get("message")) if m)}


# --- scenes from config ---------------------------------------------------
#
# A scene in config.yaml is a label, a note, and an ordered list of STEPS
# from the registry below -- named rack jobs, each one of the moves the
# hand-written scenes were already made of. The step names are the
# vocabulary; composing them is configuration. Everything is validated at
# import, so an unknown step stops the boot with the menu of real ones,
# never a 404 at 2am -- the key() guarantee, moved to the load layer.
#
# Everything-off stays BUILT-IN. The stop button is not configuration.

SCENE_STEPS: dict[str, tuple[str, Handler]] = {
    # display name in scene results, then the job
    "tv.to_mac": ("TV", _tv_to_mac),      # wake, then over to the Mac's HDMI
    "tv.on": ("TV", tvlink.power_on),
    "tv.off": ("TV", tvlink.power_off),
    "dac.to_usb": ("DAC", daclink.to_usb),   # powers it on, then USB
    "dac.off": ("DAC", daclink.power_off),
    "amp.on": ("amp", amplink.power_on),
    "amp.off": ("amp", amplink.power_off),
    "amp.music_level": ("amp", amplink.on_at_music_level),
    "mini.volume": ("mini", musiclink.scene_volume),
    "mini.player": ("player", musiclink.show_player),
    "music.quiesce": ("Music", musiclink.quiesce),
}

# What ui.scenes means when config does not say: exactly the Music scene the
# app has always had. Order is the power-up order, half a second apart: TV,
# then DAC, then amp -- the amp last because it is the biggest draw and the
# one whose inrush is worth keeping clear of the others. The two Mac-side
# steps follow; they cost the wall nothing but ride the same queue.
_DEFAULT_SCENES = [{
    "id": "music",
    "label": "Music mode",
    "glyph": "♫",
    "note": "amp on · TV to Mac · levels set",
    "steps": ["tv.to_mac", "dac.to_usb", "amp.music_level",
              "mini.volume", "mini.player"],
    "stagger": 0.5,
}]

_ID_OK = "abcdefghijklmnopqrstuvwxyz0123456789_"


def scenes() -> list[dict[str, Any]]:
    """The validated ui.scenes list -- 1 to 4 of them, or the default."""
    from devices import config as device_config

    configured = (device_config.load_config().get("ui") or {}).get("scenes")
    if not configured:
        return list(_DEFAULT_SCENES)
    if not isinstance(configured, list) or not 1 <= len(configured) <= 4:
        raise ValueError("ui.scenes must be a list of 1 to 4 scenes")
    out = []
    for entry in configured:
        scene_id = str(entry.get("id") or "")
        if not scene_id or any(c not in _ID_OK for c in scene_id):
            raise ValueError(f"ui.scenes id {scene_id!r} must be a lowercase "
                             "slug (a-z, 0-9, _)")
        steps = entry.get("steps") or []
        unknown = [s for s in steps if s not in SCENE_STEPS]
        if not steps or unknown:
            menu = ", ".join(sorted(SCENE_STEPS))
            raise ValueError(
                f"ui.scenes {scene_id!r}: unknown steps {unknown} -- "
                f"the steps that exist: {menu}")
        out.append({
            "id": scene_id,
            "label": str(entry.get("label") or scene_id),
            "glyph": str(entry.get("glyph") or ""),
            "note": str(entry.get("note") or ""),
            "steps": [str(s) for s in steps],
            "stagger": float(entry.get("stagger") or 0.0),
        })
    if len({s["id"] for s in out}) != len(out):
        raise ValueError("ui.scenes ids must be unique")
    return out


def _scene_handler(scene: dict[str, Any]) -> Handler:
    jobs = [SCENE_STEPS[name] for name in scene["steps"]]
    stagger = scene["stagger"]

    def run(args: dict[str, Any]) -> dict[str, Any]:
        return _rack(*jobs, stagger=stagger)
    return _one_scene_at_a_time(run)


def _scene_rows() -> list[tuple[str, Command]]:
    rows = []
    for scene in scenes():
        note = scene["note"] or ("runs: " + ", ".join(scene["steps"]))
        rows.append(_cmd(f"scene.{scene['id']}", "scene", scene["label"],
                         handler=_scene_handler(scene), note=note))
    return rows


def _scene_off(args: dict[str, Any]) -> dict[str, Any]:
    """TV, amp and DAC to standby, Music paused and its queue emptied."""
    return _rack(("amp", amplink.power_off), ("TV", tvlink.power_off),
                 ("DAC", daclink.power_off),
                 ("Music", musiclink.quiesce))


def _scene_resync(args: dict[str, Any]) -> dict[str, Any]:
    """Re-read whatever can actually be read -- today, the TV."""
    from . import state

    snap = state.snapshot()
    snap["implemented"] = implemented()
    return {"message": "re-read the TV; the rest cannot be asked yet",
            "state": snap}


@dataclass(frozen=True)
class Command:
    """One thing the remote can ask for."""

    id: str
    device: str  # scene | tv | amp | dac | music
    label: str  # said back to the user in the toast, so write it for humans
    handler: Handler | None = None
    note: str = ""  # why it is not simply "press button, thing happens"


def _cmd(*args: Any, **kwargs: Any) -> tuple[str, Command]:
    command = Command(*args, **kwargs)
    return command.id, command


# --- the catalogue -------------------------------------------------------
#
# Notes carry the traps recorded in configs/: the ones that will otherwise be
# rediscovered painfully at wiring time.

# The MAC7200's trim commands have no buttons (removed from the amp panel,
# same precedent as the TV's picture modes): TRU/TRD belong to the PDF
# dialect this firmware rejects, and no working equivalent has been
# discovered. The ids stay here as the record, and for curl if one turns up.
_TRIM_NOTE = ("This unit rejects the documented TRU/TRD commands; the real "
              "trim commands, if any, are still undiscovered.")

def _capability(category: str, method: str, handler: Handler) -> Handler | None:
    """Wire the handler only when the configured driver actually has the
    verb. A driver that leaves an optional interface method alone gets the
    same honest treatment as an unwritten handler: the row answers 501
    with its note, and the phone dims the key. Which is the point of 3.0:
    what the rack can do is derived from what its drivers say, not from a
    hand-maintained table agreeing with the code."""
    from devices import registry

    cls = registry.driver_class(category)
    interface = registry.CATEGORIES[category][1]
    inherited = getattr(cls, method, None) is getattr(interface, method, None)
    return None if inherited else handler


def _rows() -> list[tuple[str, Command]]:
    return _scene_rows() + [
        # -- whole-rack ---------------------------------------------------
        _cmd("scene.off", "scene", "Everything off",
             handler=_one_scene_at_a_time(_scene_off),
             note="TV, amp and D900 to standby, Music paused and its queue "
                  "emptied. The DAC only powers down when its tracked state "
                  "says it is on -- a toggle cannot be aimed blind."),
        _cmd("scene.resync", "scene", "Re-read every device",
             handler=_scene_resync,
             note="The TV (SSAP) and amp (RS-232) are asked for real."),

        # -- LG OLED77G6 --------------------------------------------------
        _cmd("tv.power.on", "tv", "TV on",
             handler=tvlink.power_on,
             note="Wake-on-LAN first, then connect: standby keeps port 3000 "
                  "open but refuses new SSAP registrations (ws close 1008), "
                  "so the order is not optional."),
        _cmd("tv.power.off", "tv", "TV off",
             handler=tvlink.power_off,
             note="To standby, not hard off -- Quick Start+ keeps the network "
                  "stack up, which is what makes waking it possible."),
        _cmd("tv.input.hdmi1", "tv", "TV to HDMI 1",
             handler=tvlink.set_input("HDMI_1")),
        _cmd("tv.input.hdmi2", "tv", "TV to HDMI 2",
             handler=tvlink.set_input("HDMI_2")),
        _cmd("tv.input.hdmi3", "tv", "TV to HDMI 3",
             handler=tvlink.set_input("HDMI_3")),
        _cmd("tv.input.hdmi4", "tv", "TV to HDMI 4",
             handler=tvlink.set_input("HDMI_4")),
        _cmd("tv.nav.up", "tv", "TV up", handler=tvlink.press("UP")),
        _cmd("tv.nav.down", "tv", "TV down", handler=tvlink.press("DOWN")),
        _cmd("tv.nav.left", "tv", "TV left", handler=tvlink.press("LEFT")),
        _cmd("tv.nav.right", "tv", "TV right", handler=tvlink.press("RIGHT")),
        _cmd("tv.nav.ok", "tv", "TV OK", handler=tvlink.press("ENTER")),
        _cmd("tv.nav.back", "tv", "TV back", handler=tvlink.press("BACK")),
        _cmd("tv.nav.home", "tv", "TV home", handler=tvlink.press("HOME")),
        _cmd("tv.nav.exit", "tv", "TV exit", handler=tvlink.press("EXIT")),
        _cmd("tv.picture.cinema", "tv", "Picture mode cinema",
             note="CONFIRMED impossible over the API on this set: webOS 3.0 "
                  "has no settings write service (404). Only path is scripted "
                  "menu presses. Before implementing THAT, note the owner's "
                  "calibration must be captured in the private Application "
                  "Support state -- restore from it, and "
                  "re-run scripts/tv_picture_baseline.py after deliberate "
                  "changes."),
        _cmd("tv.picture.standard", "tv", "Picture mode standard"),
        _cmd("tv.picture.vivid", "tv", "Picture mode vivid"),
        _cmd("tv.picture.game", "tv", "Picture mode game"),
        _cmd("tv.speakers.off", "tv", "Force TV speakers off",
             handler=tvlink.speakers_off,
             note="The invariant. Will be enforced by watching the TV's state "
                  "subscription; this is the manual re-assert."),
        _cmd("tv.info", "tv", "TV info", handler=tvlink.press("INFO")),

        # -- McIntosh MAC7200 ---------------------------------------------
        # This unit rejects the transcribed PDF dialect wholesale (measured
        # 2026-08-03); everything live here speaks the token-as-command form
        # in mac7200_protocol.yaml's `measured:` block.
        _cmd("amp.power.on", "amp", "Amp on",
             handler=amplink.power_on,
             note="A single (PWR 1) -- the documented PON-twice quirk belongs "
                  "to the rejected dialect. The amp acknowledges at once but "
                  "the audio path settles for ~10s."),
        _cmd("amp.power.off", "amp", "Amp off", handler=amplink.power_off),
        _cmd("amp.power.toggle", "amp", "Amp power",
             handler=amplink.power_toggle,
             note="Reads the real power state over RS-232 first, so the one "
                  "key is never a guess about which way it will go."),
        _cmd("amp.vol.up", "amp", "Volume up", handler=amplink.vol_up),
        _cmd("amp.vol.down", "amp", "Volume down", handler=amplink.vol_down),
        _cmd("amp.vol.set", "amp", "Set volume",
             handler=amplink.vol_set,
             note="(VOL 0-100), absolute on the amp's own scale. The amp "
                  "slews to the target rather than jumping."),
        _cmd("amp.vol.music", "amp", "Music level",
             handler=amplink.vol_preset("music"),
             note="Target lives in config.yaml amp.presets.music."),
        _cmd("amp.vol.cinema", "amp", "Cinema level",
             handler=amplink.vol_preset("cinema"),
             note="Target lives in config.yaml amp.presets.cinema."),
        _cmd("amp.mute", "amp", "Mute", handler=amplink.mute),
        _cmd("amp.mute.on", "amp", "Mute amp",
             handler=amplink.set_mute(True)),
        _cmd("amp.mute.off", "amp", "Unmute amp",
             handler=amplink.set_mute(False)),
        _cmd("amp.input.dac", "amp", "Amp to the D900",
             handler=_capability("amp", "set_input", amplink.set_input()),
             note="Sends whichever (INP n) config's amp.dac_input_id names -- "
                  "PRESUMED 2 until confirmed against the front panel."),
        _cmd("amp.input.mc", "amp", "Amp to MC",
             handler=_capability("amp", "set_input", amplink.set_input(1))),
        _cmd("amp.input.mm", "amp", "Amp to MM",
             handler=_capability("amp", "set_input", amplink.set_input(2))),
        _cmd("amp.input.cd1", "amp", "Amp to CD1",
             handler=_capability("amp", "set_input", amplink.set_input(3))),
        _cmd("amp.input.cd2", "amp", "Amp to CD2",
             handler=_capability("amp", "set_input", amplink.set_input(4))),
        _cmd("amp.input.dvd", "amp", "Amp to DVD",
             handler=_capability("amp", "set_input", amplink.set_input(5))),
        _cmd("amp.input.aux", "amp", "Amp to AUX",
             handler=_capability("amp", "set_input", amplink.set_input(6))),
        _cmd("amp.input.server", "amp", "Amp to Server",
             handler=_capability("amp", "set_input", amplink.set_input(7))),
        _cmd("amp.input.d2a", "amp", "Amp to D2A",
             handler=_capability("amp", "set_input", amplink.set_input(8))),
        _cmd("amp.input.tuner", "amp", "Amp to Tuner",
             handler=_capability("amp", "set_input", amplink.set_input(9))),
        _cmd("amp.trim.bass.up", "amp", "Bass up", note=_TRIM_NOTE),
        _cmd("amp.trim.bass.down", "amp", "Bass down", note=_TRIM_NOTE),
        _cmd("amp.trim.treble.up", "amp", "Treble up", note=_TRIM_NOTE),
        _cmd("amp.trim.treble.down", "amp", "Treble down", note=_TRIM_NOTE),
        _cmd("amp.trim.balance.left", "amp", "Balance left", note=_TRIM_NOTE),
        _cmd("amp.trim.balance.right", "amp", "Balance right",
             note=_TRIM_NOTE),
        _cmd("amp.speakers.op1", "amp", "Speaker output 1",
             note="(OP1 n) should work by the token-as-command pattern, but "
                  "the polarity is unverified and the failure mode is silent "
                  "dead speakers -- staying unwired until tried by hand."),
        _cmd("amp.speakers.op2", "amp", "Speaker output 2"),
        _cmd("amp.query", "amp", "Read state from the amp",
             handler=amplink.query,
             note="(QRY) is the only query this firmware has -- (HLP) is "
                  "rejected, and '?' parameters parse as 0."),


        # -- Topping D900 --------------------------------------------------
        # IR through the iTach, port 2, PROVEN 2026-08-07: a full 8-press
        # cycle returned to its starting input, so every press landed. Open
        # loop still -- nothing here is ever read back, hence "(tracked)"
        # in every message and dac.resync as the correction.
        _cmd("dac.input.usb", "dac", "D900 to USB",
             handler=daclink.select("usb"),
             note="Seven presses from OPT1 at 0.3s each -- the cycle only "
                  "turns one way, so this direction is the long way round."),
        _cmd("dac.input.opt1", "dac", "D900 to OPT1",
             handler=daclink.select("opt1"),
             note="One press from USB."),
        _cmd("dac.input.next", "dac", "D900 next input",
             handler=_capability("dac", "step", daclink.input_next)),
        _cmd("dac.power", "dac", "D900 power",
             handler=_capability("dac", "power_toggle", daclink.power_toggle),
             note="The remote's power is a toggle, so this flips a tracked "
                  "belief; from an unestablished state it stays unknown "
                  "until someone looks at the panel."),
        _cmd("dac.power.resync", "dac", "Tell the app if the D900 is on",
             handler=_capability("dac", "power_resync", daclink.power_resync),
             note="Power is a toggle, so reaching a state needs knowing the "
                  "current one. This is how that gets seeded or corrected."),
        _cmd("dac.resync", "dac", "Tell the app which D900 input is live",
             handler=_capability("dac", "resync", daclink.resync),
             note="IR is open-loop: a press the DAC misses desyncs the count "
                  "silently. This corrects the app without sending anything."),

        # -- configured music source --------------------------------------
        # Apple Music and Roon implement the same library/service/transport
        # contract. Browsing data is on /api/music/*; only actions live here.
        _cmd("music.play_pause", "music", "Play / pause",
             handler=musiclink.play_pause,
             note="Also launches Music if it is somehow not running."),
        _cmd("music.play", "music", "Play",
             handler=_capability("music", "play", musiclink.play)),
        _cmd("music.pause", "music", "Pause",
             handler=_capability("music", "pause", musiclink.pause)),
        _cmd("music.next", "music", "Next track", handler=musiclink.next_track),
        _cmd("music.prev", "music", "Previous track",
             handler=musiclink.prev_track),
        _cmd("music.shuffle", "music", "Shuffle",
             handler=musiclink.toggle_shuffle),
        _cmd("music.shuffle.on", "music", "Shuffle on",
             handler=musiclink.set_shuffle(True)),
        _cmd("music.shuffle.off", "music", "Shuffle off",
             handler=musiclink.set_shuffle(False)),
        _cmd("music.repeat_one", "music", "Repeat one",
             handler=musiclink.toggle_repeat_one,
             note="Toggles one/off only -- repeat-all is not offered."),
        _cmd("music.repeat_one.on", "music", "Repeat one on",
             handler=musiclink.set_repeat_one(True)),
        _cmd("music.repeat.off", "music", "Repeat off",
             handler=musiclink.set_repeat_one(False)),
        _cmd("music.play_album", "music", "Play album",
             handler=musiclink.play_album),
        _cmd("music.play_playlist", "music", "Play playlist",
             handler=musiclink.play_playlist,
             note="Whole playlist, or from a tapped song onward."),
        _cmd("music.play_recent", "music", "Play from recently added",
             handler=musiclink.play_recent,
             note="The song view's tap: this song, then newest-to-oldest "
                  "onward -- or the rest of the library pre-shuffled when "
                  "shuffle is on."),
        _cmd("music.play_track", "music", "Play song",
             handler=musiclink.play_track,
             note="Rebuilds the queue playlist with the song's album and "
                  "starts at the song -- Music's own Up Next is not "
                  "scriptable."),
        _cmd("music.queue_add", "music", "Add to queue",
             handler=musiclink.queue_add,
             note="Appends to the avctl playlist. 'Play next' cannot exist: "
                  "AppleScript can neither insert into nor reorder a "
                  "playlist, and Up Next is closed. Appends only follow "
                  "automatically while playback is inside that playlist."),
        _cmd("music.clear", "music", "Stop and clear the queue",
             handler=musiclink.clear_queue,
             note="Stops playback and empties the avctl playlist. The stop "
                  "is required, not a bonus: Music keeps the current track "
                  "open after it leaves the playlist and would play it out "
                  "over an empty queue."),
        _cmd("music.refresh", "music", "Re-scan recently added",
             handler=musiclink.refresh),
        # The mini's OWN output volume -- where this user actually rides
        # level day to day. macOS system volume, not Music's slider (that
        # stays at 100 into the DAC) and not the amp (which has its own dial).
        _cmd("music.vol.up", "music", "Mini volume up",
             handler=_capability("music", "set_system_volume", musiclink.vol_up)),
        _cmd("music.vol.down", "music", "Mini volume down",
             handler=_capability("music", "set_system_volume", musiclink.vol_down)),
        _cmd("music.vol.set", "music", "Set mini volume",
             handler=_capability("music", "set_system_volume", musiclink.vol_set)),
        _cmd("music.mute", "music", "Mute the mini",
             handler=_capability("music", "set_system_muted", musiclink.mute_toggle)),
        _cmd("music.mute.on", "music", "Mute the mini",
             handler=_capability(
                 "music", "set_system_muted", musiclink.set_muted(True))),
        _cmd("music.mute.off", "music", "Unmute the mini",
             handler=_capability(
                 "music", "set_system_muted", musiclink.set_muted(False))),
        # Search itself is a GET route (/api/music/search) -- only the add
        # action is a command. Until music.team_id lands in config.yaml
        # this answers 502 with exactly what is missing.
        _cmd("music.play_song", "music", "Play just this song",
             handler=musiclink.play_song,
             note="The queue playlist becomes this one song. The album-"
                  "context play is music.play_track."),
        _cmd("music.service.play", "music", "Play service item",
             handler=musiclink.play_service,
             note="Provider-neutral direct playback: Apple Music catalog or "
                  "Roon/Qobuz, without importing into the library."),
        _cmd("music.service.queue", "music", "Queue service item",
             handler=musiclink.queue_service,
             note="Provider-neutral append through the centralized queue."),
        _cmd("music.add", "music", "Add to library",
             handler=musiclink.add,
             note="Shown only when the configured service advertises library "
                  "mutation. Apple Music needs one-time /music/auth; Roon "
                  "saves Qobuz metadata in avctl's virtual library."),
    ]


COMMANDS: dict[str, Command] = dict(_rows())


def rebuild() -> None:
    """Re-derive the table -- for tests that swap the configured drivers."""
    global COMMANDS
    COMMANDS = dict(_rows())


def implemented() -> list[str]:
    """Ids that actually do something. The rest are drawn as coming soon."""
    return sorted(id for id, command in COMMANDS.items() if command.handler)


def get(command_id: str) -> Command | None:
    return COMMANDS.get(command_id)
