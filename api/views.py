"""The remote's markup: one shell, registered panels, and a language agent.

Layout decisions worth knowing before editing:

* **One page, registered panels, hash routing.** The phone loads this once and
  switches panels locally, so pressing a button never costs a page load. That
  matters in a dark room with one bar of signal.
* **The screen is global.** Scene, TV input, amp volume, the mini's volume
  and what is playing sit above every panel, because "what is the rack doing"
  is the question the phone is pulled out to answer, whichever panel is open.
* **Only what works is drawn.** The D900 and the disc player have no
  controls anywhere: they are IR-only and no reliable IR path to them exists
  (DECIDED 2026-08-05 after the blaster attempts). A button that cannot do
  its thing is furniture.
* **Every button goes through `key()`**, which refuses ids that are not in
  commands.py. Adding a button therefore forces adding the command, and the
  browser dims whatever has no handler yet.

The layout is written for a phone held one-handed: nothing that matters sits
above the screen, and the panel tabs are at the bottom where a thumb is.
"""

from __future__ import annotations

import json
import os
import threading
from html import escape, unescape
from pathlib import Path
from typing import Any

from . import amplink, commands, pwa
from .auth import Identity

# Breaks the phone's cache whenever app.css/app.js change. A stale remote
# that half-works is worse than one that plainly does not load -- proven
# live 2026-08-08, when a hand-bumped version was forgotten and the panel
# ran old JS against a new API (empty titles, dead clicks, blank search).
# Content hash now: forgetting is no longer possible.
def _asset_version() -> str:
    import hashlib
    ui = Path(__file__).resolve().parent / "ui"
    digest = hashlib.sha256()
    for name in ("app.css", "app.js"):
        try:
            digest.update((ui / name).read_bytes())
        except OSError:
            digest.update(name.encode())
    return digest.hexdigest()[:10]


ASSET_VERSION = _asset_version()


def _splash_links() -> str:
    """One apple-touch-startup-image per device size we render for.

    iOS matches these by exact logical size and ratio -- an image without
    the right media query is silently ignored, which is why the list lives
    in pwa.SPLASH_POINTS and the route whitelist is generated from it too.
    """
    return "".join(
        f"<link rel='apple-touch-startup-image' "
        f"href='/static/splash-{w * r}x{h * r}.png' "
        f"media='(device-width: {w}px) and (device-height: {h}px) and "
        f"(-webkit-device-pixel-ratio: {r}) and (orientation: portrait)'>"
        for w, h, r in pwa.SPLASH_POINTS
    )


def _page(title: str, body: str, script: bool = False) -> str:
    tag = f"<script src='/static/app.js?v={ASSET_VERSION}' defer></script>" if script else ""
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1,"
        "viewport-fit=cover'>"
        "<meta name='color-scheme' content='light dark'>"
        "<meta name='apple-mobile-web-app-capable' content='yes'>"
        # 'black', not 'black-translucent': opaque keeps the clock and
        # battery off the screen readouts, and the app is dark anyway.
        "<meta name='apple-mobile-web-app-status-bar-style' content='black'>"
        "<meta name='apple-mobile-web-app-title' content='avctl'>"
        f"<meta name='theme-color' content='{pwa.THEME_COLOR}'>"
        "<link rel='manifest' href='/manifest.webmanifest'>"
        "<link rel='apple-touch-icon' href='/static/icon-180.png'>"
        + _splash_links() +
        f"<title>{escape(title)}</title>"
        f"<link rel='stylesheet' href='/static/app.css?v={ASSET_VERSION}'>"
        f"{tag}</head><body>{body}</body></html>"
    )


# --- building blocks -----------------------------------------------------


def key(
    command_id: str,
    label: str,
    sub: str = "",
    glyph: str = "",
    cls: str = "",
    repeat: bool = False,
    args: dict | None = None,
) -> str:
    """One button.

    An id the table does not know renders as a DIMMED key rather than
    crashing the render: a newer panel is allowed to draw a button this
    build has not grown yet, and the press answers "coming soon" -- the
    same honest degradation an unwritten handler gets. Drift in OUR OWN
    config still fails loudly, at boot, where scenes and panels validate;
    this softness is for version skew, not typos.
    """
    if commands.get(command_id) is None:
        cls = (cls + " soon").strip()

    attrs = [f"class='{('key ' + cls).strip()}'", f"data-cmd='{command_id}'"]
    if repeat:
        # Volume and the D900 cycle are the only things worth holding down.
        attrs.append("data-repeat-ok")
    if args:
        attrs.append(f"data-args='{escape(json.dumps(args), quote=True)}'")

    inner = f"<span class='g'>{glyph}</span>" if glyph else ""
    inner += escape(label)
    if sub:
        inner += f"<small>{escape(sub)}</small>"
    return f"<button {' '.join(attrs)}>{inner}</button>"


def panel(title: str, body: str, aside: str = "", aside_id: str = "") -> str:
    identity = (f" id='{escape(aside_id, quote=True)}'"
                if aside_id else "")
    side = f"<small{identity}>{escape(aside)}</small>" if aside else ""
    return f"<section class='panel'><h2>{escape(title)}{side}</h2>{body}</section>"


def gap() -> str:
    """One row's worth of space between two grids in the same panel."""
    return "<div style='height:7px'></div>"


def grid(*keys: str, cols: int = 2) -> str:
    cls = "grid" + (f" c{cols}" if cols != 2 else "")
    return f"<div class='{cls}'>{''.join(keys)}</div>"


def dpad(prefix: str) -> str:
    """The TV's four-way cluster."""
    corner = "<span class='key corner'></span>"
    return (
        "<div class='dpad'>"
        + corner + key(f"{prefix}.nav.up", "", glyph="&#9650;") + corner
        + key(f"{prefix}.nav.left", "", glyph="&#9664;")
        + key(f"{prefix}.nav.ok", "OK", cls="ok accent")
        + key(f"{prefix}.nav.right", "", glyph="&#9654;")
        + corner + key(f"{prefix}.nav.down", "", glyph="&#9660;") + corner
        + "</div>"
    )


def note(text: str) -> str:
    return f"<p class='note'>{text}</p>"


