"""Philips Hue Bridge client — local CLIP API v2 (LAN HTTPS, no cloud).

The point: backstage Claude calls a function and the room light reacts —
wake-up light, come-home / bedtime scenes, a night-watch status light.
Unlike the SwitchBot cloud path this is pure LAN: millisecond latency,
no cloud dependency, works even when the internet is down.

Design contract (same as switchbot_client.py — this is a self-driving
backstage script, so it must NEVER crash the caller):

  * Every public function returns a structured dict. Success looks like
    {'ok': True, ...}; failure looks like {'ok': False, 'error': ...}.
    No exceptions escape to the caller — the lamp is nice-to-have, not
    a critical path, so we degrade instead of dying.
  * The Bridge speaks HTTPS with a self-signed cert -> verify=False and
    the InsecureRequestWarning is silenced on purpose (LAN only).
  * Auth is one header: hue-application-key (obtained once via the
    physical link button by hue_pair.py; stored in config.json).

API cheatsheet (v2, base https://<bridge>/clip/v2):
    GET  /resource/light            list lights (each has a UUID rid)
    PUT  /resource/light/<rid>      body may combine:
         {"on":{"on":true}}
         {"dimming":{"brightness":60}}          # percent 0-100
         {"color":{"xy":{"x":0.3,"y":0.32}}}    # CIE xy, 0.0-1.0
         {"color_temperature":{"mirek":300}}    # 153 cold .. 500 warm
         {"dynamics":{"duration":2000}}          # fade ms

Config: reads CFG.hue_bridge_ip / hue_application_key /
hue_default_light_id. Empty ip or key -> every public function returns
a clear "not configured" error instead of blowing up.

CLI (hands-on acceptance):
    python3 hue_client.py lights                 # list ids + names + state
    python3 hue_client.py status [light_id]
    python3 hue_client.py on  [light_id]
    python3 hue_client.py off [light_id]
    python3 hue_client.py bri 60 [light_id]      # brightness percent
    python3 hue_client.py color 255 180 120 [light_id]   # RGB 0-255
    python3 hue_client.py temp 2700 [light_id]   # kelvin 2000-6500
    python3 hue_client.py timed sunrise 15       # native long fade (min)
    python3 hue_client.py effect candle          # decorative; "none" cancels
"""

from __future__ import annotations

import json
import sys

import requests
import urllib3

from config import CFG

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# LAN calls are fast; 10s is already generous and never hangs a watch cycle.
TIMEOUT_S = 10


def _not_configured() -> dict | None:
    if not CFG.hue_bridge_ip or not CFG.hue_application_key:
        return {
            "ok": False,
            "error": "hue not configured: hue_bridge_ip / hue_application_key "
                     "missing in config.json (run hue_pair.py with the link "
                     "button pressed)",
        }
    return None


def _base() -> str:
    return f"https://{CFG.hue_bridge_ip}/clip/v2"


def _headers() -> dict:
    return {"hue-application-key": CFG.hue_application_key}


def _request(method: str, path: str, body: dict | None = None) -> dict:
    """One HTTP round-trip, normalized to the {'ok': ...} contract."""
    err = _not_configured()
    if err:
        return err
    try:
        resp = requests.request(
            method, _base() + path, json=body, headers=_headers(),
            verify=False, timeout=TIMEOUT_S,
        )
    except requests.RequestException as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    try:
        data = resp.json()
    except ValueError:
        return {"ok": False, "error": f"HTTP {resp.status_code}: non-JSON reply"}

    # v2 errors arrive as {"errors": [{"description": ...}], "data": []}
    errors = data.get("errors") or []
    if resp.status_code != 200 or errors:
        desc = "; ".join(e.get("description", "?") for e in errors) or f"HTTP {resp.status_code}"
        return {"ok": False, "error": desc, "status": resp.status_code}
    return {"ok": True, "data": data.get("data", [])}


def _resolve_light_id(light_id: str | None) -> dict:
    lid = light_id or CFG.hue_default_light_id
    if lid:
        return {"ok": True, "light_id": lid}
    # No explicit id and no default: use the first light on the Bridge.
    lights = list_lights()
    if not lights["ok"]:
        return lights
    if not lights["lights"]:
        return {"ok": False, "error": "no lights paired to this Bridge yet"}
    return {"ok": True, "light_id": lights["lights"][0]["id"]}


