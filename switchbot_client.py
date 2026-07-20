"""SwitchBot cloud OpenAPI v1.1 client (Color Bulb control).

The point: backstage Claude calls a function and the lamp reacts —
wake signal, come-home / bedtime scenes, a night-watch status light.

Design contract (this is a self-driving backstage script, so it must
NEVER crash the caller):

  * Every public method returns a structured dict. Success looks like
    {'ok': True, ...}; failure looks like {'ok': False, 'error': ...}.
    No exceptions escape to the caller — the lamp is nice-to-have, not
    a critical path, so we degrade silently instead of dying.
  * Success is judged by the response envelope's body statusCode == 100,
    NOT the HTTP status (which can be 200 while statusCode says 161).
  * The signature is recomputed on every request (t + nonce change each
    time and the signature has a short validity window) — headers are
    never cached.

Signature (per the official README Python sample, the authoritative
source — see mind/switchbot_research.md):

    string_to_sign = token + t + nonce          # three parts, no separator
    sign = base64( HMAC_SHA256(key=secret, msg=string_to_sign) )

Traps deliberately avoided:
  * base64, NOT hex, NOT .upper() — base64 is case-sensitive.
  * msg is token+t+nonce (three parts), not token+t (two).
  * t is a 13-digit millisecond timestamp; nonce is a fresh uuid4.

Config: reads CFG.switchbot_token / switchbot_secret / switchbot_device_id
/ switchbot_base_url. Empty token or secret -> every public method returns
a clear "not configured" error instead of blowing up.

CLI (for hands-on acceptance once a real token is pasted in):
    python3 switchbot_client.py devices
    python3 switchbot_client.py status
    python3 switchbot_client.py on
    python3 switchbot_client.py off
    python3 switchbot_client.py toggle
    python3 switchbot_client.py brightness 60
    python3 switchbot_client.py color 122 80 20
    python3 switchbot_client.py temp 3000
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import sys
import time
import uuid

import requests

from config import CFG

# Network timeout for every SwitchBot cloud call. The cloud occasionally
# 5xx / times out; 10s is generous without hanging a watch cycle.
TIMEOUT = 10

# SwitchBot statusCode -> human-readable Chinese hint. 100 = success.
# Sourced from the official API README error table.
_STATUS_HINTS = {
    100: "成功",
    151: "设备类型错误（这条命令可能不适用于该设备）",
    152: "设备未找到（deviceId 是否正确？）",
    160: "命令不被支持",
    161: "设备离线（灯泡断电/掉线，检查供电和 WiFi）",
    171: "Hub 设备离线",
    190: "设备内部错误：状态未与服务器同步，或命令格式非法",
}


def _explain(status_code, message=None) -> str:
    """Turn a non-100 statusCode into a human-readable line."""
    hint = _STATUS_HINTS.get(status_code)
    if hint:
        base = f"statusCode {status_code}: {hint}"
    else:
        base = f"statusCode {status_code}: 未知错误码"
    if message and message != "success":
        base += f"（message: {message}）"
    return base


def _sign(token: str, secret: str, t: str, nonce: str) -> str:
    """Pure signature function — base64(HMAC-SHA256(secret, token+t+nonce)).

    Kept as a standalone pure function so the offline self-test can pin
    it with fixed inputs. No .upper(), no hex; base64 exactly.
    """
    string_to_sign = f"{token}{t}{nonce}".encode("utf-8")
    secret_bytes = secret.encode("utf-8")
    digest = hmac.new(secret_bytes, msg=string_to_sign,
                      digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def _headers(token: str, secret: str) -> dict:
    """Build a fresh, fully-signed header dict (new t/nonce every call)."""
    t = str(int(round(time.time() * 1000)))
    nonce = str(uuid.uuid4())
    return {
        "Authorization": token,
        "Content-Type": "application/json",
        "charset": "utf8",
        "t": t,
        "sign": _sign(token, secret, t, nonce),
        "nonce": nonce,
    }


def _is_configured() -> bool:
    return bool(CFG.switchbot_token) and bool(CFG.switchbot_secret)


def _not_configured() -> dict:
    return {
        "ok": False,
        "error": ("SwitchBot 未配置：config.json 缺少 switchbot_token / "
                  "switchbot_secret（贴上 token/secret 后即可用）"),
    }


def _request(method: str, path: str, body: dict | None = None) -> dict:
    """Core HTTP round-trip. Signs, sends, parses the SwitchBot envelope.

    Returns {'ok': True, 'body': <inner body>, 'message': ...} on
    statusCode 100, else {'ok': False, 'error': ..., 'status_code': ...}.
    Any network/parse failure is caught and returned as a structured
    error — nothing raises out of here.
    """
    if not _is_configured():
        return _not_configured()

    url = f"{CFG.switchbot_base_url.rstrip('/')}{path}"
    try:
        headers = _headers(CFG.switchbot_token, CFG.switchbot_secret)
        if method == "GET":
            resp = requests.get(url, headers=headers, timeout=TIMEOUT)
        elif method == "POST":
            resp = requests.post(url, headers=headers,
                                 data=json.dumps(body or {}), timeout=TIMEOUT)
        else:
            return {"ok": False, "error": f"不支持的方法: {method}"}
    except requests.exceptions.RequestException as e:
        return {"ok": False,
                "error": f"网络请求失败: {type(e).__name__}: {e}"}
    except Exception as e:  # last-resort net: never let anything escape
        return {"ok": False,
                "error": f"请求构造失败: {type(e).__name__}: {e}"}

    try:
        data = resp.json()
    except ValueError:
        snippet = (resp.text or "")[:200]
        return {"ok": False,
                "error": (f"响应非 JSON (HTTP {resp.status_code}): "
                          f"{snippet}")}

    status_code = data.get("statusCode")
    if status_code != 100:
        return {"ok": False, "status_code": status_code,
                "error": _explain(status_code, data.get("message"))}

    return {"ok": True, "body": data.get("body", {}),
            "message": data.get("message")}


# ---------------------------------------------------------------------------
# Core endpoints
# ---------------------------------------------------------------------------

def list_devices() -> dict:
    """GET /v1.1/devices. -> {'ok': True, 'devices': [...], 'infrared': [...]}"""
    res = _request("GET", "/v1.1/devices")
    if not res["ok"]:
        return res
    body = res["body"] or {}
    return {
        "ok": True,
        "devices": body.get("deviceList", []),
        "infrared": body.get("infraredRemoteList", []),
    }


def get_status(device_id: str | None = None) -> dict:
    """GET /v1.1/devices/{id}/status. -> {'ok': True, 'status': {...}}"""
    device_id = device_id or CFG.switchbot_device_id
    if not device_id:
        return {"ok": False,
                "error": "未指定 device_id，且 config.json 的 "
                         "switchbot_device_id 为空"}
    res = _request("GET", f"/v1.1/devices/{device_id}/status")
    if not res["ok"]:
        return res
    return {"ok": True, "status": res["body"]}


def send_command(device_id: str | None, command: str,
                 parameter="default", command_type: str = "command") -> dict:
    """POST /v1.1/devices/{id}/commands. -> {'ok': True, 'body': {...}}"""
    device_id = device_id or CFG.switchbot_device_id
    if not device_id:
        return {"ok": False,
                "error": "未指定 device_id，且 config.json 的 "
                         "switchbot_device_id 为空"}
    body = {
        "command": command,
        "parameter": parameter,
        "commandType": command_type,
    }
    res = _request("POST", f"/v1.1/devices/{device_id}/commands", body)
    if not res["ok"]:
        return res
    return {"ok": True, "body": res["body"], "message": res.get("message")}


def resolve_bulb_id() -> dict:
    """Find the first Color Bulb's deviceId so Sol never hand-copies it.

    -> {'ok': True, 'device_id': ..., 'device_name': ...} or error.
    """
    res = list_devices()
    if not res["ok"]:
        return res
    for dev in res["devices"]:
        if dev.get("deviceType") == "Color Bulb":
            return {"ok": True, "device_id": dev.get("deviceId"),
                    "device_name": dev.get("deviceName")}
    return {"ok": False,
            "error": "设备列表里没有 Color Bulb（灯泡是否已配网、"
                     "是否开了 Cloud Service？）"}


# ---------------------------------------------------------------------------
# Color Bulb convenience wrappers
# ---------------------------------------------------------------------------

def turn_on(device_id: str | None = None) -> dict:
    return send_command(device_id, "turnOn", "default")


def turn_off(device_id: str | None = None) -> dict:
    return send_command(device_id, "turnOff", "default")


def toggle(device_id: str | None = None) -> dict:
    return send_command(device_id, "toggle", "default")


def set_brightness(brightness: int, device_id: str | None = None) -> dict:
    """Brightness 1-100 (integer)."""
    try:
        b = int(brightness)
    except (TypeError, ValueError):
        return {"ok": False, "error": f"亮度必须是整数，收到: {brightness!r}"}
    if not 1 <= b <= 100:
        return {"ok": False, "error": f"亮度须在 1-100 之间，收到: {b}"}
    return send_command(device_id, "setBrightness", str(b))


def set_color(r: int, g: int, b: int, device_id: str | None = None) -> dict:
    """RGB, each channel 0-255. Sent as 'R:G:B'."""
    try:
        rr, gg, bb = int(r), int(g), int(b)
    except (TypeError, ValueError):
        return {"ok": False,
                "error": f"RGB 必须是整数，收到: {r!r}, {g!r}, {b!r}"}
    for name, v in (("R", rr), ("G", gg), ("B", bb)):
        if not 0 <= v <= 255:
            return {"ok": False,
                    "error": f"{name} 通道须在 0-255 之间，收到: {v}"}
    return send_command(device_id, "setColor", f"{rr}:{gg}:{bb}")


def set_color_temperature(kelvin: int, device_id: str | None = None) -> dict:
    """Color temperature 2700 (warm) - 6500 (cool), integer."""
    try:
        k = int(kelvin)
    except (TypeError, ValueError):
        return {"ok": False, "error": f"色温必须是整数，收到: {kelvin!r}"}
    if not 2700 <= k <= 6500:
        return {"ok": False, "error": f"色温须在 2700-6500 之间，收到: {k}"}
    return send_command(device_id, "setColorTemperature", str(k))


# ---------------------------------------------------------------------------
# CLI (manual acceptance once a real token is in config.json)
# ---------------------------------------------------------------------------

def _cli_device_id() -> dict:
    """Resolve a device_id for CLI commands: config first, else auto-detect."""
    if CFG.switchbot_device_id:
        return {"ok": True, "device_id": CFG.switchbot_device_id}
    res = resolve_bulb_id()
    if not res["ok"]:
        return res
    print(f"(自动认出灯泡: {res.get('device_name')} = {res['device_id']}，"
          f"可回填到 config.json 的 switchbot_device_id)", file=sys.stderr)
    return {"ok": True, "device_id": res["device_id"]}


def _pp(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(__doc__)
        return 1

    cmd = argv[0]

    if cmd == "devices":
        res = list_devices()
        _pp(res)
        return 0 if res["ok"] else 1

    # commands below need a device_id
    if cmd in ("status", "on", "off", "toggle",
               "brightness", "color", "temp"):
        dev = _cli_device_id()
        if not dev["ok"]:
            _pp(dev)
            return 1
        device_id = dev["device_id"]

        if cmd == "status":
            _pp(get_status(device_id))
        elif cmd == "on":
            _pp(turn_on(device_id))
        elif cmd == "off":
            _pp(turn_off(device_id))
        elif cmd == "toggle":
            _pp(toggle(device_id))
        elif cmd == "brightness":
            if len(argv) < 2:
                print("用法: brightness N (1-100)", file=sys.stderr)
                return 1
            _pp(set_brightness(argv[1], device_id))
        elif cmd == "color":
            if len(argv) < 4:
                print("用法: color R G B (each 0-255)", file=sys.stderr)
                return 1
            _pp(set_color(argv[1], argv[2], argv[3], device_id))
        elif cmd == "temp":
            if len(argv) < 2:
                print("用法: temp K (2700-6500)", file=sys.stderr)
                return 1
            _pp(set_color_temperature(argv[1], device_id))
        return 0

    print(f"未知命令: {cmd}\n{__doc__}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