def more(title: str, body: str) -> str:
    """A fold for controls that are real but rarely reached for.

    The rack gets switched between two things. Nine amp inputs and six trim
    keys are not that, and putting them on the same screen as the volume makes
    the volume harder to find in the dark.
    """
    return (
        f"<details class='more'><summary>{escape(title)}</summary>"
        f"<div>{body}</div></details>"
    )


def volume_dial() -> str:
    """The amp volume control, on both the home and the amp panel.

    The number is a class rather than an id because it appears twice, and the
    two must never disagree about how loud the room is.
    """
    return (
        "<div class='dialrow'>"
        "<button class='dial' data-cmd='amp.mute'>"
        "<span class='v js-vol'>--</span>"
        "<span class='u'>tap: mute</span></button>"
        "<div class='stack'>"
        + key("amp.vol.up", "Volume up", glyph="&#43;", repeat=True)
        + key("amp.vol.down", "Volume down", glyph="&#8722;", repeat=True)
        + "</div></div>"
    )


# --- the screen ----------------------------------------------------------


def _screen() -> str:
    """Four readouts and a ticker. Values arrive from /api/state, never from
    the last button pressed -- the physical remotes are still in the room."""
    def ro(key_: str, label: str) -> str:
        return f"<div class='ro'><b>{label}</b><span id='ro-{key_}'>--</span></div>"

    return (
        "<section class='screen'>"
        "<div class='screen-head'><span>scene</span><span id='scene'>--</span></div>"
        "<div class='readouts'>"
        # The four boxes as a status strip (the widget's layout, brought
        # home with the Glass scheme): TV, then the audio chain outward
        # from the speakers -- DAC, amp, and the mini feeding it. The id
        # stays ro-mac (app.js paints by id); only the LABEL says MINI.
        # What is playing is not here -- the transport bar below carries it
        # with the artwork, and saying it twice is glass spent on nothing.
        # No ticker either: problems show on the LED.
        + ro("tv", "TV") + ro("dac", "DAC") + ro("amp", "AMP")
        + "<div class='ro'><b>MINI</b><span id='ro-mac'>--</span></div>"
        + "</div>"
        "</section>"
    )


# --- panels --------------------------------------------------------------


def _scene_grid() -> str:
    """The start buttons, from ui.scenes in config -- one fat key when
    there is one scene, full-width rows for two, a 2-up grid for three or
    four. Everything-off is not configuration; it is always last."""
    configured = commands.scenes()
    if len(configured) <= 2:
        # No 'tall' for the lone scene any more: on a 440pt phone the hero
        # treatment read as bloat, not importance (verdict from the first
        # day of the native app, 2026-08-08).
        keys = [key(f"scene.{s['id']}", s["label"], s["note"],
                    glyph=escape(s["glyph"]), cls="wide accent")
                for s in configured]
    else:
        keys = [key(f"scene.{s['id']}", s["label"], s["note"],
                    glyph=escape(s["glyph"]), cls="accent")
                for s in configured]
    keys.append(key("scene.off", "Everything off", glyph="&#9211;",
                    cls="wide off"))
    return grid(*keys)


def _home() -> str:
    """Everything reachable without leaving the first screen.

    The scene keys -- the start buttons config declares and the stop
    button -- then the two controls that get touched between them: TV
    power/input and the amp volume. Only what actually works is drawn.
    """
    scenes = _scene_grid()

    # One 2x2 like the amp section, subtitles kept by request: they state
    # what is confirmed, not guessed (inputs read off the TV 2026-08-02).
    tv_controls = grid(
        key("tv.power.on", "TV on", "wake-on-LAN", cls="accent"),
        key("tv.power.off", "TV off", cls="off"),
        key("tv.input.hdmi1", "HDMI 1", "Disc"),
        key("tv.input.hdmi2", "HDMI 2", "Mac mini"),
    )

    # One 4-column grid so the two rows line up exactly: each power key is
    # half the width, the DAC's step matches them, and the volume pair
    # splits the amp's half into quarters. Every key the same height as
    # every other key on this screen.
    rack_controls = grid(
        key("dac.power", "DAC power", "IR toggle", cls="half"),
        key("amp.power.toggle", "Amp power", cls="half accent"),
        key("dac.input.next", "DAC input", "step", cls="half"),
        key("amp.vol.up", "Vol", glyph="&#43;", repeat=True),
        key("amp.vol.down", "Vol", glyph="&#8722;", repeat=True),
        cols=4,
    )

    return (
        panel("Scenes", scenes)
        + panel("TV", tv_controls, aside="OLED77G6")
        + panel("DAC / Amp", rack_controls, aside="D900 / MAC7200")
    )


def _tv() -> str:
    power = grid(
        key("tv.power.on", "On", "wake-on-LAN", cls="accent"),
        key("tv.power.off", "Off", cls="off"),
    )
    inputs = grid(
        key("tv.input.hdmi1", "HDMI 1", "Disc"),
        key("tv.input.hdmi2", "HDMI 2", "Mac mini"),
        key("tv.input.hdmi3", "HDMI 3", "empty"),
        key("tv.input.hdmi4", "HDMI 4", "empty"),
        cols=4,
    )
    nav = dpad("tv") + grid(
        key("tv.nav.back", "Back", glyph="&#8617;"),
        key("tv.nav.home", "Home", glyph="&#8962;"),
        cols=2,
    )
    # The invariant gets a key of its own because it is the one thing about
    # this TV that is not a preference: its speakers must never come on. It is
    # enforced continuously elsewhere; this is the manual re-assert for when
    # someone has just been in the TV's own menus.
    speakers = key("tv.speakers.off", "Speakers off", "invariant", cls="wide warm")
    # No picture-mode keys. Not "coming soon" -- confirmed impossible on this
    # set (webOS 3.0 has no settings write service; measured 404 2026-08-02),
    # and a button that can never work is furniture. The command ids stay in
    # commands.py as the record of why, next to the pointer at the calibration
    # baseline any future menu-walking implementation must restore.
    extras = grid(
        key("tv.nav.exit", "Exit", glyph="&#10005;"),
        key("tv.info", "Info", glyph="&#8505;"),
    )
    return (
        panel("Power", power, aside="LG OLED77G6")
        + panel("Input", inputs)
        + panel("Navigate", nav)
        + panel("Audio", speakers)
        + more("Extras", extras)
    )