# ---------------------------------------------------------------- public API

def list_lights() -> dict:
    """All lights known to the Bridge: id, name, on/off, brightness."""
    r = _request("GET", "/resource/light")
    if not r["ok"]:
        return r
    lights = []
    for it in r["data"]:
        lights.append({
            "id": it.get("id", ""),
            "name": (it.get("metadata") or {}).get("name", "?"),
            "on": (it.get("on") or {}).get("on"),
            "brightness": (it.get("dimming") or {}).get("brightness"),
        })
    return {"ok": True, "lights": lights}


def get_status(light_id: str | None = None) -> dict:
    """Full raw state of one light (color, temp, dynamics included)."""
    rid = _resolve_light_id(light_id)
    if not rid["ok"]:
        return rid
    r = _request("GET", f"/resource/light/{rid['light_id']}")
    if not r["ok"]:
        return r
    return {"ok": True, "light": r["data"][0] if r["data"] else {}}


def set_light(light_id: str | None = None, *, on: bool | None = None,
              brightness: float | None = None, xy: tuple | None = None,
              mirek: int | None = None, duration_ms: int | None = None) -> dict:
    """The one true setter — combine any of on/brightness/xy/mirek in one PUT."""
    rid = _resolve_light_id(light_id)
    if not rid["ok"]:
        return rid
    body: dict = {}
    if on is not None:
        body["on"] = {"on": bool(on)}
    if brightness is not None:
        body["dimming"] = {"brightness": max(0.0, min(100.0, float(brightness)))}
    if xy is not None:
        body["color"] = {"xy": {"x": float(xy[0]), "y": float(xy[1])}}
    if mirek is not None:
        body["color_temperature"] = {"mirek": max(153, min(500, int(mirek)))}
    if duration_ms is not None:
        body["dynamics"] = {"duration": int(duration_ms)}
    if not body:
        return {"ok": False, "error": "set_light called with nothing to set"}
    r = _request("PUT", f"/resource/light/{rid['light_id']}", body)
    if not r["ok"]:
        return r
    return {"ok": True, "light_id": rid["light_id"], "applied": body}


def turn_on(light_id: str | None = None, duration_ms: int | None = None) -> dict:
    return set_light(light_id, on=True, duration_ms=duration_ms)


def turn_off(light_id: str | None = None, duration_ms: int | None = None) -> dict:
    return set_light(light_id, on=False, duration_ms=duration_ms)


def set_brightness(percent: float, light_id: str | None = None) -> dict:
    return set_light(light_id, on=True, brightness=percent)


def set_color_rgb(r: int, g: int, b: int, light_id: str | None = None) -> dict:
    return set_light(light_id, on=True, xy=rgb_to_xy(r, g, b))


def set_color_temperature(kelvin: int, light_id: str | None = None) -> dict:
    kelvin = max(2000, min(6500, int(kelvin)))
    return set_light(light_id, on=True, mirek=round(1_000_000 / kelvin))


def blink(light_id: str | None = None) -> dict:
    """Identify pulse ("breathe") — one gentle brightness wave, then the
    light returns to whatever state it was in. The polite way to say hi."""
    rid = _resolve_light_id(light_id)
    if not rid["ok"]:
        return rid
    r = _request("PUT", f"/resource/light/{rid['light_id']}",
                 {"alert": {"action": "breathe"}})
    if not r["ok"]:
        return r
    return {"ok": True, "light_id": rid["light_id"], "action": "breathe"}


TIMED_EFFECTS = ("no_effect", "sunrise", "sunset")


