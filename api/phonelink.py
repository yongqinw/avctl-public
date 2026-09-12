"""Live Activity pushes: the island outlives the app.

The app can only feed the island while it is on screen (ActivityDriver, stage
2 of docs/phone-app.md). This module is stage 4: the mini watches its own
poller and drives the island over APNs -- starts it when Music starts,
advances it on every change, ends it when the rack goes quiet. The route is
mini -> Apple -> phone over the open internet, so none of it needs the
tailnet; only button presses do.

Inert without a `phone:` block in config.yaml (the APNs key facts). The
register route still answers -- tokens are worth keeping before the key
exists -- but pushes simply have nowhere to go.

Two clocks, because Apple uses two:
  * aps-level dates (timestamp, stale-date, dismissal-date) are unix epoch.
  * content-state Date fields cross into Swift's default JSONDecoder, whose
    Date is seconds since 2001-01-01 (the "reference date"). Send unix and
    every date lands 31 years late.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import jwt

from devices import config as device_config
from devices.apple_broker import BrokerError, ManagedAppleBroker

from . import state

REFERENCE_EPOCH = 978307200          # 2001-01-01 in unix seconds
TOKENS_FILE = Path("~/.avctl/phone.json").expanduser()
PUSH_LOG = Path("~/.avctl/phone-push.log").expanduser()


def _log(line: str) -> None:
    """One line per push attempt. The island cannot say why it went quiet;
    this file can."""
    try:
        with PUSH_LOG.open("a", encoding="utf-8") as fh:
            fh.write(time.strftime("%Y-%m-%d %H:%M:%S ") + line + "\n")
    except OSError:
        pass

# APNs auth tokens must be reused for at least 20 minutes and replaced within
# an hour. 50 stays clear of both edges.
_JWT_LIFETIME = 50 * 60

# Apple evicts a Live Activity from the island after ~8 hours, no appeal.
# Re-issue at 7h30m so the blink happens on our schedule, never Apple's.
REISSUE_AFTER = 7.5 * 3600

_lock = threading.Lock()
_jwt_cache: tuple[str, float] | None = None
_thread: threading.Thread | None = None
_stop = threading.Event()
_client = None   # the process's one APNs connection, built on first push


def _config() -> dict[str, Any]:
    try:
        return device_config.load_config().get("phone") or {}
    except FileNotFoundError:
        return {}


def island_volume_style() -> str:
    """Which volume control the island draws; see config.yaml phone block."""
    return str(_config().get("island_volume", "c"))


def configured() -> bool:
    services = device_config.load_config().get("apple_services") or {}
    mode = str(services.get("mode") or "local")
    if mode == "disabled":
        return False
    if mode == "managed":
        try:
            return ManagedAppleBroker.from_config(
                device_config.load_config()) is not None
        except BrokerError:
            return False
    cfg = _config()
    return bool(cfg.get("team_id") and cfg.get("key_id")
                and cfg.get("key_file") and cfg.get("bundle_id"))


# --- who to push to --------------------------------------------------------

def _load_tokens() -> dict[str, Any]:
    try:
        tokens = json.loads(TOKENS_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, UnicodeError):
        return {"push_to_start": [], "activities": {}, "widgets": []}
    if not isinstance(tokens, dict):
        return {"push_to_start": [], "activities": {}, "widgets": []}

    push_to_start = tokens.get("push_to_start")
    widgets = tokens.get("widgets")
    activities = tokens.get("activities")
    tokens = {
        "push_to_start": ([token for token in push_to_start
                           if isinstance(token, str) and token]
                          if isinstance(push_to_start, list) else []),
        "activities": activities if isinstance(activities, dict) else {},
        "widgets": ([token for token in widgets
                     if isinstance(token, str) and token]
                    if isinstance(widgets, list) else []),
    }
    # Migration: the widget-reload channel (iOS 26) arrived after files were
    # first written.
    tokens.setdefault("widgets", [])
    # Migration: activity records grew a birth time for the 8-hour
    # re-issue. A bare token string from the old shape gets stamped "born
    # now" -- wrong by up to its true age, but self-correcting within one
    # re-issue cycle and never a spurious blink at boot.
    for activity_id, record in list(tokens["activities"].items()):
        if isinstance(record, str):
            tokens["activities"][activity_id] = {
                "token": record, "since": time.time()}
        elif (not isinstance(record, dict)
              or not isinstance(record.get("token"), str)
              or not record["token"]):
            del tokens["activities"][activity_id]
        elif not isinstance(record.get("since"), (int, float)):
            record["since"] = time.time()
    return tokens


def _save_tokens(tokens: dict[str, Any]) -> None:
    TOKENS_FILE.parent.mkdir(parents=True, exist_ok=True)
    handle, temp = tempfile.mkstemp(dir=TOKENS_FILE.parent,
                                    prefix=".phone-")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(tokens, indent=2) + "\n")
        os.replace(temp, TOKENS_FILE)
    except OSError:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


def register(kind: str, token: str, activity_id: str | None = None) -> None:
    """The app hands over every token the system mints it.

    push_to_start tokens are per-install and rare; activity tokens are
    per-activity and churn. Both survive restarts on disk -- a reboot of the
    mini must not orphan the island.
    """
    if kind not in ("push_to_start", "activity", "widget"):
        raise ValueError(f"unknown token kind: {kind}")
    if not token:
        raise ValueError("empty token")
    stored_token = token
    try:
        broker = ManagedAppleBroker.from_config(device_config.load_config())
        if broker is not None:
            services = device_config.load_config().get("apple_services") or {}
            broker_config = services.get("broker") or {}
            environment = str(broker_config.get("apns_environment") or
                              _config().get("environment") or "sandbox")
            stored_token = "broker:" + broker.register_device(
                kind, token, activity_id, environment)
    except BrokerError as exc:
        raise ValueError(f"could not register notifications: {exc}") from None
    with _lock:
        tokens = _load_tokens()
        if kind == "push_to_start":
            if stored_token not in tokens["push_to_start"]:
                tokens["push_to_start"].append(stored_token)
        elif kind == "widget":
            if stored_token not in tokens["widgets"]:
                tokens["widgets"].append(stored_token)
        else:
            key = activity_id or stored_token
            since = (tokens["activities"].get(key) or {}).get("since",
                                                             time.time())
            tokens["activities"][key] = {"token": stored_token, "since": since}
        _save_tokens(tokens)


def _drop_activity(activity_id: str) -> None:
    with _lock:
        tokens = _load_tokens()
        if tokens["activities"].pop(activity_id, None) is not None:
            _save_tokens(tokens)


def _drop_push_to_start(token: str) -> None:
    """APNs answered 410 for this install: it is gone (app deleted, token
    rotated). Kept, it would be re-pushed -- and re-refused -- on every
    silence-becomes-music transition, forever."""
    with _lock:
        tokens = _load_tokens()
        if token in tokens["push_to_start"]:
            tokens["push_to_start"].remove(token)
            _save_tokens(tokens)


def _drop_widget(token: str) -> None:
    with _lock:
        tokens = _load_tokens()
        if token in tokens["widgets"]:
            tokens["widgets"].remove(token)
            _save_tokens(tokens)


# --- talking to APNs -------------------------------------------------------

def _auth_token(cfg: dict[str, Any]) -> str:
    global _jwt_cache
    now = time.time()
    if _jwt_cache and now - _jwt_cache[1] < _JWT_LIFETIME:
        return _jwt_cache[0]
    key = Path(cfg["key_file"]).expanduser().read_text(encoding="utf-8")
    token = jwt.encode({"iss": cfg["team_id"], "iat": int(now)}, key,
                       algorithm="ES256",
                       headers={"kid": cfg["key_id"]})
    _jwt_cache = (token, now)
    return token


def _host(cfg: dict[str, Any]) -> str:
    # Xcode-installed builds carry aps-environment: development, which is
    # the sandbox. TestFlight/ad-hoc builds would flip this to production.
    if cfg.get("environment", "sandbox") == "sandbox":
        return "https://api.sandbox.push.apple.com"
    return "https://api.push.apple.com"


def _apns_client():
    """One long-lived HTTP/2 client for every push (#102).

    Apple asks providers to hold APNs connections open and reads rapid
    connect/disconnect as throttle-worthy behaviour; a client per push paid
    a full TCP+TLS+HTTP/2 handshake per island update and leaked the old
    connection until GC.
    """
    global _client
    import httpx  # deferred: only needed once a phone: block exists

    with _lock:
        if _client is None:
            _client = httpx.Client(http2=True, timeout=10)
        return _client


def _send(device_token: str, payload: dict[str, Any]) -> int:
    """One push. Returns the HTTP status; 410 means the token is dead."""
    cfg = _config()
    event = payload.get("aps", {}).get("event", "?")
    if device_token.startswith("broker:"):
        try:
            broker = ManagedAppleBroker.from_config(device_config.load_config())
            if broker is None:
                raise BrokerError("managed Apple services are disabled")
            status, reason = broker.push(
                device_token.removeprefix("broker:"), "liveactivity", payload)
        except BrokerError as exc:
            _log(f"{event} -> BROKER EXC {exc!r}")
            raise
        detail = "" if status == 200 else f" {reason or 'rejected'}"
        _log(f"{event} -> {status}{detail} broker ref …{device_token[-8:]}")
        return status
    try:
        response = _apns_client().post(
            f"{_host(cfg)}/3/device/{device_token}",
            json=payload,
            headers={
                "authorization": f"bearer {_auth_token(cfg)}",
                "apns-topic": f"{cfg['bundle_id']}.push-type.liveactivity",
                "apns-push-type": "liveactivity",
                "apns-priority": "10",
            },
        )
    except Exception as exc:
        _log(f"{event} -> EXC {exc!r} token …{device_token[-8:]}")
        raise
    detail = "" if response.status_code == 200 else f" {response.text.strip()}"
    _log(f"{event} -> {response.status_code}{detail} token …{device_token[-8:]}")
    return response.status_code


def _send_widget_reload(device_token: str) -> int:
    """One widget-reload nudge (iOS 26). No content rides along -- the push
    tells WidgetKit to run the provider, which fetches /api/state itself.
    Returns the HTTP status; 410 means the token is dead."""
    cfg = _config()
    if device_token.startswith("broker:"):
        try:
            broker = ManagedAppleBroker.from_config(device_config.load_config())
            if broker is None:
                raise BrokerError("managed Apple services are disabled")
            status, reason = broker.push(
                device_token.removeprefix("broker:"), "widget",
                {"aps": {"content-changed": True}})
        except BrokerError as exc:
            _log(f"widget -> BROKER EXC {exc!r}")
            raise
        detail = "" if status == 200 else f" {reason or 'rejected'}"
        _log(f"widget -> {status}{detail} broker ref …{device_token[-8:]}")
        return status
    try:
        response = _apns_client().post(
            f"{_host(cfg)}/3/device/{device_token}",
            json={"aps": {"content-changed": True}},
            headers={
                "authorization": f"bearer {_auth_token(cfg)}",
                "apns-topic": f"{cfg['bundle_id']}.push-type.widgets",
                "apns-push-type": "widgets",
                # 5, not 10: high-priority widget pushes drain the widget's
                # daily reload budget far faster, and a home-screen tile can
                # afford opportunistic delivery.
                "apns-priority": "5",
            },
        )
    except Exception as exc:
        _log(f"widget -> EXC {exc!r} token …{device_token[-8:]}")
        raise
    detail = "" if response.status_code == 200 else f" {response.text.strip()}"
    _log(f"widget -> {response.status_code}{detail} token …{device_token[-8:]}")
    return response.status_code


# What the home widget actually renders, beyond the player: the rack strip.
# A reload is only worth budget when one of these moved -- volume nudges and
# progress are already covered (guess repaint / the widget's own clock).
_widget_sig: tuple | None = None


def _widget_signature(snap: dict[str, Any],
                      cs: dict[str, Any] | None) -> tuple:
    devices = snap.get("devices") or {}

    def field(dev: str, key: str) -> Any:
        return ((devices.get(dev) or {}).get("fields") or {}).get(key)

    return (
        snap.get("scene"),
        (devices.get("tv") or {}).get("power"),
        (devices.get("amp") or {}).get("power"),
        field("dac", "input"),
        None if cs is None else (cs.get("track"), cs.get("playState")),
    )


def _nudge_widgets(snap: dict[str, Any], cs: dict[str, Any] | None,
                   tokens: dict[str, Any]) -> None:
    """Reload the home widget when something it shows changed (#126).

    This is what keeps the widget honest while the app is suspended: the
    island rides liveactivity pushes, but a widget only refreshes on its
    own budgeted schedule unless the server nudges it.
    """
    global _widget_sig
    sig = _widget_signature(snap, cs)
    if sig == _widget_sig:
        return
    _widget_sig = sig
    for token in list(tokens.get("widgets", [])):
        if _send_widget_reload(token) == 410:
            _drop_widget(token)


# --- what the island is told ----------------------------------------------

def content_state(snap: dict[str, Any],
                  now: float | None = None) -> dict[str, Any] | None:
    """The push twin of the app's NowPlayingAttributes.ContentState.

    None means "no island should exist": Music stopped, quit, or unreadable.
    Field names must match the Swift Codable exactly -- this dict crosses
    into ActivityKit's decoder untranslated.
    """
    music = snap["devices"]["music"]["fields"]
    amp = snap["devices"]["amp"]["fields"]
    if music.get("state") not in ("playing", "paused"):
        return None
    cs: dict[str, Any] = {
        "track": music.get("track") or "—",
        "artist": music.get("artist") or "",
        "album": music.get("album") or "",
        "playState": music["state"],
        # Both knobs ride along: the mini's system output (what
        # music.vol.up/down nudge) and the amp's, for the twin volume strip.
        "volume": music.get("volume"),
        "muted": bool(music.get("muted")),
        "ampVolume": amp.get("volume"),
        "ampMuted": bool(amp.get("muted")),
        # Which volume control the island draws ("c+" fader / "c" plain);
        # riding the content state means a config edit reskins the island
        # on the next change, no app rebuild.
        "volumeStyle": _config().get("island_volume", "c"),
        "failed": False,
    }
    if music.get("pid"):
        # The cover key for the phone's ArtworkStore. Push can only name it;
        # whether the cover shows depends on what the phone has cached --
        # the widget process may read disk but never the network.
        cs["artworkKey"] = str(music["pid"])
    position, duration = music.get("position"), music.get("duration")
    if position is not None and duration:
        start = (time.time() if now is None else now) - position
        cs["trackStart"] = start - REFERENCE_EPOCH
        cs["trackEnd"] = start + duration - REFERENCE_EPOCH
    return cs


# ActivityKit discards an update whose timestamp is not newer than the last
# one it applied. Whole-second stamps collide when two polls land inside a
# second -- the second push (a pause chasing a volume nudge, say) silently
# vanishes. So the stamp is forced strictly increasing.
_last_ts = 0


def _aps(event: str, cs: dict[str, Any] | None, cfg: dict[str, Any],
         now: float) -> dict[str, Any]:
    global _last_ts
    ts = max(int(now), _last_ts + 1)
    _last_ts = ts
    aps: dict[str, Any] = {"timestamp": ts, "event": event}
    if cs is not None:
        aps["content-state"] = cs
        aps["stale-date"] = ts + 90
    if event == "start":
        aps["attributes-type"] = "NowPlayingAttributes"
        aps["attributes"] = {"server": cfg.get("server", "")}
    if event == "end":
        # Linger half a minute on the lock screen, then clean up.
        aps["dismissal-date"] = ts + 30
    return aps


# --- the feeder ------------------------------------------------------------

def handle_snapshot(snap: dict[str, Any], last_cs: dict[str, Any] | None,
                    now: float | None = None) -> dict[str, Any] | None:
    """One poll landed; tell every registered phone what changed.

    Returns the content-state that was current after this call, which the
    caller feeds back as `last_cs` -- comparing against it is what keeps a
    quiet rack from costing a push per poll. The progress dates are excluded
    from that comparison: they move every second by construction, and the
    island advances them itself.
    """
    now = time.time() if now is None else now
    cfg = _config()
    try:
        cs = content_state(snap, now)
    except (KeyError, TypeError) as exc:
        # A snapshot without the expected shape (version skew during a
        # deploy, a partial poll) is "cannot say", never "stopped" -- it
        # must not tear the island down, and it crash-looped the feeder in
        # the 2026-08-09 push log. Skip the beat and keep last_cs.
        _log(f"skipped malformed snapshot: {exc!r}")
        return last_cs
    with _lock:
        tokens = _load_tokens()

    # Before any island decision: the home widget shows the rack strip too,
    # so it cares about changes (TV power, a scene landing) that never touch
    # the island's content state (#126).
    _nudge_widgets(snap, cs, tokens)

    def same(a: dict[str, Any] | None, b: dict[str, Any] | None) -> bool:
        strip = ("trackStart", "trackEnd")
        if a is None or b is None:
            return a is b
        return ({k: v for k, v in a.items() if k not in strip}
                == {k: v for k, v in b.items() if k not in strip})

    if cs is None:
        if tokens["activities"]:
            for activity_id, record in list(tokens["activities"].items()):
                _send(record["token"], {"aps": _aps("end", last_cs, cfg, now)})
                _drop_activity(activity_id)
        return None

    # The 8-hour re-issue (#89): Apple evicts a Live Activity from the
    # island after ~8h no matter what. The mini knows the music is still
    # going, so a long-lived activity is ended and a fresh one raised --
    # a marathon session blinks once instead of losing its island. 7h30m
    # keeps the blink on our schedule, never Apple's.
    expired = [aid for aid, rec in tokens["activities"].items()
               if now - rec.get("since", now) > REISSUE_AFTER]
    if expired:
        for activity_id in expired:
            record = tokens["activities"][activity_id]
            _log(f"re-issue: activity {activity_id[:8]} is "
                 f"{(now - record['since']) / 3600:.1f}h old")
            _send(record["token"], {"aps": _aps("end", cs, cfg, now)})
            _drop_activity(activity_id)
        with _lock:
            tokens = _load_tokens()
        if not tokens["activities"]:
            for token in tokens["push_to_start"]:
                if _send(token, {"aps": _aps("start", cs, cfg, now)}) == 410:
                    _drop_push_to_start(token)
        return cs

    if tokens["activities"]:
        if not same(cs, last_cs):
            for activity_id, record in list(tokens["activities"].items()):
                if _send(record["token"],
                         {"aps": _aps("update", cs, cfg, now)}) == 410:
                    _drop_activity(activity_id)
    elif last_cs is None:
        # Silence just became music and no island exists anywhere: raise one
        # on every install that gave us a push-to-start token.
        for token in tokens["push_to_start"]:
            if _send(token, {"aps": _aps("start", cs, cfg, now)}) == 410:
                _drop_push_to_start(token)
    return cs


def _run(stop: threading.Event) -> None:
    seen = -1
    last_cs: dict[str, Any] | None = None
    while not stop.is_set():
        snap, seq = state.wait_for_change(seen, timeout=30)
        if snap is None or seq == seen:
            continue
        seen = seq
        # Coalesce before pushing: a finger riding the volume key lands a
        # poll-change per nudge, and pushing each one makes the island
        # flicker through every intermediate value while spending APNs
        # budget. Wait for the rack to hold still for a breath and push
        # where it settled; a lone change (a pause) waits that breath once.
        while not stop.is_set():
            more, seq2 = state.wait_for_change(seen, timeout=1.2)
            if more is None or seq2 == seen:
                break
            snap, seen = more, seq2
        try:
            last_cs = handle_snapshot(snap, last_cs)
        except Exception as exc:
            # A push that failed is a push that failed; the next change
            # tries again. The island's staleDate is the user-facing signal.
            _log(f"feeder error: {exc!r}")


def start_feeder() -> None:
    global _thread, _stop
    if not configured():
        return
    if _thread is not None and _thread.is_alive():
        return
    # A FRESH Event per thread (#103): the old shared one meant a restart's
    # clear() could resurrect a previous thread still blocked in
    # wait_for_change -- two feeders pushing forever. Each thread only ever
    # observes the event it was born with.
    stop = threading.Event()
    _stop = stop
    _thread = threading.Thread(target=_run, args=(stop,),
                               name="phone-push", daemon=True)
    _thread.start()


def stop_feeder() -> None:
    global _thread
    _stop.set()
    if _thread is not None:
        _thread.join(timeout=5)
        # A thread still alive after the join budget keeps its handle, so a
        # later start_feeder sees it and refuses to double up; its own stop
        # event stays set, so it exits at its next check regardless.
        if not _thread.is_alive():
            _thread = None