def _amp() -> str:
    power = grid(
        key("amp.power.on", "On", "settles ~10s", cls="accent"),
        key("amp.power.off", "Off", cls="off"),
    )
    volume = (
        volume_dial()
        + f"<input class='slider' id='amp-slider' type='range' min='0' "
          f"max='{amplink.max_volume()}' "
          "step='1' value='0' aria-label='volume'>"
        + f"<div class='scale'><span>0</span><span>35</span>"
          f"<span>{amplink.max_volume()}</span></div>"
        + grid(
            key("amp.vol.music", "Music level", "preset"),
            key("amp.vol.cinema", "Cinema level", "preset"),
        )
    )
    source = key("amp.input.dac", "D900", "the balanced pair", cls="wide accent")
    other_inputs = (
        grid(
            key("amp.input.mc", "MC"),
            key("amp.input.mm", "MM"),
            key("amp.input.cd1", "CD1"),
            key("amp.input.cd2", "CD2"),
            key("amp.input.dvd", "DVD"),
            key("amp.input.aux", "AUX"),
            key("amp.input.server", "Server"),
            key("amp.input.d2a", "D2A"),
            key("amp.input.tuner", "Tuner"),
            cols=3,
        )
    )
    # No trim keys. Not "coming soon" -- this firmware rejects the documented
    # TRU/TRD commands (measured 2026-08-03) and no working equivalent has
    # been discovered, so a trim button today can never work, and a button
    # that can never work is furniture. The command ids stay in commands.py
    # as the record; if a real trim dialect is ever found (the QRY dump's
    # TBA/TDB/THH tokens hint one may exist), the keys come back here.
    outputs = grid(
        key("amp.speakers.op1", "Output 1"),
        key("amp.speakers.op2", "Output 2"),
        key("amp.query", "Read state from the amp", glyph="&#8635;", cls="wide"),
    )
    # The D900 shares this page: it is one link up the same chain, it has
    # only a handful of controls, and a page of its own would be four
    # buttons on an empty screen. USB leads because it is the safe harbour
    # -- always signal, so it doubles as the wake from auto-standby.
    dac = grid(
        key("dac.input.usb", "USB", "Mac mini", cls="accent"),
        key("dac.input.opt1", "OPT1", "Disc"),
        key("dac.input.next", "Next input"),
        key("dac.power", "Power", "IR toggle"),
    ) + ("<button class='link' id='dac-resync'>Resync"
         "<small id='dac-detail'></small></button>")

    return (
        panel("DAC", dac, aside="Topping D900")
        + panel("Power", power, aside="McIntosh MAC7200")
        + panel("Volume", volume)
        + panel("Input", source)
        + more("Other inputs", other_inputs)
        + more("Outputs", outputs
               + note("<b>OP1/OP2 are documented inverted</b> -- unverified "
                      "until they have been tried once."))
    )


def _music() -> str:
    """The Music panel: a shell the browser fills in.

    Unlike the other panels the contents here are data -- whatever albums the
    library holds this week -- so the markup ships empty containers and
    app.js renders into them from /api/music/*. The buttons still go through
    key(), so every action id is forced into the command table like
    everywhere else.
    """
    # One compact rail that changes scope in place. Search replaces the
    # Albums / Songs / Playlists segment with Library / Apple Music in the
    # same slot; there is no second navigation arrangement or search state.
    head = (
        "<div class='m-nav' id='m-nav'>"
        "<div class='m-nav-head'>"
        "<button class='iconbtn m-nav-action m-nav-view' id='m-view-toggle' "
        "aria-label='Search library and music service'>"
        "<span class='m-nav-search-glyph' aria-hidden='true'></span>"
        "<span class='m-nav-back-glyph' aria-hidden='true'>&#8617;</span>"
        "</button>"
        "<button class='iconbtn m-nav-action m-nav-refresh' id='m-refresh' "
        "aria-label='re-scan recently added'>&#8635;</button>"
        "</div>"
        "<div class='seg m-nav-scopes' id='m-lib-scope'>"
        "<div class='seg-track'>"
        "<button class='seg-btn on' data-lib='recent'>Albums</button>"
        "<button class='seg-btn' data-lib='songs'>Songs</button>"
        "<button class='seg-btn' data-lib='playlists'>Playlists</button>"
        "</div></div>"
        "<div class='seg m-search-scopes' id='m-scope'>"
        "<div class='seg-track'>"
        "<button class='seg-btn on' data-scope='library'>Library</button>"
        "<button class='seg-btn' id='m-service-scope' "
        "data-scope='catalog'>Apple Music</button>"
        "</div></div></div>"
    )
    stage = (
        "<section class='m-stage' aria-label='Now Playing'>"
        "<div class='m-stage-top'>"
        "<span class='cover js-mnow-cover'></span>"
        "<div class='m-stage-copy'><h3 class='js-mnow-track'>--</h3>"
        "<p class='js-mnow-sub'></p>"
        "<div class='m-progress'><i class='js-mprogress'></i></div>"
        "<div class='m-times'><span class='js-mtime-now'>--:--</span>"
        "<span class='js-mtime-total'>--:--</span></div></div></div>"
        "<div class='m-stage-controls'>"
        + key("music.shuffle", "", glyph="&#8646;", cls="mkey")
        + key("music.prev", "", glyph="&#9646;&#9664;", cls="mkey")
        + key("music.play_pause", "", glyph="&#9654;", cls="mkey accent")
        + key("music.next", "", glyph="&#9654;&#9646;", cls="mkey")
        + "<button class='mq js-mq' hidden aria-label='Open Up Next'>"
          "<b class='js-mq-n'>0</b><small>next&nbsp;&rsaquo;</small></button>"
        "</div></section>"
    )
    cover_flow = (
        "<section class='m-coverflow' aria-label='Recently Added cover flow'>"
        "<div class='m-coverflow-head'><span>Recently added</span>"
        "<small>Swipe to browse</small></div>"
        "<div class='m-coverflow-stage'>"
        "<div class='m-coverflow-track' id='m-coverflow-track'></div>"
        "<div class='m-coverflow-copy'>"
        "<h3 id='m-coverflow-title'>Your library</h3>"
        "<p id='m-coverflow-artist'>Recently added albums</p>"
        "<div class='m-coverflow-actions'>"
        "<button class='key accent' id='m-coverflow-play' disabled>"
        "&#9654;&nbsp; Play album</button>"
        "<button class='mq js-mq' hidden aria-label='Open Up Next'>"
        "<b class='js-mq-n'>0</b><small>next&nbsp;&rsaquo;</small></button>"
        "</div><div class='m-coverflow-dots' id='m-coverflow-dots' "
        "aria-hidden='true'></div></div></div></section>"
    )
    library = (
        "<div id='m-library'>"
        # A playlist tile speaks the album tiles' whole gesture grammar --
        # tap opens, double-tap plays, hold queues -- it is just a playlist.
        "<div id='m-recent'>"
        "<div class='mgrid' id='m-grid'></div>"
        # The infinite-scroll sentinel: when it scrolls into view the next
        # page of albums is fetched, all the way to the end of the library.
        "<div id='m-more'></div>"
        "<p class='note' id='m-empty' hidden>nothing here yet -- the library "
        "scan found no albums.</p>"
        "</div>"
        "<div id='m-songs-wrap' hidden>"
        "<div class='msongs' id='m-songs'></div>"
        "<div id='m-songs-more'></div>"
        "<p class='note' id='m-songs-empty' hidden>no songs in the library "
        "yet.</p>"
        "</div>"
        "<div id='m-playlists-wrap' hidden>"
        "<div class='mgrid' id='m-playlists'></div>"
        "<p class='note' id='m-pl-empty' hidden>no playlists in the "
        "library.</p>"
        "</div>"
        "</div>"
    )
    albumv = (
        "<div id='m-album' hidden>"
        "<button class='iconbtn return-action m-album-back' id='m-back' "
        "aria-label='Back to music browsing'>&#8617;</button>"
        "<div class='m-album-head' id='m-album-head'></div>"
        "<div class='msongs' id='m-tracks'></div>"
        "</div>"
    )
    # Two searches, one box -- scope pills under it, the way Music's own
    # search screen does it. Library is the default: playing what is already
    # here is the everyday act, adding from the catalog the occasional one.
    # Both scopes remain album-only. Catalog relevance also considers songs:
    # searching a track title promotes the album containing that recording,
    # while the UI keeps the same simple album browsing grammar.
    search_view = (
        "<div id='m-search' hidden>"
        "<input class='m-q' id='m-q' type='search' inputmode='search' "
        "placeholder='search the library' autocomplete='off' "
        "autocapitalize='off' autocorrect='off' spellcheck='false'>"
        "<div class='m-explore' id='m-explore' hidden></div>"
        "<div class='mgrid' id='m-results'></div>"
        "<p class='note' id='m-search-note'>relevant albums first -- "
        "library tiles: tap opens, double-tap plays, hold queues; "
        "service tiles open the album for its supported actions.</p>"
        "</div>"
    )
    return panel(
        "Music", head + stage + cover_flow + library + albumv + search_view,
        aside="Music", aside_id="m-provider-label",
    )