def timed_effect(name: str, duration_min: float | None = None,
                 light_id: str | None = None) -> dict:
    """Native long-fade effects: 'sunrise' (dark -> warm bright) and
    'sunset' (the reverse). The lamp runs the whole gradient itself —
    one PUT replaces a hand-rolled brightness loop. 'no_effect' cancels."""
    if name not in TIMED_EFFECTS:
        return {"ok": False,
                "error": f"unknown timed effect {name!r}, pick from {TIMED_EFFECTS}"}
    rid = _resolve_light_id(light_id)
    if not rid["ok"]:
        return rid
    body: dict = {"timed_effects": {"effect": name}}
    if duration_min is not None and name != "no_effect":
        body["timed_effects"]["duration"] = int(float(duration_min) * 60_000)
    r = _request("PUT", f"/resource/light/{rid['light_id']}", body)
    if not r["ok"]:
        return r
    return {"ok": True, "light_id": rid["light_id"], "applied": body}


def set_effect(name: str, light_id: str | None = None) -> dict:
    """Decorative effects (candle, cosmos, underwater, ...). The lamp
    reports its own supported list in status; we pass the name through
    and let the Bridge reject unknown ones. 'no_effect' cancels."""
    rid = _resolve_light_id(light_id)
    if not rid["ok"]:
        return rid
    r = _request("PUT", f"/resource/light/{rid['light_id']}",
                 {"on": {"on": True}, "effects": {"effect": name}}
                 if name != "no_effect" else
                 {"effects": {"effect": "no_effect"}})
    if not r["ok"]:
        return r
    return {"ok": True, "light_id": rid["light_id"], "effect": name}


def rgb_to_xy(r: int, g: int, b: int) -> tuple:
    """sRGB 0-255 -> CIE xy (Hue wide-gamut approximation, good enough
    for mood/status lighting; per-lamp gamut clipping intentionally skipped)."""
    def lin(c: float) -> float:
        c /= 255.0
        return ((c + 0.055) / 1.055) ** 2.4 if c > 0.04045 else c / 12.92
    rl, gl, bl = lin(r), lin(g), lin(b)
    x = rl * 0.664511 + gl * 0.154324 + bl * 0.162028
    y = rl * 0.283881 + gl * 0.668433 + bl * 0.047685
    z = rl * 0.000088 + gl * 0.072310 + bl * 0.986039
    s = x + y + z
    return (0.0, 0.0) if s == 0 else (round(x / s, 4), round(y / s, 4))


# ----------------------------------------------------------------------- CLI

def _pp(obj: dict) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False))


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(__doc__, file=sys.stderr)
        return 1
    cmd, args = argv[0], argv[1:]

    if cmd == "lights":
        _pp(list_lights())
        return 0
    if cmd == "status":
        _pp(get_status(args[0] if args else None))
        return 0
    if cmd == "on":
        _pp(turn_on(args[0] if args else None))
        return 0
    if cmd == "off":
        _pp(turn_off(args[0] if args else None))
        return 0
    if cmd == "blink":
        _pp(blink(args[0] if args else None))
        return 0
    if cmd == "bri":
        if not args:
            print("用法: bri N (0-100) [light_id]", file=sys.stderr)
            return 1
        _pp(set_brightness(float(args[0]), args[1] if len(args) > 1 else None))
        return 0
    if cmd == "color":
        if len(args) < 3:
            print("用法: color R G B (each 0-255) [light_id]", file=sys.stderr)
            return 1
        _pp(set_color_rgb(int(args[0]), int(args[1]), int(args[2]),
                          args[3] if len(args) > 3 else None))
        return 0
    if cmd == "temp":
        if not args:
            print("用法: temp K (2000-6500) [light_id]", file=sys.stderr)
            return 1
        _pp(set_color_temperature(int(args[0]), args[1] if len(args) > 1 else None))
        return 0
    if cmd == "timed":
        if not args:
            print("用法: timed sunrise|sunset|no_effect [分钟] [light_id]", file=sys.stderr)
            return 1
        dur = float(args[1]) if len(args) > 1 else None
        _pp(timed_effect(args[0], dur, args[2] if len(args) > 2 else None))
        return 0
    if cmd == "effect":
        if not args:
            print("用法: effect candle|cosmos|...|no_effect [light_id]", file=sys.stderr)
            return 1
        _pp(set_effect(args[0] if args[0] != "none" else "no_effect",
                       args[1] if len(args) > 1 else None))
        return 0

    print(f"未知命令: {cmd}\n{__doc__}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
