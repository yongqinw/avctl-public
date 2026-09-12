"""Who is allowed to talk to the control app.

The decision in docs/remote-access.md is that authentication is not ours to
invent. The app binds to loopback, `tailscale serve` fronts it, and the tailnet
has already decided who gets through -- SSO, MFA, per-device keys, ACLs, none
of it written here. This module's job is to *read* the answer Tailscale gives
us, not to make one up.

The bearer token is a second path, for the window before Tailscale is set up
and for curl. It is a shared secret, which is the very thing the Tailscale plan
exists to avoid, so treat it as scaffolding: once the tailnet is live and
/whoami shows a real login, set AVCTL_ALLOWED_USERS and the token stops being
the interesting door.
"""

from __future__ import annotations

import ipaddress
import os
import secrets
import threading
from dataclasses import dataclass

from fastapi import Request

from . import settings

# Tailscale Serve adds these to each proxied request. If the names are ever
# wrong the app fails *closed* -- a missing header is simply not an identity --
# so the failure mode is a 401, not an open door. /whoami exists to confirm
# what actually arrives once the tailnet is up.
LOGIN_HEADER = "tailscale-user-login"
NAME_HEADER = "tailscale-user-name"

COOKIE_NAME = "avctl_token"


@dataclass(frozen=True)
class Identity:
    """Who the caller is, and which door they came through."""

    who: str
    method: str  # "tailscale" | "token"
    display: str | None = None


def _is_loopback(host: str | None) -> bool:
    if not host:
        return False
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def trusts_identity_headers() -> bool:
    """Whether `Tailscale-User-*` headers may be believed.

    They are only meaningful because the sole thing that can reach a loopback
    socket is tailscaled proxying inward. Bind to anything else -- 0.0.0.0 for
    a quick phone test on the LAN, say -- and any client can simply send the
    header themselves and become whoever they like. So the trust is tied to the
    bind address rather than assumed.
    """
    return _is_loopback(settings.BIND)


_token_lock = threading.Lock()
_token_cache: str | None = None


def load_token() -> str:
    """The shared secret, generated and persisted on first run.

    Generating rather than defaulting matters: there is no configuration in
    which this app starts up with no authentication at all.

    Cached after the first read -- identify() runs on every request, and a
    button press should not cost a disk read (#98). Generation is a single
    O_CREAT|O_EXCL create: the old touch-then-write left a window where a
    concurrent caller read an empty file and generated a competing token.
    """
    global _token_cache
    if settings.TOKEN:
        return settings.TOKEN

    with _token_lock:
        if _token_cache:
            return _token_cache

        path = settings.TOKEN_FILE
        if path.exists():
            existing = path.read_text(encoding="utf-8").strip()
            if existing:
                _token_cache = existing
                return existing
            # An empty file is the old generate path's crash debris; clear
            # it so the exclusive create below can win.
            path.unlink(missing_ok=True)

        token = secrets.token_urlsafe(32)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            # Another process won the race; its token is the real one.
            existing = path.read_text(encoding="utf-8").strip()
            if existing:
                _token_cache = existing
                return existing
            raise RuntimeError(f"{path} exists but is empty; delete it "
                               "and restart") from None
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            fh.write(token + "\n")
        _token_cache = token
        return token


def _presented_token(request: Request) -> str | None:
    """A token from any of the three places a phone or curl might put one."""
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    # Query param so a phone can be authorised by visiting a link once; the
    # cookie set from it is what carries every request after that.
    return request.query_params.get("token") or request.cookies.get(COOKIE_NAME)


def identify(request: Request) -> Identity | None:
    """The caller's identity, or None if they have not proved one."""
    if trusts_identity_headers():
        login = request.headers.get(LOGIN_HEADER)
        if login:
            allowed = settings.ALLOWED_USERS
            if allowed and login.lower() not in allowed:
                return None
            return Identity(
                who=login,
                method="tailscale",
                display=request.headers.get(NAME_HEADER),
            )

    presented = _presented_token(request)
    if presented and secrets.compare_digest(presented, load_token()):
        return Identity(who="token", method="token")

    return None