def transport_queue() -> str:
    """The transport-owned logical queue, shown in place of the control rail."""
    return (
        "<section class='mq-focus' id='m-queue' hidden aria-label='Up Next'>"
        "<header class='mq-focus-head'>"
        "<button class='iconbtn return-action mq-back' id='mq-back' "
        "aria-label='Back to transport'>&#8617;</button>"
        "<h2>Up Next</h2><span></span>"
        "</header>"
        "<div class='mq-scroll' id='mq-scroll'>"
        "<div class='mq-section js-mq-playing-wrap' id='mq-playing-wrap'>"
        "<h3>Playing</h3><div class='js-mq-playing' id='mq-playing'></div>"
        "</div>"
        "<div class='mq-section'>"
        "<h3>Next &middot; <span class='js-mq-count' id='mq-count'>0</span></h3>"
        "<div class='mq-list js-mq-list' id='mq-list'></div>"
        "<p class='mq-empty js-mq-empty' id='mq-empty' hidden>Nothing queued. Press Play "
        "to build a new queue.</p>"
        "</div>"
        "</div>"
        "<button class='mq-clear js-mq-clear' id='mq-clear'>"
        "Stop &amp; clear queue</button>"
        "</section>"
    )


def _agent() -> str:
    """The global language control surface; transport remains shell-owned."""
    return (
        "<section class='agent-shell'>"
        "<header class='agent-head'><span>Ask avctl "
        "<small class='agent-usage' id='agent-usage' "
        "aria-label='Estimated Fireworks session cost'>&middot; usage "
        "&asymp;$0.00</small></span>"
        "<button class='agent-new' id='agent-new' type='button'>New</button>"
        "</header>"
        "<div class='agent-log' id='agent-log' aria-live='polite'>"
        "<div class='agent-empty' id='agent-empty'>"
        "<b>What should the rack do?</b>"
        "<button type='button' data-agent-example='Play the music we added today'>"
        "Play music added today</button>"
        "<button type='button' data-agent-example='Set the amp to 55'>"
        "Set the amp to 55</button>"
        "<button type='button' data-agent-example='Search both my library and "
        "Apple Music for Kind of Blue'>"
        "Search library + Apple Music</button>"
        "</div></div>"
        "<form class='agent-compose' id='agent-form'>"
        "<button class='agent-mode' id='agent-mode' type='button' hidden "
        "aria-label='Use voice input'>"
        "<svg class='agent-mode-mic' viewBox='0 0 24 24' aria-hidden='true'>"
        "<path d='M12 15a4 4 0 0 0 4-4V7a4 4 0 0 0-8 0v4a4 4 0 0 0 4 4Z'/>"
        "<path d='M5.5 11a6.5 6.5 0 0 0 13 0M12 17.5V21M9 21h6'/></svg>"
        "<svg class='agent-mode-keyboard' viewBox='0 0 24 24' aria-hidden='true'>"
        "<rect x='3' y='6' width='18' height='12' rx='2'/><path "
        "d='M6 9h1M10 9h1M14 9h1M18 9h0M6 12h1M10 12h1M14 12h1M18 12h0M7 15h10'/></svg>"
        "</button>"
        "<textarea id='agent-input' rows='1' maxlength='2000' "
        "placeholder='Tell avctl what to do' aria-label='Ask avctl'></textarea>"
        "<button class='agent-hold' id='agent-hold' type='button' hidden>"
        "Hold to talk</button>"
        "<button type='submit' id='agent-send' aria-label='Send'>&#8593;</button>"
        "</form></section>"
    )


