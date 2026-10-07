#!/usr/bin/env python3
"""ViewLink Local bridge.

A tiny local HTTP queue between the ViewLink Chrome extension (producer) and
the ViewLink MetaTrader 5 Expert Advisor (consumer).

- Standard library only, Python 3.8+.
- Listens on 127.0.0.1 only (never on all interfaces).
- Every endpoint except /health requires the X-API-Key header.

Endpoints:
    GET     /health    liveness probe, no auth
    POST    /signal    extension -> queue a validated trade signal
    GET     /next      EA -> next non-expired signal, or {}
    POST    /result    EA -> execution report for a signal
    GET     /results   extension -> last 50 execution reports
    OPTIONS *          CORS preflight
"""

import collections
import hmac
import json
import math
import os
import re
import secrets
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

HOST = "127.0.0.1"
DEFAULT_PORT = 8787
DEFAULT_TTL = 30.0
MAX_BODY_BYTES = 10 * 1024
MAX_RESULTS = 50
MAX_QUEUE = 100
MAX_LOTS = 100.0
MAX_COMMENT_LEN = 31  # MT5 order comment limit
MAX_MESSAGE_LEN = 200

KEY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".viewlink_key")

SYMBOL_RE = re.compile(r"^[A-Za-z0-9._#\-]{1,24}$")
SIGNAL_ID_RE = re.compile(r"^[0-9a-f]{12}$")
COMMENT_STRIP_RE = re.compile(r"[^A-Za-z0-9 ._\-]")
MESSAGE_STRIP_RE = re.compile(r"[^A-Za-z0-9 .,:;_()\[\]/#'=+\-]")

SIDES = ("buy", "sell")
ORDERS = ("market", "limit", "stop")


class ValidationError(ValueError):
    """Raised when a request payload is rejected. The message is user-facing."""


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def _number(payload, name, required=False):
    """Return payload[name] as a finite, non-negative float (0.0 if absent)."""
    value = payload.get(name)
    if value is None or value == "":
        if required:
            raise ValidationError("%s is required" % name)
        return 0.0
    if isinstance(value, bool):
        raise ValidationError("%s must be a number" % name)
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValidationError("%s must be a number" % name)
    if not math.isfinite(number):
        raise ValidationError("%s must be a finite number" % name)
    if number < 0:
        raise ValidationError("%s must not be negative" % name)
    return number


def _choice(payload, name, allowed):
    value = payload.get(name)
    if not isinstance(value, str) or value.strip().lower() not in allowed:
        raise ValidationError("%s must be one of: %s" % (name, ", ".join(allowed)))
    return value.strip().lower()


def sanitize_text(value, max_len, pattern=COMMENT_STRIP_RE):
    """Keep only a safe subset of characters and cap the length."""
    if not isinstance(value, str):
        return ""
    return pattern.sub("", value).strip()[:max_len]


def check_geometry(side, entry, sl, tp):
    """Check SL/TP placement. 0 means 'not set'. entry 0 means 'market price'."""
    if side == "buy":
        if entry > 0 and sl > 0 and not sl < entry:
            raise ValidationError("buy: SL (%g) must be below entry (%g)" % (sl, entry))
        if entry > 0 and tp > 0 and not tp > entry:
            raise ValidationError("buy: TP (%g) must be above entry (%g)" % (tp, entry))
        if sl > 0 and tp > 0 and not sl < tp:
            raise ValidationError("buy: SL (%g) must be below TP (%g)" % (sl, tp))
    else:
        if entry > 0 and sl > 0 and not sl > entry:
            raise ValidationError("sell: SL (%g) must be above entry (%g)" % (sl, entry))
        if entry > 0 and tp > 0 and not tp < entry:
            raise ValidationError("sell: TP (%g) must be below entry (%g)" % (tp, entry))
        if sl > 0 and tp > 0 and not sl > tp:
            raise ValidationError("sell: SL (%g) must be above TP (%g)" % (sl, tp))


