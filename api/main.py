"""Routes.

Two surfaces over the same data: HTML for the phone, JSON for scripts and for
whatever the scene runner needs later. Both go through the same auth
dependency, so there is one place where "who is this" is decided.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import threading
import time
from collections import deque
from contextlib import asynccontextmanager
from html import escape
from dataclasses import asdict
from urllib.parse import parse_qs, urlsplit

from pathlib import Path

from fastapi import (BackgroundTasks, Body, Depends, FastAPI, HTTPException,
                     Query, Request, Response, WebSocket, WebSocketDisconnect)
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               RedirectResponse, StreamingResponse)
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

from devices import registry

from . import (agent_providers, agentlink, bonjour, commands, files, minilink,
               musiclink, phonelink, pwa, settings,
               setup as setup_service, state, views, voicelink)
from .auth import COOKIE_NAME, Identity, identify, load_token, trusts_identity_headers
from devices.music import MEDIA_ID_RE, MusicError

UI_DIR = Path(__file__).resolve().parent / "ui"

# A whitelist rather than a static mount: this app is reachable from a phone
# over the tailnet, and "serve whatever is in this directory" is a category of
# bug worth simply not having.
ASSETS = {
    "app.css": "text/css; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
}


class _SensitiveAccessLogFilter(logging.Filter):
    """Redact legacy URL credentials before uvicorn formats an access line."""

    _TOKEN_QUERY = re.compile(r"([?&]token=)[^&\s\"]+", re.IGNORECASE)

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(
                self._TOKEN_QUERY.sub(r"\1[REDACTED]", value)
                if isinstance(value, str) else value
                for value in record.args
            )
        if isinstance(record.msg, str):
            record.msg = self._TOKEN_QUERY.sub(
                r"\1[REDACTED]", record.msg)
        return True


logging.getLogger("uvicorn.access").addFilter(_SensitiveAccessLogFilter())


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Flush because stdout is block-buffered once redirected to a service log.
    # Never print the credential itself; the token file is the retrieval path.
    trusted = "trusted" if trusts_identity_headers() else "IGNORED (bind is not loopback)"
    print(f"  root            {settings.ROOT}", flush=True)
    print(f"  identity hdrs   {trusted}", flush=True)
    load_token()  # Preserve first-boot token creation without logging its value.
    print("  token           configured", flush=True)
    print(f"  token file      {settings.TOKEN_FILE}", flush=True)
    # Resolve every configured driver and the ui config NOW: a typo'd
    # class name or scene step must stop the boot with its reason, not
    # surface as a 502 at 2am.
    for line in registry.validate():
        print(f"  driver          {line}", flush=True)
    print(f"  panels          {' '.join(views.panel_order())}", flush=True)
    print("  scenes          "
          + ", ".join(s["id"] for s in commands.scenes()), flush=True)
    active_agent = agent_providers.active_profile()
    print(f"  agent           {active_agent.name} · {active_agent.driver} · "
          f"{active_agent.model}", flush=True)
    # The rack is read by one background beat, not by whoever's phone asks --
    # see state.start_poller. Started here so importing this module (tests,
    # scripts) never touches a device.
    state.start_poller()
    # Island pushes ride the same poller; without a phone: block in
    # config.yaml this is a no-op and the line below says so.
    phonelink.start_feeder()
    print("  island pushes   "
          + ("on" if phonelink.configured() else
             "inert (no phone: block in config.yaml)"), flush=True)
    agentlink.warmup()
    voicelink.warmup()
    # iPhone and iPad may both keep the Mini panel open. Their input is one
    # ordered stream at the helper boundary rather than a winner-takes-all
    # controller lease.
    app.state.mini_input_lock = asyncio.Lock()
    app.state.mini_input_session = None
    app.state.mini_input_members = {}
    app.state.core_advertisement = bonjour.CoreAdvertisement()
    advertised = app.state.core_advertisement.start()
    print("  bonjour         " + ("_avctl._tcp" if advertised else "off"),
          flush=True)
    yield
    setup_service.cancel_roon_authorization()
    app.state.core_advertisement.stop()
    phonelink.stop_feeder()
    state.stop_poller()


app = FastAPI(title="avctl", docs_url=None, redoc_url=None, lifespan=lifespan)


def require(request: Request) -> Identity:
    identity = identify(request)
    if identity is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    return identity


def _same_origin(socket: WebSocket) -> bool:
    """Reject cross-site WebSocket use of an otherwise valid auth cookie."""
    origin = socket.headers.get("origin", "")
    host = socket.headers.get("host", "")
    if not origin or not host:
        return False
    try:
        return secrets.compare_digest(urlsplit(origin).netloc.lower(),
                                      host.lower())
    except ValueError:
        return False


async def _send_mini_event(application: FastAPI, session: minilink.MiniSession,
                           event: dict) -> None:
    """The one serialized write door shared by every remote controller."""
    async with application.state.mini_input_lock:
        if application.state.mini_input_session is not session:
            raise minilink.MiniUnavailable("Mac input helper disconnected")
        await session.send(event)


def _set_token_cookie(response: Response, request: Request, token: str) -> None:
    """Marked secure only on HTTPS: behind `tailscale serve` it always will
    be, but during local http testing a secure cookie would silently never
    be set."""
    response.set_cookie(
        COOKIE_NAME,
        token,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        max_age=60 * 60 * 24 * 90,
    )


def _remember(response: Response, request: Request) -> None:
    """Turn a ?token= visit into a cookie, so the phone only does it once.

    Only a token that actually verifies is remembered: identity may have come
    from Tailscale headers, in which case a stale bookmarked ?token= would
    otherwise overwrite a perfectly good cookie with garbage (#97).
    """
    token = request.query_params.get("token")
    if not token or not secrets.compare_digest(token, load_token()):
        return
    _set_token_cookie(response, request, token)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    """Unauthenticated liveness only -- deliberately says nothing else."""
    return {"status": "ok"}


@app.get("/setup")
@app.get("/bootstrap")
def local_bootstrap(request: Request) -> RedirectResponse:
    """Open packaged first-run setup without putting the owner token in a URL."""
    client = request.client.host if request.client else ""
    host = (request.url.hostname or "").lower()
    forwarded = request.headers.get("forwarded") or request.headers.get(
        "x-forwarded-for")
    if (settings.INSTALL_KIND != "package" or host not in {"127.0.0.1", "localhost"}
            or client not in {"127.0.0.1", "::1"} or forwarded):
        raise HTTPException(status_code=404, detail="not found")
    response = RedirectResponse("/?setup=1", status_code=303)
    _set_token_cookie(response, request, load_token())
    return response


@app.get("/whoami")
def whoami(request: Request) -> JSONResponse:
    """What the app believes about the caller, and why.

    Unauthenticated on purpose: this is the endpoint that tells you whether
    `tailscale serve` is actually injecting identity headers, and it is far
    less useful for that if you must already be authenticated to read it. It
    reports only what the caller themselves sent.
    """
    identity = identify(request)
    return JSONResponse(
        {
            "authenticated": identity is not None,
            "who": identity.who if identity else None,
            "method": identity.method if identity else None,
            "trusts_identity_headers": trusts_identity_headers(),
            "bind": settings.BIND,
            "tailscale_headers": {
                key: value
                for key, value in request.headers.items()
                if key.lower().startswith("tailscale-")
            },
        }
    )


@app.get("/api/ls")
def api_ls(
    path: str = Query("", description="directory, relative to the configured root"),
    identity: Identity = Depends(require),
) -> JSONResponse:
    try:
        current, entries = files.listing(path)
    except files.OutsideRoot:
        raise HTTPException(status_code=403, detail="path outside root")
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="no such directory")
    except NotADirectoryError:
        raise HTTPException(status_code=400, detail="not a directory")
    except PermissionError:
        raise HTTPException(status_code=403, detail="permission denied")

    return JSONResponse(
        {
            "root": str(settings.ROOT),
            "path": current,
            "count": len(entries),
            "entries": [asdict(entry) for entry in entries],
        }
    )


@app.get("/api/state")
def api_state(identity: Identity = Depends(require)) -> JSONResponse:
    """Everything the screen shows, in one request.

    One endpoint rather than one per device because the phone paints the whole
    screen at once, and four round trips over a tailnet on cellular is four
    chances for the readouts to disagree with each other.

    Served from the poller's cache: the devices already answered these
    questions this beat, and a second phone asking must not cost the TV a
    second SSAP probe. `age` says how old the answers are, so a client that
    cares can tell a fresh readout from a held one.
    """
    snap, seq, age = state.latest()
    if snap is None:
        # The poller has not landed its first read yet (service just booted).
        # Wait for ITS probe rather than running our own: two phones asking
        # at once must cost one rack probe, not three (#94).
        snap, seq = state.wait_for_change(0, timeout=5.0)
        age = 0.0
    if snap is None:
        # Still warming up (a device is slow or offline). Serve the diary
        # with an honest null age instead of probing from the request thread.
        snap = state.last_known()
        age = None
    if snap is None:
        # Fresh install, nothing persisted yet: the one case a request-side
        # probe is still the least-bad answer.
        snap, age = state.snapshot(), 0.0
        seq = 0
    payload = dict(snap)
    payload["implemented"] = commands.implemented()
    payload["island_volume"] = phonelink.island_volume_style()
    payload["age"] = None if age is None else round(age, 1)
    if seq:
        # The poller's sequence number, same meaning as on the SSE stream:
        # the client drops whichever of the two paths answers stale (#105).
        payload["seq"] = seq
    return JSONResponse(payload)


@app.websocket("/api/mini/input")
async def api_mini_input(socket: WebSocket) -> None:
    """Relay one authenticated, ordered controller stream to the Mac helper.

    There is deliberately no request-body logging on this path: keyboard text
    is transient input, not application data. iPhone and iPad streams share a
    server lock so the helper always receives complete events in one order.
    """
    if identify(socket) is None or not _same_origin(socket):
        await socket.close(code=1008, reason="not authenticated")
        return

    controller = secrets.token_urlsafe(12)
    session = None
    joined = False
    try:
        try:
            async with socket.app.state.mini_input_lock:
                session = socket.app.state.mini_input_session
                if session is None:
                    session = await minilink.open_session()
                    socket.app.state.mini_input_session = session
                status = dict(session.status)
                if status.get("permission") is True:
                    socket.app.state.mini_input_members[controller] = session
                    joined = True
        except minilink.MiniUnavailable as exc:
            await socket.accept()
            await socket.send_json(minilink.unavailable_status(str(exc)))
            await socket.close(code=1013, reason="helper unavailable")
            return

        await socket.accept()
        await socket.send_json(status)
        if status.get("permission") is not True:
            async with socket.app.state.mini_input_lock:
                if socket.app.state.mini_input_session is session:
                    socket.app.state.mini_input_session = None
                await session.close()
            await socket.close(code=1013, reason="accessibility permission required")
            return
        recent: deque[float] = deque()
        while True:
            raw = await socket.receive_text()
            if len(raw.encode("utf-8")) > minilink.MAX_WIRE_BYTES:
                await socket.close(code=1009, reason="event too large")
                return
            now = time.monotonic()
            while recent and recent[0] < now - 1.0:
                recent.popleft()
            recent.append(now)
            if len(recent) > 180:
                await socket.close(code=1008, reason="input rate exceeded")
                return
            try:
                event = minilink.validate_event(json.loads(raw))
                await _send_mini_event(socket.app, session, event)
            except json.JSONDecodeError:
                await socket.close(code=1008, reason="invalid event")
                return
            except minilink.InvalidMiniEvent as exc:
                await socket.close(code=1008, reason=str(exc))
                return
            except minilink.MiniUnavailable:
                async with socket.app.state.mini_input_lock:
                    if socket.app.state.mini_input_session is session:
                        socket.app.state.mini_input_session = None
                await socket.send_json(minilink.unavailable_status(
                    "Mac input helper disconnected"))
                await socket.close(code=1011, reason="helper disconnected")
                return
    except WebSocketDisconnect:
        pass
    finally:
        if session is not None and joined:
            async with socket.app.state.mini_input_lock:
                socket.app.state.mini_input_members.pop(controller, None)
                # An ungraceful phone disconnect must never strand a held
                # modifier or mouse button. This can interrupt the other
                # controller's hold, which is safer than a stuck Command key.
                try:
                    await session.send({"type": "release_all"})
                except minilink.MiniUnavailable:
                    pass
                if session not in socket.app.state.mini_input_members.values():
                    if socket.app.state.mini_input_session is session:
                        socket.app.state.mini_input_session = None
                    await session.close()


@app.post("/api/cmd")
def api_cmd(
    payload: dict = Body(...),
    identity: Identity = Depends(require),
) -> JSONResponse:
    """Run one command from the table, or explain why it does not exist yet.

    The 501 path is the normal one today: the UI is being built ahead of the
    device modules, so most of the remote is drawn but inert. Answering with
    the command's own label and note means the phone can say something useful
    without keeping its own copy of what is and is not finished.
    """
    command_id = str(payload.get("cmd", ""))
    command = commands.get(command_id)
    if command is None:
        # Not a 404: a newer panel (or an older phone) may know buttons
        # this build does not, and version skew should read as "coming
        # soon", never as an error. Typos get the same soft answer -- the
        # price of being kind to the future.
        return JSONResponse(
            {"status": "coming_soon", "cmd": command_id, "device": "",
             "label": command_id or "that button",
             "note": "this build does not know that button yet"},
            status_code=501,
        )

    if command.handler is None:
        return JSONResponse(
            {
                "status": "coming_soon",
                "cmd": command.id,
                "device": command.device,
                "label": command.label,
                "note": command.note,
            },
            status_code=501,
        )

    args = payload.get("args") or {}
    if not isinstance(args, dict):
        raise HTTPException(status_code=400, detail="args must be an object")

    if payload.get("background"):
        # For callers that cannot wait out a scene -- a Live Activity intent
        # gets seconds, scene.off can hold the rack for 45s of TV wake. 202:
        # accepted, still running. The outcome reaches every client through
        # the poller and the SSE feed, which is where a phone was going to
        # learn it anyway; a scene already busy still answers here as a
        # normal poke of an occupied lock (the thread's SceneBusyError has
        # nowhere to land, and "told to wait" was the answer either way).
        def _run_then_poke() -> None:
            try:
                command.handler(args)
            except Exception:
                pass    # the next poll reads what actually happened
            finally:
                state.poke()
        threading.Thread(target=_run_then_poke, daemon=True,
                         name=f"cmd:{command.id}").start()
        return JSONResponse(
            {"status": "accepted", "cmd": command.id, "label": command.label},
            status_code=202,
        )

    try:
        result = command.handler(args)
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except NotImplementedError as exc:
        # A driver verb this device genuinely does not have. The key is
        # normally dimmed already (capability gating), but a stale page or
        # a scene step can still arrive here -- and "this device does not
        # work that way" is coming-soon honesty, not a failure.
        return JSONResponse(
            {"status": "coming_soon", "cmd": command.id,
             "device": command.device, "label": command.label,
             "note": str(exc) or command.note},
            status_code=501,
        )
    except commands.SceneBusyError as exc:
        # Not a downstream failure: the rack is fine, it is mid-scene. 409
        # so the phone can say "still working on the last one" honestly.
        raise HTTPException(status_code=409, detail=str(exc))
    except (OSError, RuntimeError) as exc:
        # The device is there and the code is written, but the thing did not
        # happen -- a dead serial port, an unplugged TV. 502: the failure is
        # downstream of us, and the phone should say so rather than pretend.
        raise HTTPException(status_code=502, detail=str(exc))

    # The rack just changed; read it now, not at the next beat -- this is
    # what turns a button press into a sub-second SSE repaint.
    state.poke()
    return JSONResponse({"status": "ok", "cmd": command.id, "label": command.label,
                         **(result or {})})


class _AgentClientGone(RuntimeError):
    """The caller disconnected before the agent could safely act."""


class _AgentSessionBusy(RuntimeError):
    """A second request tried to race the same conversation."""


_AGENT_SESSION_LOCK = threading.Lock()
_AGENT_SESSIONS_IN_FLIGHT: set[tuple[str, str]] = set()


async def _ask_while_connected(
    request: Request, message: str, session_id: str, caller: str,
    request_id: str | None = None, channel: str = "text",
) -> dict:
    if await request.is_disconnected():
        raise _AgentClientGone
    key = (caller, session_id)
    with _AGENT_SESSION_LOCK:
        if key in _AGENT_SESSIONS_IN_FLIGHT:
            raise _AgentSessionBusy
        _AGENT_SESSIONS_IN_FLIGHT.add(key)
    disconnected = threading.Event()

    async def watch_disconnect() -> None:
        # Both endpoint bodies are fully consumed before this starts. Waiting
        # on the ASGI receive channel avoids a polling window between a phone
        # cancellation and the model returning its hardware tool calls.
        while True:
            try:
                event = await request.receive()
            except Exception:
                # A broken/closing ASGI channel is not evidence that the
                # caller remains present. Fail closed before any tool runs.
                disconnected.set()
                return
            if event["type"] == "http.disconnect":
                disconnected.set()
                return

    try:
        watcher = asyncio.create_task(watch_disconnect())
        try:
            kwargs = {"cancelled": disconnected.is_set}
            if request_id is not None:
                kwargs["request_id"] = request_id
            if channel != "text":
                kwargs["channel"] = channel
            result = await run_in_threadpool(
                agentlink.ask, message, session_id, caller, **kwargs)
            if disconnected.is_set():
                raise _AgentClientGone
            return result
        except agentlink.AgentError:
            if disconnected.is_set():
                raise _AgentClientGone from None
            raise
        finally:
            watcher.cancel()
            try:
                await watcher
            except asyncio.CancelledError:
                pass
    finally:
        with _AGENT_SESSION_LOCK:
            _AGENT_SESSIONS_IN_FLIGHT.discard(key)


@app.post("/api/agent")
async def api_agent(
    request: Request,
    payload: dict = Body(...),
    identity: Identity = Depends(require),
) -> JSONResponse:
    """Interpret one utterance; execution remains inside avctl's allowlist."""
    raw_message = payload.get("message")
    raw_session = payload.get("session")
    raw_request_id = payload.get("request_id")
    if not isinstance(raw_message, str):
        raise HTTPException(status_code=400,
                            detail="message must be a string")
    if not isinstance(raw_session, str):
        raise HTTPException(status_code=400,
                            detail="session must be a string")
    message = raw_message.strip()
    session_id = raw_session.strip()
    if raw_request_id is not None and not isinstance(raw_request_id, str):
        raise HTTPException(status_code=400,
                            detail="request_id must be a string")
    request_id = raw_request_id.strip() if raw_request_id is not None else None
    if request_id is not None and not re.fullmatch(
            r"[A-Za-z0-9_-]{8,100}", request_id):
        raise HTTPException(status_code=400, detail="invalid request id")
    if len(message) > 2000:
        raise HTTPException(status_code=400, detail="message is too long")
    try:
        result = await _ask_while_connected(
            request, message, session_id, identity.who, request_id)
    except _AgentClientGone:
        raise HTTPException(
            status_code=499,
            detail="request was cancelled before action") from None
    except _AgentSessionBusy:
        raise HTTPException(
            status_code=409,
            detail="this conversation is already handling another request"
        ) from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except agentlink.AgentError as exc:
        print(f"  agent request   failed={exc}", flush=True)
        raise HTTPException(status_code=502, detail=str(exc))
    except Exception as exc:
        # Mirror the voice route's last-resort boundary. An unforeseen bug can
        # occur after a device write, so return JSON, hide native details, and
        # make the client check state before it offers a retry.
        failure = type(exc).__name__
        state.poke()
        print(f"  agent request   failed={failure}", flush=True)
        return JSONResponse({
            "detail": (f"agent failed unexpectedly ({failure}); action "
                       "status is unknown, so check before retrying"),
        }, status_code=502)
    return JSONResponse({"status": "ok", **result})


@app.get("/api/agent/history")
def api_agent_history(
    session: str = Query(...),
    identity: Identity = Depends(require),
) -> JSONResponse:
    """Restore the current private Ask conversation after an app reload."""
    session_id = session.strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", session_id):
        raise HTTPException(status_code=400, detail="invalid session id")
    try:
        history = agentlink.session_history(identity.who, session_id)
    except agentlink.AgentError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    return JSONResponse({"status": "ok", **history})


@app.get("/api/settings/agent")
def api_agent_settings(
    identity: Identity = Depends(require),
) -> JSONResponse:
    """Return provider profiles without ever exposing credential contents."""
    try:
        payload = agent_providers.public_settings()
    except agent_providers.ProviderError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    return JSONResponse({"status": "ok", **payload})


@app.get("/api/settings/panels")
def api_panel_settings(
    identity: Identity = Depends(require),
) -> JSONResponse:
    """Return registered panels, active visibility, and saved order."""
    try:
        payload = views.panel_settings()
    except ValueError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    return JSONResponse({"status": "ok", **payload})


@app.get("/api/settings/music")
def api_music_settings(
    identity: Identity = Depends(require),
) -> JSONResponse:
    """Return the active, safely switchable music backends."""
    try:
        payload = musiclink.music_backend_settings()
    except musiclink.MusicError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    return JSONResponse({"status": "ok", **payload})


@app.post("/api/settings/music")
def api_set_music_backend(
    payload: dict = Body(...),
    identity: Identity = Depends(require),
) -> JSONResponse:
    """Persist and live-switch the Music panel and Ask music backend."""
    backend = payload.get("backend")
    if not isinstance(backend, str):
        raise HTTPException(status_code=400, detail="backend must be a string")
    try:
        selected = musiclink.set_music_backend(backend)
    except musiclink.MusicError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return JSONResponse({"status": "ok", "reload_required": True,
                         **selected})


@app.post("/api/settings/panels")
def api_set_panel_settings(
    payload: dict = Body(...),
    identity: Identity = Depends(require),
) -> JSONResponse:
    """Save panel visibility/order; the client reloads to rebuild the rail."""
    try:
        selected = views.set_panel_settings(
            payload.get("order"), payload.get("enabled"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return JSONResponse({"status": "ok", "reload_required": True,
                         **selected})


@app.get("/api/setup")
def api_setup_manifest(
    identity: Identity = Depends(require),
) -> JSONResponse:
    """Driver choices and wizard state, with no credentials or secrets."""
    try:
        payload = setup_service.manifest()
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    return JSONResponse({"status": "ok", **payload})


@app.post("/api/setup/discover")
async def api_setup_discover(
    payload: dict = Body(...),
    identity: Identity = Depends(require),
) -> JSONResponse:
    """Run one explicitly requested, read-only discovery provider."""
    kind = payload.get("kind")
    prefix = payload.get("prefix")
    if not isinstance(kind, str):
        raise HTTPException(status_code=400, detail="kind must be a string")
    if prefix is not None and not isinstance(prefix, str):
        raise HTTPException(status_code=400, detail="prefix must be a string")
    if kind.strip() == "mini":
        try:
            session = await minilink.open_session()
        except minilink.MiniUnavailable as exc:
            return JSONResponse({"status": "ok", "kind": "mini",
                                 "helper": minilink.unavailable_status(str(exc))})
        try:
            return JSONResponse({"status": "ok", "kind": "mini",
                                 "helper": session.status})
        finally:
            await session.close()
    try:
        result = await run_in_threadpool(
            setup_service.discovery, kind.strip(), prefix)
    except setup_service.SetupError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return JSONResponse({"status": "ok", **result})


@app.post("/api/setup/access")
def api_setup_access(
    payload: dict = Body(...),
    identity: Identity = Depends(require),
) -> JSONResponse:
    if str(payload.get("action") or "") != "enable_tailscale_serve":
        raise HTTPException(status_code=400, detail="unknown access setup action")
    try:
        result = setup_service.enable_tailscale_serve(payload.get("port"))
    except setup_service.SetupError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return JSONResponse({"status": "ok", **result})


@app.post("/api/setup/apple-music")
async def api_setup_apple_music(
    payload: dict = Body(...),
    identity: Identity = Depends(require),
) -> JSONResponse:
    if str(payload.get("action") or "") != "authorize":
        raise HTTPException(status_code=400,
                            detail="unknown Apple Music setup action")
    try:
        result = await run_in_threadpool(setup_service.authorize_apple_music)
    except setup_service.SetupError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return JSONResponse({"status": "ok", **result})


@app.post("/api/setup/voice")
async def api_setup_voice(
    payload: dict = Body(...),
    identity: Identity = Depends(require),
) -> JSONResponse:
    """Prepare the local Ask transcription model; never handle credentials."""
    action = str(payload.get("action") or "status")
    try:
        if action == "prepare":
            result = await run_in_threadpool(voicelink.prepare)
        elif action == "status":
            result = voicelink.status()
        else:
            raise voicelink.VoiceError(
                "voice setup action must be prepare or status")
    except voicelink.VoiceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return JSONResponse({"status": "ok", **result})


@app.post("/api/setup/mini")
async def api_setup_mini(
    payload: dict = Body(...),
    identity: Identity = Depends(require),
) -> JSONResponse:
    """Enable/check the optional Mac input helper and its TCC permission."""
    action = str(payload.get("action") or "status")
    if action not in {"enable", "disable", "status"}:
        raise HTTPException(status_code=400,
                            detail="mini setup action must be enable, disable, or status")
    result: dict = {}
    if action != "status":
        try:
            result = await run_in_threadpool(
                setup_service.configure_input_helper, action == "enable")
        except setup_service.SetupError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
    if action != "disable":
        try:
            session = await minilink.open_session()
        except minilink.MiniUnavailable as exc:
            result["helper"] = minilink.unavailable_status(str(exc))
        else:
            try:
                result["helper"] = session.status
            finally:
                await session.close()
    return JSONResponse({"status": "ok", **result})


@app.post("/api/setup/activate")
def api_setup_activate(
    payload: dict = Body(...),
    identity: Identity = Depends(require),
) -> JSONResponse:
    """Validate and atomically save the device portion of a setup draft."""
    try:
        result = setup_service.activate(payload)
    except setup_service.SetupError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except OSError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    return JSONResponse({"status": "ok", **result})


@app.post("/api/setup/restart")
def api_setup_restart(
    background: BackgroundTasks,
    identity: Identity = Depends(require),
) -> JSONResponse:
    if settings.INSTALL_KIND != "package":
        raise HTTPException(status_code=400,
                            detail="automatic restart needs a packaged install")
    background.add_task(setup_service.restart_packaged_core)
    return JSONResponse({"status": "ok", "restarting": True})


@app.post("/api/setup/roon")
def api_setup_roon(
    payload: dict = Body(...),
    identity: Identity = Depends(require),
) -> JSONResponse:
    """Authorize the Core as a Roon extension; never return its token."""
    action = str(payload.get("action") or "status")
    try:
        if action == "start":
            result = setup_service.start_roon_authorization(payload)
        elif action == "status":
            result = setup_service.roon_authorization_status()
        elif action == "cancel":
            result = setup_service.cancel_roon_authorization()
        else:
            raise setup_service.SetupError(
                "Roon setup action must be start, status, or cancel")
    except setup_service.SetupError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except OSError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    return JSONResponse({"status": "ok", **result})


@app.post("/api/setup/apple-services")
def api_setup_apple_services(
    payload: dict = Body(...),
    identity: Identity = Depends(require),
) -> JSONResponse:
    """Pair or verify managed Apple capabilities with an owner broker."""
    try:
        result = setup_service.apple_services(payload)
    except setup_service.SetupError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except OSError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    return JSONResponse({"status": "ok", **result})


@app.post("/api/settings/agent")
def api_set_agent_profile(
    payload: dict = Body(...),
    identity: Identity = Depends(require),
) -> JSONResponse:
    """Select the provider used from the next Ask turn onward."""
    name = payload.get("profile")
    if not isinstance(name, str):
        raise HTTPException(status_code=400, detail="profile must be a string")
    try:
        selected = agent_providers.set_active_profile(name.strip())
    except agent_providers.ProviderError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return JSONResponse({"status": "ok", "active_profile": selected.name})


@app.post("/api/settings/agent/credential")
def api_set_agent_credential(
    payload: dict = Body(...),
    identity: Identity = Depends(require),
) -> JSONResponse:
    """Write an Ask credential privately; its value is never returned."""
    name = payload.get("profile")
    value = payload.get("credential")
    if not isinstance(name, str) or not isinstance(value, str):
        raise HTTPException(
            status_code=400, detail="profile and credential must be strings")
    try:
        status = agent_providers.save_credential(name.strip(), value)
    except (agent_providers.ProviderError, OSError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return JSONResponse({"status": "ok", "profile": name.strip(),
                         "credential": status})


@app.post("/api/settings/agent/test")
async def api_test_agent_profile(
    payload: dict = Body(...),
    identity: Identity = Depends(require),
) -> JSONResponse:
    """Run an explicit, side-effect-free structured tool-call probe."""
    name = payload.get("profile")
    if not isinstance(name, str):
        raise HTTPException(status_code=400, detail="profile must be a string")
    try:
        result = await run_in_threadpool(
            agent_providers.test_profile, name.strip())
    except agent_providers.ProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None
    return JSONResponse({"status": "ok", **result})


@app.post("/api/agent/voice")
async def api_agent_voice(
    request: Request,
    session: str = Query(...),
    request_id: str | None = Query(None),
    identity: Identity = Depends(require),
) -> JSONResponse:
    """Transcribe one recording locally, then use the existing Ask agent."""
    if not voicelink.enabled():
        raise HTTPException(status_code=503,
                            detail="voice Ask is disabled in setup")
    session_id = session.strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", session_id):
        # Reject before reading or transcribing an upload. A bad client should
        # not get to reserve the single MLX inference lane.
        raise HTTPException(status_code=400, detail="invalid session id")
    if request_id is not None and not re.fullmatch(
            r"[A-Za-z0-9_-]{8,100}", request_id):
        raise HTTPException(status_code=400, detail="invalid request id")
    content_type = request.headers.get("content-type")
    if not voicelink.supported(content_type):
        raise HTTPException(status_code=415, detail="unsupported audio format")
    try:
        declared = int(request.headers.get("content-length") or 0)
    except ValueError:
        raise HTTPException(status_code=400,
                            detail="invalid content length") from None
    if declared < 0:
        raise HTTPException(status_code=400, detail="invalid content length")
    if declared > voicelink.MAX_AUDIO_BYTES:
        raise HTTPException(status_code=413, detail="recording is too large")
    audio = bytearray()
    try:
        async for chunk in request.stream():
            if len(audio) + len(chunk) > voicelink.MAX_AUDIO_BYTES:
                raise HTTPException(status_code=413,
                                    detail="recording is too large")
            audio.extend(chunk)
    except ClientDisconnect:
        raise HTTPException(status_code=400,
                            detail="recording upload was interrupted") from None
    started = time.perf_counter()
    try:
        transcript = await run_in_threadpool(
            voicelink.transcribe, bytes(audio), str(content_type))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except voicelink.VoiceError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    transcribed = time.perf_counter()
    try:
        result = await _ask_while_connected(
            request, transcript, session_id, identity.who, request_id,
            channel="voice")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except _AgentClientGone:
        failed = time.perf_counter()
        transcribe_ms = round((transcribed - started) * 1000)
        agent_ms = round((failed - transcribed) * 1000)
        print(f"  voice request   transcribe={transcribe_ms}ms "
              f"agent={agent_ms}ms cancelled=before-action", flush=True)
        return JSONResponse(
            {"detail": "voice request was cancelled before action",
             "transcript": transcript},
            status_code=499,
            headers={"Server-Timing":
                     f"transcribe;dur={transcribe_ms}, agent;dur={agent_ms}"},
        )
    except _AgentSessionBusy:
        failed = time.perf_counter()
        transcribe_ms = round((transcribed - started) * 1000)
        agent_ms = round((failed - transcribed) * 1000)
        return JSONResponse(
            {"detail": "this conversation is already handling another request",
             "transcript": transcript},
            status_code=409,
            headers={"Server-Timing":
                     f"transcribe;dur={transcribe_ms}, agent;dur={agent_ms}"},
        )
    except agentlink.AgentError as exc:
        # The expensive and irreplaceable part succeeded. Give the client the
        # transcript so a provider outage never forces another recording.
        failed = time.perf_counter()
        transcribe_ms = round((transcribed - started) * 1000)
        agent_ms = round((failed - transcribed) * 1000)
        print(f"  voice request   transcribe={transcribe_ms}ms "
              f"agent={agent_ms}ms failed=agent", flush=True)
        return JSONResponse(
            {"detail": str(exc), "transcript": transcript},
            status_code=502,
            headers={"Server-Timing":
                     f"transcribe;dur={transcribe_ms}, agent;dur={agent_ms}"},
        )
    except Exception as exc:
        # Preserve the irreplaceable transcript even across an unforeseen
        # adapter/programming failure. Do not expose exception text, which may
        # contain local paths or device details; the type is enough to debug.
        failed = time.perf_counter()
        transcribe_ms = round((transcribed - started) * 1000)
        agent_ms = round((failed - transcribed) * 1000)
        failure = type(exc).__name__
        print(f"  voice request   transcribe={transcribe_ms}ms "
              f"agent={agent_ms}ms failed={failure}", flush=True)
        return JSONResponse(
            {"detail": (f"agent failed unexpectedly ({failure}); "
                        "action status is unknown, so check before retrying"),
             "transcript": transcript},
            status_code=502,
            headers={"Server-Timing":
                     f"transcribe;dur={transcribe_ms}, agent;dur={agent_ms}"},
        )
    completed = time.perf_counter()
    transcribe_ms = round((transcribed - started) * 1000)
    agent_ms = round((completed - transcribed) * 1000)
    total_ms = round((completed - started) * 1000)
    print(f"  voice request   transcribe={transcribe_ms}ms "
          f"agent={agent_ms}ms total={total_ms}ms", flush=True)
    return JSONResponse(
        {"status": "ok", "transcript": transcript, **result},
        headers={"Server-Timing":
                 f"transcribe;dur={transcribe_ms}, agent;dur={agent_ms}"},
    )


@app.post("/api/agent/reset")
def api_agent_reset(
    payload: dict = Body(...),
    identity: Identity = Depends(require),
) -> JSONResponse:
    raw_session = payload.get("session")
    if not isinstance(raw_session, str):
        raise HTTPException(status_code=400,
                            detail="session must be a string")
    session_id = raw_session.strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", session_id):
        raise HTTPException(status_code=400, detail="invalid session id")
    key = (identity.who, session_id)
    with _AGENT_SESSION_LOCK:
        if key in _AGENT_SESSIONS_IN_FLIGHT:
            raise HTTPException(
                status_code=409,
                detail="this conversation is already handling another request")
        agentlink.reset_session(identity.who, session_id)
    return JSONResponse({"status": "ok"})


# --- the app's own updates (OTA, App-Store-style without the App Store) ---
#
# Publish a signed .ipa and its manifest into settings.APP_DIST.
# iOS installs from an
# itms-services link, which requires HTTPS with a certificate it trusts --
# tailscale serve provides exactly that. The whole flow rides the tailnet, so
# it works precisely when the VPN is up: the situation where the devicectl
# push path cannot reach the phone.

_APP_ARTIFACTS = {
    "manifest.plist": "text/xml",
    "avctl.ipa": "application/octet-stream",
}


@app.get("/app", response_class=HTMLResponse)
def app_install(request: Request, identity: Identity = Depends(require)) -> HTMLResponse:
    """One button: install (or update to) whatever build is published.

    Whatever the deployer last published -- newest or a rollback -- is what
    the button installs. Version control stays with the deployer; this page
    is just the doorbell.
    """
    manifest = settings.APP_DIST / "manifest.plist"
    if not manifest.is_file():
        return HTMLResponse("<h1>no app build published yet</h1>", status_code=404)
    base = str(request.base_url).rstrip("/")
    link = ("itms-services://?action=download-manifest&url="
            + base + "/app/manifest.plist")
    version = (settings.APP_DIST / "version").read_text(encoding="utf-8").strip() \
        if (settings.APP_DIST / "version").is_file() else "unknown"
    return HTMLResponse(
        "<!doctype html><meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<body style='background:#131517;color:#e6e6e6;font-family:-apple-system;"
        "display:flex;flex-direction:column;align-items:center;justify-content:center;"
        f"height:90vh;gap:14px'><div>avctl build {escape(version)}</div>"
        f"<a href='{escape(link)}' style='background:#2e8b57;color:#fff;"
        "padding:16px 38px;border-radius:14px;text-decoration:none;"
        "font-size:19px;font-weight:600'>Install</a>"
        "<div style='color:#888;font-size:13px'>installs over the tailnet, "
        "VPN on is fine</div></body>")


@app.api_route("/app/{name}", methods=["GET", "HEAD"])
def app_artifact(name: str, identity: Identity = Depends(require)) -> FileResponse:
    media = _APP_ARTIFACTS.get(name)
    if media is None:
        raise HTTPException(status_code=404, detail="no such artifact")
    path = settings.APP_DIST / name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="no build published yet")
    return FileResponse(path, media_type=media)


@app.post("/api/phone/register")
def api_phone_register(
    payload: dict = Body(...),
    identity: Identity = Depends(require),
) -> JSONResponse:
    """The app hands over APNs tokens so the island can outlive it.

    Comes through the same auth door as everything else: over the tailnet
    (or with the bearer token), so a token can only be planted by someone
    who could already drive the rack.
    """
    try:
        phonelink.register(
            str(payload.get("kind", "")),
            str(payload.get("token", "")),
            activity_id=payload.get("activity_id"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return JSONResponse({"status": "ok"})


# Seconds between heartbeats on a quiet stream -- the value comes from
# config.yaml (sync.sse_heartbeat). Still a module-level name so a test can
# shrink it without touching the file.
SSE_HEARTBEAT = settings.SSE_HEARTBEAT


async def _event_stream():
    """The SSE body: one `state` event per poll that changed something.

    Async on purpose: the old sync generator was iterated via the shared
    anyio threadpool, so every connected phone pinned one of its ~40 workers
    for the life of the stream and for up to a heartbeat after disconnecting
    (#93). Now a stream costs a coroutine; the poller thread kicks us over
    via call_soon_threadsafe.

    Heartbeats are a named `ping` event rather than an SSE comment: comments
    never reach EventSource listeners, which left the client with no way to
    notice a silently dead connection (#106).
    """
    seen = 0    # 0: nothing sent yet; the poller's first snapshot is seq 1
    last_sent = ""
    wake = asyncio.Event()
    loop = asyncio.get_running_loop()

    def kick() -> None:     # runs on the poller thread
        loop.call_soon_threadsafe(wake.set)

    state.add_listener(kick)
    try:
        while True:
            wake.clear()
            snap, seq, _ = state.latest()
            if snap is not None and seq != seen:
                seen = seq
                payload = dict(snap)
                payload["implemented"] = commands.implemented()
                payload["island_volume"] = phonelink.island_volume_style()
                # `seq` identifies the poll, not the rack state. Including
                # it in the comparison made every quiet beat look different
                # and turned this stream back into polling over SSE. Compare
                # the stable payload first, then attach the newest sequence
                # only to an event that is actually going out.
                comparable = json.dumps(payload)
                if comparable != last_sent:  # an unchanged poll is a beat,
                    last_sent = comparable   # not news
                    payload["seq"] = seq
                    data = json.dumps(payload)
                    yield f"event: state\ndata: {data}\n\n"
                continue
            try:
                await asyncio.wait_for(wake.wait(), timeout=SSE_HEARTBEAT)
            except TimeoutError:
                yield "event: ping\ndata: {}\n\n"
    finally:
        state.remove_listener(kick)


@app.get("/api/events")
def api_events(identity: Identity = Depends(require)) -> StreamingResponse:
    """The push side of /api/state: one `state` event per completed poll.

    Server-Sent Events rather than a websocket because the traffic is
    strictly one-way and EventSource reconnects itself. The stream sends the
    current snapshot immediately, then again whenever the poller lands a new
    one -- which a button press forces via poke(), so the phone sees its own
    action reflected in well under a second. Quiet stretches carry named
    `ping` events so the CLIENT can also tell a quiet stream from a dead one
    and rebuild it -- a comment heartbeat never reached JS at all.
    """
    return StreamingResponse(
        _event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


# --- provider-neutral music browsing -------------------------------------
#
# GET routes rather than commands: album grids, track lists and artwork are
# data the panel renders, not actions, and the command table has nothing
# useful to say about a JPEG. Actions (play, queue, refresh) stay in
# /api/cmd like everything else.


@app.get("/api/music/recent")
def api_music_recent(
    refresh: int = Query(0),
    offset: int = Query(0, ge=0),
    limit: int = Query(0, ge=0, le=500),
    identity: Identity = Depends(require),
) -> JSONResponse:
    """One page of the recently-added shelf.

    Paged because the shelf runs to the whole library (472 albums here) and
    the grid loads more as it scrolls, like Music's own. The scan itself is
    cached in musiclink, so deeper pages cost a slice, not a rescan.
    """
    size = limit or musiclink.page_size()
    try:
        albums = musiclink.recent(force=bool(refresh))
    except MusicError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return JSONResponse({
        "albums": albums[offset:offset + size],
        "total": len(albums),
    })


@app.get("/api/music/recent-songs")
def api_music_recent_songs(
    refresh: int = Query(0),
    offset: int = Query(0, ge=0),
    limit: int = Query(0, ge=0, le=500),
    identity: Identity = Depends(require),
) -> JSONResponse:
    """One page of the library as songs, newest-added first -- the song
    view's twin of /api/music/recent, same cache discipline."""
    size = limit or musiclink.page_size()
    try:
        songs = musiclink.recent_songs(force=bool(refresh))
    except MusicError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return JSONResponse({
        "songs": songs[offset:offset + size],
        "total": len(songs),
    })


@app.get("/api/music/album")
def api_music_album(
    album: str = Query(...),
    artist: str = Query(""),
    identity: Identity = Depends(require),
) -> JSONResponse:
    try:
        tracks = musiclink.album_tracks(album, artist)
    except MusicError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return JSONResponse({"album": album, "artist": artist, "tracks": tracks})


@app.get("/api/music/queue")
def api_music_queue(
    identity: Identity = Depends(require),
) -> JSONResponse:
    """The authoritative avctl queue for the transport's Focus view."""
    details = musiclink.queue_details()
    musiclink.prefetch_queue_artwork(details)
    return JSONResponse(details)


# The stored bytes are JPEG or PNG in practice; sniff rather than assume,
# and fall back to TIFF (Safari renders it, and Safari is what visits).
_ART_TYPES = [(b"\xff\xd8", "image/jpeg"), (b"\x89PNG", "image/png")]


@app.get("/api/music/playlists")
def api_music_playlists(identity: Identity = Depends(require)) -> JSONResponse:
    """The library's second shelf: every user playlist, Music's own order."""
    try:
        return JSONResponse({"playlists": musiclink.playlists()})
    except NotImplementedError as exc:
        raise HTTPException(status_code=501, detail=str(exc))
    except MusicError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.get("/api/music/playlist")
def api_music_playlist(
    pid: str = Query(..., min_length=1),
    identity: Identity = Depends(require),
) -> JSONResponse:
    """One playlist's tracks, in playlist order."""
    try:
        return JSONResponse(musiclink.playlist_tracks(pid))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except NotImplementedError as exc:
        raise HTTPException(status_code=501, detail=str(exc))
    except MusicError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.get("/api/music/artwork/{pid}")
def api_music_artwork(
    pid: str,
    cached: bool = Query(False),
    identity: Identity = Depends(require),
) -> FileResponse:
    if not MEDIA_ID_RE.match(pid):
        raise HTTPException(status_code=404, detail="not a track id")
    try:
        path = (musiclink.cached_artwork_file(pid) if cached
                else musiclink.artwork_file(pid))
    except FileNotFoundError:
        # A queue can ask before another view has populated the cache. Do not
        # let WebKit retain that temporary miss after the cover becomes
        # available later in the same session.
        raise HTTPException(
            status_code=404,
            detail="no artwork",
            headers={"Cache-Control": "no-store"},
        )
    except MusicError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    with path.open("rb") as fh:
        head = fh.read(4)
    media_type = next((t for magic, t in _ART_TYPES if head.startswith(magic)),
                      "image/tiff")
    return FileResponse(
        path,
        media_type=media_type,
        # Immutable is safe: persistent IDs are stable, and new art for the
        # same track is rare enough that clearing the cache dir is the
        # answer. A year rather than a week for the same reason -- a
        # pid-addressed cover cannot go stale, so every re-fetch a client
        # ever makes is waste.
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


@app.get("/api/music/search")
def api_music_search(
    q: str = Query(..., min_length=1),
    identity: Identity = Depends(require),
) -> JSONResponse:
    """Albums matching the keyword directly or through their songs."""
    try:
        albums = musiclink.search_albums(q)
    except MusicError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    info = musiclink.service_info()
    return JSONResponse({"albums": albums,
                         "authorized": bool(info.get("authorized")),
                         "service": info})


@app.get("/api/music/explore")
def api_music_explore(
    limit: int = Query(10, ge=1, le=20),
    identity: Identity = Depends(require),
) -> JSONResponse:
    """Read-only local affinity and configured-service discovery."""
    try:
        return JSONResponse(musiclink.explore(limit))
    except MusicError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.get("/api/music/search/album")
def api_music_search_album(
    id: str = Query(..., min_length=1),
    identity: Identity = Depends(require),
) -> JSONResponse:
    """One catalog album with tracks: the add-all-or-cherry-pick view."""
    try:
        album = musiclink.catalog_album(id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except MusicError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    info = musiclink.service_info()
    return JSONResponse({**album,
                         "authorized": bool(info.get("authorized")),
                         "service": info})


@app.get("/api/music/info")
def api_music_info(identity: Identity = Depends(require)) -> JSONResponse:
    """Provider-neutral labels/capabilities for Music and Ask clients."""
    try:
        return JSONResponse(musiclink.service_info())
    except (MusicError, NotImplementedError, ValueError) as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.get("/api/music/library/search")
def api_music_library_search(
    q: str = Query(..., min_length=1),
    identity: Identity = Depends(require),
) -> JSONResponse:
    """The local library, grouped to albums -- these play, they are here."""
    try:
        albums = musiclink.local_album_search(q)
    except MusicError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return JSONResponse({"albums": albums})


@app.get("/api/music/devtoken")
def api_music_devtoken(identity: Identity = Depends(require)) -> JSONResponse:
    """For /music/auth only: MusicKit JS must be configured with the developer
    token before it can mint the user token."""
    try:
        token = musiclink.dev_token()
    except MusicError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return JSONResponse({"token": token})


@app.post("/api/music/usertoken")
def api_music_usertoken(
    payload: dict = Body(...),
    identity: Identity = Depends(require),
) -> JSONResponse:
    token = str(payload.get("token", "")).strip()
    if not token or len(token) > 4096:
        raise HTTPException(status_code=400, detail="no token in payload")
    musiclink.save_user_token(token)
    return JSONResponse({"status": "ok"})


@app.get("/music/auth", response_class=HTMLResponse)
def music_auth(request: Request) -> HTMLResponse:
    """One-time MusicKit JS authorize page.

    The only page that loads anything from a CDN: MusicKit JS is the sole way
    to mint a Music User Token, Apple does not allow self-hosting it, and this
    page is visited once and closed.
    """
    identity = identify(request)
    if identity is None:
        return HTMLResponse(views.gate(), status_code=401)
    response = HTMLResponse(views.music_auth())
    _remember(response, request)
    return response


# --- the installable app --------------------------------------------------
#
# Manifest, icon and launch images are unauthenticated on purpose: they are
# branding, not data, and iOS fetches some of them outside the page's
# session. A day of cache, not a week -- these change with the design, and
# there is no ASSET_VERSION query string breaking them loose.
_PWA_CACHE = {"Cache-Control": "public, max-age=86400"}


@app.get("/manifest.webmanifest")
def manifest() -> Response:
    return Response(
        content=json.dumps(pwa.manifest()),
        media_type="application/manifest+json",
        headers=_PWA_CACHE,
    )


@app.get("/static/icon-{size}.png")
def icon(size: int) -> Response:
    if size not in pwa.ICON_SIZES:
        raise HTTPException(status_code=404, detail="no icon at that size")
    return Response(content=pwa.icon_png(size), media_type="image/png",
                    headers=_PWA_CACHE)


@app.get("/static/splash-{width}x{height}.png")
def splash(width: int, height: int) -> Response:
    # Whitelisted exactly: this endpoint renders pixels on demand, and only
    # the sizes the page's own head links to are worth a render.
    if (width, height) not in pwa.splash_sizes():
        raise HTTPException(status_code=404, detail="no splash at that size")
    return Response(content=pwa.splash_png(width, height),
                    media_type="image/png", headers=_PWA_CACHE)


@app.get("/static/{name}")
def static(name: str) -> FileResponse:
    """The remote's stylesheet and script.

    Separate files rather than inlined into the page: they are the bulk of the
    app, they change far less often than the markup, and a phone that has them
    cached opens the remote without touching the network for anything but
    state.
    """
    media_type = ASSETS.get(name)
    if media_type is None:
        raise HTTPException(status_code=404, detail="no such asset")
    return FileResponse(
        UI_DIR / name,
        media_type=media_type,
        # Immutable is safe because views.ASSET_VERSION is in the query string
        # and gets bumped whenever these change.
        headers={"Cache-Control": "public, max-age=604800, immutable"},
    )


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    """The remote itself.

    Ships no device state in the markup -- the page renders unknowns and then
    fills them from /api/state, so there is exactly one code path producing
    what the screen says, whether the phone has just loaded it or has had it
    open for an hour.
    """
    identity = identify(request)
    if identity is None:
        # 401 so curl and scripts see the truth, but with the token box as the
        # body so a phone browser gets something it can actually act on.
        return HTMLResponse(views.gate(), status_code=401)

    response = HTMLResponse(views.remote(identity))
    _remember(response, request)
    return response


@app.post("/", response_class=HTMLResponse)
async def unlock(request: Request) -> Response:
    """The gate form's target: token in the body, never in the URL.

    The form used to be method=get, which put the secret in the query string
    -- uvicorn's access log, browser history, any Referer (#98). Now the
    token travels once in a POST body, lands in the HttpOnly cookie, and the
    303 lands the phone on a clean URL.
    """
    # This form has one URL-encoded field. Parsing it directly avoids pulling
    # the optional python-multipart package into every frozen Core merely to
    # establish one cookie. Keep the body bounded before decoding.
    body = await request.body()
    if len(body) > 4_096:
        return HTMLResponse(views.gate("that token did not match."),
                            status_code=401)
    try:
        values = parse_qs(body.decode("utf-8"), max_num_fields=4)
        token = str((values.get("token") or [""])[0]).strip()
    except (UnicodeDecodeError, ValueError):
        token = ""
    if not token or not secrets.compare_digest(token, load_token()):
        return HTMLResponse(views.gate("that token did not match."),
                            status_code=401)
    response = RedirectResponse("/", status_code=303)
    _set_token_cookie(response, request, token)
    return response