def _mini() -> str:
    """A phone/iPad trackpad and keyboard for the logged-in Mac session."""
    return (
        "<section class='mini-shell' id='mini-shell'>"
        "<header class='mini-head'><span>Mac mini</span>"
        "<span class='mini-state' id='mini-state'>"
        "<i aria-hidden='true'></i><span>Connecting&hellip;</span></span></header>"
        "<div class='mini-trackpad' id='mini-trackpad' role='application' "
        "aria-label='Mac mini trackpad' tabindex='0'>"
        "<span class='mini-trackpad-mark' aria-hidden='true'></span>"
        "<p><b>Move</b> with one finger</p>"
        "<small>Two fingers scroll or right-click &middot; "
        "three fingers switch Spaces</small>"
        "</div>"
        "<div class='mini-keydeck'>"
        "<div class='mini-modifiers' aria-label='Keyboard modifiers'>"
        "<button type='button' data-mini-mod='ControlLeft' aria-pressed='false'>"
        "&#8963;</button>"
        "<button type='button' data-mini-mod='AltLeft' aria-pressed='false'>"
        "&#8997;</button>"
        "<button type='button' data-mini-mod='ShiftLeft' aria-pressed='false'>"
        "&#8679;</button>"
        "<button type='button' data-mini-mod='MetaLeft' aria-pressed='false'>"
        "&#8984;</button>"
        "<button type='button' data-mini-key='Tab'>tab</button>"
        "<button type='button' data-mini-key='Backspace'>delete</button>"
        "</div>"
        "<div class='mini-arrows' aria-label='Arrow keys'>"
        "<button type='button' data-mini-key='ArrowLeft' aria-label='Left'>"
        "&#8592;</button>"
        "<button type='button' data-mini-key='ArrowDown' aria-label='Down'>"
        "&#8595;</button>"
        "<button type='button' data-mini-key='ArrowUp' aria-label='Up'>"
        "&#8593;</button>"
        "<button type='button' data-mini-key='ArrowRight' aria-label='Right'>"
        "&#8594;</button>"
        "</div>"
        "<div class='mini-typebar'>"
        "<textarea id='mini-input' rows='1' maxlength='2048' "
        "autocapitalize='sentences' "
        "autocomplete='off' spellcheck='false' enterkeyhint='enter' "
        "placeholder='Tap to type on the Mac' "
        "aria-label='Type on the Mac mini'></textarea>"
        "<button type='button' id='mini-keyboard-done' "
        "aria-label='Hide keyboard'>&#8964;</button>"
        "</div>"
        "<p class='mini-note'>The Mac must be logged in, unlocked, and grant "
        "Accessibility access to avctl Input Helper.</p>"
        "</div></section>"
    )


def transport_split_queue() -> str:
    """The permanent queue deck used only by the Split Deck appearance."""
    return (
        "<aside class='mq-split' aria-label='Up Next'>"
        "<header class='mq-split-head'><h2>Up Next</h2>"
        "<span><b class='js-mq-count'>0</b> next</span>"
        "<button class='mq-split-clear js-mq-clear' hidden "
        "data-clear-label='Stop' data-confirm-label='Confirm' "
        "aria-label='Stop playback and clear queue'>&#9632;&nbsp; Stop</button>"
        "</header>"
        "<div class='mq-split-scroll'>"
        "<div class='mq-section js-mq-playing-wrap'>"
        "<h3>Playing</h3><div class='js-mq-playing'></div></div>"
        "<div class='mq-section'><h3>Next</h3>"
        "<div class='mq-list js-mq-list'></div>"
        "<p class='mq-empty js-mq-empty' hidden>Nothing queued. Press Play "
        "to build a new queue.</p></div></div></aside>"
    )


def music_transport() -> str:
    """The mini-player: cover + track/album/artist, then the transport row.

    One instance, owned by the shell -- fixed under the rail on every
    panel. Classes rather than ids stay from its two-copy era; app.js
    paints whatever copies exist. The round button is the
    mini's own output volume: top half up, bottom half down, the number in
    the middle (no data-cmd -- app.js splits the tap on the midline). No
    repeat key by request; the command stays in the table for curl.
    """
    return (
        "<div class='mnow'>"
        "<span class='cover js-mnow-cover'></span>"
        "<span class='t'><b class='js-mnow-track'>--</b>"
        "<small class='js-mnow-sub'></small></span>"
        # The queue lives on this line rather than squeezing a fifth transport
        # key in below. It opens the dedicated Focus view; destructive clear
        # lives inside that view, away from the thumb-height transport.
        "<button class='mq js-mq' hidden aria-live='polite'>"
        "<b class='js-mq-n'>0</b><small>next&nbsp;&rsaquo;</small>"
        "</button>"
        "</div>"
        "<div class='mrow'>"
        "<div class='mkeys'>"
        + key("music.shuffle", "", glyph="&#8646;", cls="mkey")
        + key("music.prev", "", glyph="&#9646;&#9664;", cls="mkey")
        + key("music.play_pause", "", glyph="&#9654;", cls="mkey accent")
        + key("music.next", "", glyph="&#9654;&#9646;", cls="mkey")
        + "</div>"
        "<button class='mvol' aria-label='mini volume'>"
        "<span class='half'>&#43;</span>"
        "<span class='v js-mvol'>--</span>"
        "<span class='half'>&#8722;</span>"
        "</button>"
        "</div>"
    )