def validate_signal(payload):
    """Validate an extension payload and return a clean, flat signal dict."""
    if not isinstance(payload, dict):
        raise ValidationError("body must be a JSON object")

    symbol = payload.get("symbol")
    if not isinstance(symbol, str) or not SYMBOL_RE.match(symbol):
        raise ValidationError("symbol must match [A-Za-z0-9._#-], 1 to 24 characters")

    side = _choice(payload, "side", SIDES)
    order = _choice(payload, "order", ORDERS)

    lots = _number(payload, "lots", required=True)
    if lots <= 0 or lots > MAX_LOTS:
        raise ValidationError("lots must be > 0 and <= %g" % MAX_LOTS)

    entry = _number(payload, "entry")
    if order != "market" and entry <= 0:
        raise ValidationError("entry price is required for %s orders" % order)
    sl = _number(payload, "sl")
    tp = _number(payload, "tp")
    check_geometry(side, entry, sl, tp)

    return {
        "symbol": symbol,
        "side": side,
        "order": order,
        "lots": lots,
        "entry": entry,
        "sl": sl,
        "tp": tp,
        "comment": sanitize_text(payload.get("comment", "ViewLink"), MAX_COMMENT_LEN),
    }


def validate_result(payload):
    """Validate an EA execution report."""
    if not isinstance(payload, dict):
        raise ValidationError("body must be a JSON object")
    signal_id = payload.get("id")
    if not isinstance(signal_id, str) or not SIGNAL_ID_RE.match(signal_id):
        raise ValidationError("id must be a 12-character hex string")
    ok = payload.get("ok")
    if not isinstance(ok, bool):
        raise ValidationError("ok must be a boolean")
    retcode = payload.get("retcode", 0)
    ticket = payload.get("ticket", 0)
    for name, value in (("retcode", retcode), ("ticket", ticket)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValidationError("%s must be a non-negative integer" % name)
    return {
        "id": signal_id,
        "ok": ok,
        "retcode": retcode,
        "ticket": ticket,
        "message": sanitize_text(payload.get("message", ""), MAX_MESSAGE_LEN, MESSAGE_STRIP_RE),
    }


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

class Bridge:
    """Thread-safe signal queue and result store."""

    def __init__(self, api_key, ttl=DEFAULT_TTL, clock=time.time):
        if not api_key:
            raise ValueError("api_key must not be empty")
        self.api_key = api_key
        self.ttl = float(ttl)
        self.clock = clock
        self._lock = threading.Lock()
        self._queue = collections.deque()
        self._results = collections.deque(maxlen=MAX_RESULTS)

    def check_key(self, provided):
        if not provided:
            return False
        return hmac.compare_digest(provided.encode("utf-8"), self.api_key.encode("utf-8"))

    def add_signal(self, payload):
        signal = validate_signal(payload)
        signal["id"] = uuid.uuid4().hex[:12]
        signal["ts"] = round(self.clock(), 3)
        with self._lock:
            self._drop_expired()
            if len(self._queue) >= MAX_QUEUE:
                raise ValidationError("queue is full, is the EA running?")
            self._queue.append(signal)
        return signal

    def next_signal(self):
        """Pop the oldest non-expired signal, or return None."""
        with self._lock:
            self._drop_expired()
            return self._queue.popleft() if self._queue else None

    def add_result(self, payload):
        result = validate_result(payload)
        result["ts"] = round(self.clock(), 3)
        with self._lock:
            self._results.append(result)
        return result

    def results(self):
        with self._lock:
            return list(self._results)

    def _drop_expired(self):
        now = self.clock()
        while self._queue and now - self._queue[0]["ts"] > self.ttl:
            expired = self._queue.popleft()
            log("signal %s expired after %.0fs (not picked up by the EA)" % (expired["id"], self.ttl))
            self._results.append({
                "id": expired["id"], "ok": False, "retcode": 0, "ticket": 0,
                "message": "expired: not picked up by the EA within %.0fs" % self.ttl,
                "ts": round(now, 3),
            })


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------

def dumps(obj):
    """Compact JSON, easy to parse from MQL5."""
    return json.dumps(obj, separators=(",", ":"))


def log(message):
    sys.stderr.write("[viewlink] %s %s\n" % (time.strftime("%H:%M:%S"), message))


class Handler(BaseHTTPRequestHandler):
    server_version = "ViewLinkBridge/1.0"
    bridge = None  # injected by make_server()

    # -- routing ----------------------------------------------------------
    def do_OPTIONS(self):
        self._send(204, None)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/health":
            return self._send(200, {"ok": True, "service": "viewlink-bridge"})
        if not self._authorized():
            return
        if path == "/next":
            signal = self.bridge.next_signal()
            if signal:
                log("signal %s delivered to EA" % signal["id"])
            return self._send(200, signal or {})
        if path == "/results":
            return self._send(200, {"ok": True, "results": self.bridge.results()})
        self._send(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        path = urlsplit(self.path).path
        if path not in ("/signal", "/result"):
            return self._send(404, {"ok": False, "error": "not found"})
        if not self._authorized():
            return
        payload = self._read_json()
        if payload is None:
            return
        try:
            if path == "/signal":
                signal = self.bridge.add_signal(payload)
                log("signal %s queued: %s %s %s %g lots" % (
                    signal["id"], signal["side"], signal["order"], signal["symbol"], signal["lots"]))
                return self._send(200, {"ok": True, "id": signal["id"], "ts": signal["ts"]})
            result = self.bridge.add_result(payload)
            log("result for %s: ok=%s retcode=%d ticket=%d %s" % (
                result["id"], result["ok"], result["retcode"], result["ticket"], result["message"]))
            return self._send(200, {"ok": True})
        except ValidationError as exc:
            log("rejected %s: %s" % (path, exc))
            return self._send(400, {"ok": False, "error": str(exc)})

    # -- helpers ----------------------------------------------------------
    def _authorized(self):
        if self.bridge.check_key(self.headers.get("X-API-Key", "")):
            return True
        log("unauthorized request to %s" % self.path)
        self._send(401, {"ok": False, "error": "invalid or missing API key"})
        return False

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY_BYTES:
            self._send(413, {"ok": False, "error": "body too large (max %d bytes)" % MAX_BODY_BYTES})
            return None
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, ValueError):
            self._send(400, {"ok": False, "error": "body is not valid JSON"})
            return None

    def _cors_headers(self):
        origin = self.headers.get("Origin", "")
        # Only browser extensions get CORS access; web pages never do.
        if origin.startswith("chrome-extension://"):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-API-Key")
            self.send_header("Access-Control-Max-Age", "600")

    def _send(self, status, obj):
        body = b"" if obj is None else dumps(obj).encode("utf-8")
        self.send_response(status)
        self._cors_headers()
        if obj is not None:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def log_message(self, fmt, *args):
        # Default access log is noisy (the EA polls every second); keep it quiet.
        pass


def make_server(bridge, port=DEFAULT_PORT):
    """Build a server bound to 127.0.0.1 only. port=0 picks a free port."""
    handler = type("BoundHandler", (Handler,), {"bridge": bridge})
    return ThreadingHTTPServer((HOST, port), handler)


# ---------------------------------------------------------------------------
# Key management and entry point
# ---------------------------------------------------------------------------

def load_or_create_key(path=KEY_FILE):
    """Return (key, origin). VIEWLINK_KEY wins, then the key file, then a new key."""
    env_key = os.environ.get("VIEWLINK_KEY", "").strip()
    if env_key:
        return env_key, "environment variable VIEWLINK_KEY"
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            key = fh.read().strip()
        if key:
            return key, path
    key = secrets.token_urlsafe(24)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(key + "\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass  # Windows ignores POSIX modes
    return key, path + " (newly generated)"


def env_float(name, default):
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise SystemExit("error: %s must be a number, got %r" % (name, raw))
    if not math.isfinite(value) or value <= 0:
        raise SystemExit("error: %s must be a positive number" % name)
    return value


def main():
    port = int(env_float("VIEWLINK_PORT", DEFAULT_PORT))
    ttl = env_float("VIEWLINK_TTL", DEFAULT_TTL)
    key, origin = load_or_create_key()
    bridge = Bridge(key, ttl=ttl)
    try:
        server = make_server(bridge, port)
    except OSError as exc:
        raise SystemExit("error: cannot listen on %s:%d (%s). Is the bridge already running?"
                         % (HOST, port, exc))
    print("ViewLink bridge listening on http://%s:%d" % (HOST, port))
    print("API key (%s):" % origin)
    print("    %s" % key)
    print("Signal TTL: %.0fs. Press Ctrl+C to stop." % ttl)
    sys.stdout.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping bridge.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