def music_auth() -> str:
    """The one-time MusicKit authorize page, reached from the search view.

    Deliberately not part of the remote: it loads Apple's MusicKit JS from
    their CDN (the only way a Music User Token can be minted -- there is no
    self-hosted or server-side path), runs once, and is closed. The remote
    itself stays CDN-free.
    """
    return _page(
        "avctl - authorize Apple Music",
        "<div class='app' style='padding:14px'>"
        "<div class='brand'><span>avctl</span></div>"
        "<h3>Authorize Apple Music</h3>"
        "<p class='note'>Sign in with the Apple ID whose library the mini "
        "plays. The token this mints is stored on the mini only.</p>"
        "<button class='go' id='go' disabled>waiting for MusicKit&hellip;</button>"
        "<p class='note' id='msg'></p>"
        "<script src='https://js-cdn.music.apple.com/musickit/v3/musickit.js' "
        "async></script>"
        "<script>"
        "const go=document.getElementById('go'),msg=document.getElementById('msg');"
        "function fail(t){msg.textContent=t;}"
        "document.addEventListener('musickitloaded',async()=>{"
        "try{const r=await fetch('/api/music/devtoken');"
        "const d=await r.json().catch(()=>({}));"
        "if(!r.ok){fail(d.detail||'no developer token');return;}"
        "await MusicKit.configure({developerToken:d.token,"
        "app:{name:'avctl',build:'1'}});"
        "go.disabled=false;go.textContent='Sign in with Apple Music';"
        "go.onclick=async()=>{try{"
        "const t=await MusicKit.getInstance().authorize();"
        "const s=await fetch('/api/music/usertoken',{method:'POST',"
        "headers:{'Content-Type':'application/json'},"
        "body:JSON.stringify({token:t})});"
        "msg.textContent=s.ok?'authorized -- you can close this page':"
        "'could not store the token';"
        "}catch(e){fail('authorize cancelled or failed');}};"
        "}catch(e){fail('MusicKit failed to start: '+e);}"
        "});"
        "</script>"
        "</div>",
    )


def _sheet() -> str:
    """The resync picker: the user reads the D900's front panel and we
    believe them.

    All eight inputs rather than the two in daily use, because the desync
    this fixes usually comes from someone having used the physical remote
    an unknown number of times.
    """
    from devices import registry

    buttons = "".join(
        key("dac.resync", value.upper(), args={"input": value})
        .replace("<button ", f"<button data-input='{value}' ")
        for value in registry.device("dac").inputs
    )
    return (
        "<div class='sheet' id='sheet'><div class='sheet-card'>"
        "<h3>Which input is the D900 on?</h3>"
        "<p>Read it off the front panel. Nothing is sent to the DAC -- this "
        "only corrects what the app believes, so the next switch counts the "
        "right number of presses.</p>"
        f"<div class='grid c4'>{buttons}</div>"
        "<div style='height:12px'></div>"
        "<h3>Is it on?</h3>"
        "<p>Power is a toggle, so the app has to know the current state "
        "before it can aim for one.</p>"
        "<div class='grid'>"
        + key("dac.power.resync", "It is ON", args={"on": True}, cls="accent")
          .replace("<button ", "<button data-input='power-on' ")
        + key("dac.power.resync", "It is OFF", args={"on": False}, cls="off")
          .replace("<button ", "<button data-input='power-off' ")
        + "</div>"
        "<div style='height:8px'></div>"
        "<button class='key wide' id='sheet-cancel'>Cancel</button>"
        "</div></div>"
    )


# Everything a panel is, by slug: its tab face and its renderer. ui.panels
# in config picks presence and order from THIS menu -- it cannot invent a
# panel, and home is required because the shell's screen and transport
# assume somewhere to stand.
_PANELS: dict[str, tuple[str, str, Any]] = {
    "home": ("Home", "&#8962;", _home),
    "music": ("Music", "&#9834;", _music),
    "agent": ("Ask", "&#10022;", _agent),
    "mini": ("Mini", "&#8984;", _mini),
    "tv": ("TV", "&#9635;", _tv),
    "amp": ("DAC / Amp", "&#9673;", _amp),
}

PANEL_SETTINGS_FILE = Path(
    os.environ.get("AVCTL_UI_PANELS_FILE", "~/.avctl/ui_panels.json")
).expanduser()
_PANEL_SETTINGS_LOCK = threading.Lock()


def _validate_enabled(value: Any, source: str) -> list[str]:
    if (not isinstance(value, list)
            or any(not isinstance(item, str) for item in value)):
        raise ValueError(f"{source} must be a list of panel ids")
    duplicates = len(value) != len(set(value))
    unknown = [item for item in value if item not in _PANELS]
    if unknown or duplicates:
        raise ValueError(f"{source} {value!r} -- panels that exist: "
                         f"{', '.join(_PANELS)}, each at most once")
    if "home" not in value:
        raise ValueError(f"{source} must include home")
    return list(value)


def _configured_panels() -> tuple[list[str], list[str], bool]:
    """Return full display order, enabled order, and environment ownership.

    The persisted object deliberately remembers disabled panels too. A hidden
    panel therefore keeps its place, and newly registered panels (for example
    a future DVD renderer) are appended without invalidating an older file.
    """
    from devices import config as device_config

    environment = os.environ.get("AVCTL_UI_PANELS", "").strip()
    if environment:
        enabled = _validate_enabled(
            [part.strip() for part in environment.split(",") if part.strip()],
            "AVCTL_UI_PANELS",
        )
        return enabled + [item for item in _PANELS if item not in enabled], \
            enabled, True

    try:
        raw = json.loads(PANEL_SETTINGS_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raw = None
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not read saved panel settings: {exc}") from None
    if raw is not None:
        if not isinstance(raw, dict):
            raise ValueError("saved panel settings must be an object")
        order = raw.get("order")
        enabled_raw = raw.get("enabled")
        if (not isinstance(order, list)
                or any(not isinstance(item, str) for item in order)
                or len(order) != len(set(order))):
            raise ValueError("saved panel order must contain unique panel ids")
        if (not isinstance(enabled_raw, list)
                or any(not isinstance(item, str) for item in enabled_raw)
                or len(enabled_raw) != len(set(enabled_raw))):
            raise ValueError("saved enabled panels must contain unique panel ids")
        # Ignore ids removed by a newer build, and append ids introduced by it.
        known_order = [item for item in order if item in _PANELS]
        known_order.extend(item for item in _PANELS if item not in known_order)
        enabled_set = {item for item in enabled_raw if item in _PANELS}
        if "home" not in enabled_set:
            raise ValueError("saved enabled panels must include home")
        return known_order, [item for item in known_order
                             if item in enabled_set], False

    configured = (device_config.load_config().get("ui") or {}).get("panels")
    if not configured:
        enabled = list(_PANELS)
    else:
        enabled = _validate_enabled(configured, "ui.panels")
    return enabled + [item for item in _PANELS if item not in enabled], \
        enabled, False


def panel_order() -> list[str]:
    """The active panel order, with runtime settings above config.yaml."""
    return _configured_panels()[1]


def panel_settings() -> dict[str, Any]:
    """The registry projected as safe, editable settings metadata."""
    order, enabled, managed = _configured_panels()
    enabled_set = set(enabled)
    return {
        "managed": managed,
        "panels": [
            {
                "id": slug,
                "label": _PANELS[slug][0],
                "glyph": unescape(_PANELS[slug][1]),
                "enabled": slug in enabled_set,
                "locked": slug == "home",
            }
            for slug in order
        ],
    }


def set_panel_settings(order: Any, enabled: Any) -> dict[str, Any]:
    """Atomically persist full panel order and visibility."""
    if os.environ.get("AVCTL_UI_PANELS", "").strip():
        raise ValueError("panel settings are managed by AVCTL_UI_PANELS")
    if (not isinstance(order, list)
            or any(not isinstance(item, str) for item in order)
            or len(order) != len(set(order))
            or set(order) != set(_PANELS)):
        raise ValueError("order must include every registered panel exactly once")
    active = _validate_enabled(enabled, "enabled panels")
    active_set = set(active)
    saved = {
        "order": list(order),
        "enabled": [item for item in order if item in active_set],
    }
    with _PANEL_SETTINGS_LOCK:
        try:
            PANEL_SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
            temporary = PANEL_SETTINGS_FILE.with_name(
                PANEL_SETTINGS_FILE.name + ".tmp")
            temporary.write_text(json.dumps(saved, indent=2) + "\n",
                                 encoding="utf-8")
            temporary.chmod(0o600)
            temporary.replace(PANEL_SETTINGS_FILE)
        except OSError as exc:
            raise ValueError(
                f"could not save panel settings: {exc.strerror}") from None
    return panel_settings()


def _tabs(order: list[str] | None = None) -> str:
    order = panel_order() if order is None else order
    wide = len(order) - (1 if "music" in order else 0)
    return f"<nav class='tabs' style='--tabs:{len(order)};--wide-tabs:{wide}'>" + "".join(
        f"<a class='tab' data-tab='{slug}' href='#{slug}'>"
        f"<span class='g'>{_PANELS[slug][1]}</span>{_PANELS[slug][0]}</a>"
        for slug in order
    ) + "</nav>"


def _settings_workspace() -> str:
    """An alternate root workspace, not a sixth control panel."""
    layouts = (
        ("docked", "Docked", "Library above the full transport"),
        ("stage", "Stage", "Now Playing leads Music"),
        ("cover-flow", "Cover Flow", "Swipe recently added albums"),
        ("split-deck", "Split Deck", "Library and queue share the stage"),
    )
    themes = (
        ("glass", "Glass"), ("mcintosh", "McIntosh"),
        ("porcelain", "Porcelain"), ("midnight", "Midnight"),
        ("warm", "Warm Hi-Fi"), ("contrast", "High Contrast"),
    )
    layout_buttons = "".join(
        f"<button class='setting-choice layout-choice' data-layout='{value}'>"
        f"<span class='layout-mark layout-{value}' aria-hidden='true'>"
        "<i></i><i></i><i></i></span>"
        f"<b>{label}</b><small>{detail}</small></button>"
        for value, label, detail in layouts
    )
    theme_buttons = "".join(
        f"<button class='theme-choice theme-{value}' data-theme='{value}'>"
        "<span aria-hidden='true'><i></i><i></i><i></i></span>"
        f"<b>{label}</b></button>"
        for value, label in themes
    )
    return (
        "<section class='settings-workspace' id='settings-workspace' "
        "aria-label='Settings' aria-hidden='true' inert>"
        "<nav class='settings-nav' aria-label='Settings sections'>"
        "<button class='on' data-settings-page='setup'>"
        "<span aria-hidden='true'>&#10003;</span>Setup</button>"
        "<button data-settings-page='appearance'>"
        "<span aria-hidden='true'>&#9680;</span>Appearance</button>"
        "<button data-settings-page='panels'>"
        "<span aria-hidden='true'>&#9776;</span>Panels</button>"
        "<button data-settings-page='ask'>"
        "<span aria-hidden='true'>&#10022;</span>Ask &amp; Models</button>"
        "<button data-settings-page='connection'>"
        "<span aria-hidden='true'>&#9673;</span>App</button>"
        "</nav>"
        "<div class='settings-detail'>"
        "<section class='settings-page on setup-page' data-settings-detail='setup'>"
        "<header><p>Guided setup</p><h1>Build your avctl</h1>"
        "<span>Choose the services and controls this Core should own. "
        "Discovery is read-only; nothing touches hardware until you approve "
        "a test later.</span></header>"
        "<div id='setup-root' class='setup-root'>"
        "<div class='setup-loading'>Reading this Core&hellip;</div></div>"
        "</section>"
        "<section class='settings-page' data-settings-detail='appearance'>"
        "<header><p>Display</p><h1>Appearance</h1>"
        "<span>Changes apply to every panel on this device.</span></header>"
        "<div class='setting-group'><h2>Music layout</h2>"
        f"<div class='layout-choices'>{layout_buttons}</div></div>"
        "<div class='setting-group'><h2>Color scheme</h2>"
        f"<div class='theme-choices'>{theme_buttons}</div></div>"
        "</section>"
        "<section class='settings-page' data-settings-detail='panels'>"
        "<header><p>Navigation</p><h1>Panels</h1>"
        "<span>Choose what appears in the control rail and in what order. "
        "Newly registered panels will appear here automatically.</span></header>"
        "<div class='setting-group music-backend-settings'>"
        "<div class='setting-title-row'><h2>Music backend</h2>"
        "<span class='provider-state' id='music-backend-state'>"
        "Loading&hellip;</span></div>"
        "<div class='provider-profiles' id='music-backends'></div>"
        "<div class='provider-actions'>"
        "<button class='setting-primary' id='music-backend-apply' "
        "disabled>Switch backend</button></div>"
        "<p class='setting-note'>Switching pauses the old source and clears "
        "its queue so Apple Music and Roon can never play at the same time. "
        "The choice applies to Music and Ask on every client.</p></div>"
        "<div class='setting-group panel-settings'>"
        "<div class='setting-title-row'><h2>Controls</h2>"
        "<span class='provider-state' id='panel-settings-state'>"
        "Loading&hellip;</span></div>"
        "<div class='panel-setting-list' id='panel-setting-list'></div>"
        "<div class='provider-actions'>"
        "<button class='setting-primary' id='panel-settings-apply' "
        "disabled>Apply &amp; reload</button></div>"
        "<p class='setting-note'>Home always remains available. Hidden "
        "panels keep their position so they return where you left them.</p>"
        "</div></section>"
        "<section class='settings-page' data-settings-detail='ask'>"
        "<header><p>Assistant</p><h1>Ask &amp; Models</h1>"
        "<span>Choose where Ask runs. The next conversation turn uses the "
        "selected profile.</span></header>"
        "<div class='setting-group'><div class='setting-title-row'>"
        "<h2>Provider profile</h2><span class='provider-state' "
        "id='provider-state'>Loading&hellip;</span></div>"
        "<div class='provider-profiles' id='provider-profiles'></div>"
        "<div class='provider-actions'>"
        "<button class='setting-secondary' id='provider-test'>"
        "Test connection</button>"
        "<button class='setting-primary' id='provider-apply'>Apply</button>"
        "</div><p class='setting-note'>Endpoints, models, and credential "
        "sources are defined on the Mac mini. Secret contents are never sent "
        "to this panel.</p></div>"
        "</section>"
        "<section class='settings-page' data-settings-detail='connection'>"
        "<header><p>This device</p><h1>App Connection</h1>"
        "<span>Device-local connection and artwork cache settings.</span></header>"
        "<div class='setting-group native-settings' id='native-settings'>"
        "<h2>Server</h2><label class='setting-field'>"
        "<span>avctl address</span><input id='native-server' type='url' "
        "inputmode='url' autocapitalize='off' autocomplete='off' "
        "spellcheck='false' placeholder='https://…'></label>"
        "<label class='setting-field'><span>Bearer token</span>"
        "<input id='native-token' type='password' autocapitalize='off' "
        "autocomplete='off' spellcheck='false' "
        "placeholder='Leave blank to keep the current token'></label>"
        "<div class='provider-actions connection-actions'>"
        "<button class='setting-secondary' id='native-token-clear'>"
        "Clear token</button><button class='setting-primary' "
        "id='native-settings-save'>Save</button></div>"
        "<p class='setting-note' id='native-settings-state'>"
        "Reading this device&hellip;</p></div>"
        "<div class='setting-group artwork-settings'><h2>Artwork cache</h2>"
        "<div class='setting-readout'><span>Covers cached</span>"
        "<b id='native-cover-count'>--</b></div>"
        "<p class='setting-note' id='native-cover-note'>"
        "The island can only draw artwork already on this device.</p>"
        "<button class='setting-secondary' id='native-cover-warm'>"
        "Warm cover cache now</button></div>"
        "</section></div></section>"
    )


# --- pages ---------------------------------------------------------------


def gate(message: str | None = None) -> str:
    """Shown when the caller has not proved an identity.

    A token box rather than a bare 401, because getting a secret into a phone
    browser otherwise means typing a long URL by hand.
    """
    warning = f"{escape(message)}<br>" if message else ""
    return _page(
        "avctl",
        "<div class='app' style='padding-bottom:14px'>"
        "<div class='brand'><span>avctl</span><span class='led'></span>"
        "<span class='who'>locked</span></div>"
        "<div class='screen'><div class='screen-head'><span>locked</span></div>"
        f"<div class='ticker' style='border:0;white-space:normal'>{warning}"
        "not reachable from the internet.<br>paste the access token to continue."
        "</div></div>"
        "<form class='gate' method='post' action='/'>"
        "<input name='token' type='text' autocomplete='off' autocapitalize='off' "
        "autocorrect='off' spellcheck='false' placeholder='access token'>"
        "<button class='go' type='submit'>Unlock</button>"
        "<code>~/.avctl/token on the mini</code>"
        "</form></div>",
    )


def remote(identity: Identity) -> str:
    """The remote: a screen, configured panels, and thumb-reachable tabs."""
    order = panel_order()
    app_class = "app" + (" no-music-panel" if "music" not in order else "")
    return _page(
        "avctl",
        f"<div class='{app_class}'>"
        "<div class='brand'><span id='brand-title'>avctl</span>"
        "<span class='led' id='led'></span><span class='brand-spacer'></span>"
        "<button class='iconbtn settings-toggle' id='settings-toggle' "
        "aria-label='Open settings' aria-expanded='false'>"
        "<span class='settings-gear' aria-hidden='true'>&#9881;</span>"
        "<span class='settings-back' aria-hidden='true'>&#8617;</span></button>"
        # Re-reading every device is a chore key, not a control: it belongs up
        # here out of the way, not taking a slot on the home panel.
        "<button class='iconbtn brand-refresh' data-cmd='scene.resync' "
        "aria-label='re-read devices'>&#8635;</button></div>"
        + _screen()
        # The rail. Panels sit side by side and are swiped between; the tabs
        # below are for jumping and for saying which one you are on. Both
        # are rendered from panel_order(), so they cannot disagree -- the
        # tab highlight is derived from scroll position by index.
        + "<div class='rail-deck'>"
        + "<div class='rail-stack' id='rail-stack'>"
        + "<div class='rail' id='rail'>"
        + "".join(f"<section class='page' id='page-{slug}'>"
                  f"{_PANELS[slug][2]()}</section>"
                  for slug in order)
        + "</div>"
        + transport_queue()
        + "</div>"
        + transport_split_queue()
        + "</div>"
        # The transport bar is the shell's, like the screen: panels swipe
        # between them and neither moves. Music is the thing this rack is
        # for, so its controls deserve to be everywhere.
        + "<div class='mbar'>" + music_transport() + "</div>"
        + _tabs(order)
        + _settings_workspace()
        + "</div>"
        + "<div class='toast' id='toast'></div>"
        + _sheet(),
        script=True,
    )
