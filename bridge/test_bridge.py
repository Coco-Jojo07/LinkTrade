"""Tests for the ViewLink bridge.

Run with:  python -m pytest bridge   (or)   python bridge/test_bridge.py
Each test talks to a real server bound to 127.0.0.1 on a random free port.
"""

import json
import os
import stat
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server  # noqa: E402

KEY = "test-key-123"


class FakeClock:
    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now


def valid_signal(**overrides):
    payload = {"symbol": "EURUSD", "side": "buy", "order": "limit", "lots": 0.1,
               "entry": 1.1, "sl": 1.09, "tp": 1.12, "comment": "ViewLink"}
    payload.update(overrides)
    return payload


class BridgeHttpTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.bridge = server.Bridge(KEY, ttl=30, clock=self.clock)
        self.httpd = server.make_server(self.bridge, port=0)
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, args=(0.05,), daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def request(self, method, path, body=None, key=KEY, raw=None, headers=None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if key is not None:
            req.add_header("X-API-Key", key)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        for name, value in (headers or {}).items():
            req.add_header(name, value)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                text = resp.read().decode()
                return resp.status, (json.loads(text) if text else None), resp.headers
        except urllib.error.HTTPError as err:
            text = err.read().decode()
            return err.code, (json.loads(text) if text else None), err.headers

    # -- basics ------------------------------------------------------------
    def test_listens_on_localhost_only(self):
        self.assertEqual(self.httpd.server_address[0], "127.0.0.1")

    def test_health_needs_no_key(self):
        status, body, _ = self.request("GET", "/health", key=None)
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])

    def test_auth_refused(self):
        for key in (None, "", "wrong-key"):
            for method, path, body in (("GET", "/next", None), ("POST", "/signal", valid_signal()),
                                       ("GET", "/results", None), ("POST", "/result", {})):
                status, resp, _ = self.request(method, path, body, key=key)
                self.assertEqual(status, 401, (key, path))
                self.assertFalse(resp["ok"])
        self.assertIsNone(self.bridge.next_signal(), "rejected signal must not be queued")

    # -- signals -----------------------------------------------------------
    def test_valid_signal_roundtrip(self):
        status, body, _ = self.request("POST", "/signal", valid_signal())
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertRegex(body["id"], r"^[0-9a-f]{12}$")

        status, signal, headers = self.request("GET", "/next")
        self.assertEqual(status, 200)
        self.assertEqual(signal["id"], body["id"])
        self.assertEqual(signal["symbol"], "EURUSD")
        self.assertEqual(signal["side"], "buy")
        self.assertEqual(signal["order"], "limit")
        self.assertEqual(signal["lots"], 0.1)
        self.assertEqual(signal["ts"], 1000.0)
        # flat object only, compact separators
        self.assertTrue(all(not isinstance(v, (dict, list)) for v in signal.values()))

    def test_next_payload_is_compact_json(self):
        self.request("POST", "/signal", valid_signal())
        req = urllib.request.Request(self.base + "/next", headers={"X-API-Key": KEY})
        with urllib.request.urlopen(req, timeout=5) as resp:
            text = resp.read().decode()
        self.assertNotIn(": ", text)
        self.assertNotIn(", ", text)

    def test_market_signal_without_entry(self):
        status, body, _ = self.request("POST", "/signal",
                                       valid_signal(order="market", entry=0, sl=1.09, tp=1.12))
        self.assertEqual(status, 200, body)

    def test_side_and_order_are_normalized(self):
        status, _, _ = self.request("POST", "/signal", valid_signal(side="BUY", order="Limit"))
        self.assertEqual(status, 200)
        _, signal, _ = self.request("GET", "/next")
        self.assertEqual((signal["side"], signal["order"]), ("buy", "limit"))

    def test_invalid_symbol(self):
        for symbol in ("", "EUR USD", "EURUSD;rm", "A" * 25, "<script>", 42, None):
            status, body, _ = self.request("POST", "/signal", valid_signal(symbol=symbol))
            self.assertEqual(status, 400, symbol)
            self.assertIn("symbol", body["error"])

    def test_valid_symbol_characters(self):
        for symbol in ("EURUSD.m", "XAUUSD#", "US30-cash", "BTC_USD"):
            status, body, _ = self.request("POST", "/signal", valid_signal(symbol=symbol))
            self.assertEqual(status, 200, (symbol, body))

    def test_invalid_geometry(self):
        cases = [
            valid_signal(side="buy", entry=1.1, sl=1.11, tp=1.12),    # buy SL above entry
            valid_signal(side="buy", entry=1.1, sl=1.09, tp=1.08),    # buy TP below entry
            valid_signal(side="sell", entry=1.1, sl=1.09, tp=1.08),   # sell SL below entry
            valid_signal(side="sell", entry=1.1, sl=1.12, tp=1.11),   # sell TP above entry
            valid_signal(order="market", entry=0, sl=1.12, tp=1.10),  # buy SL above TP
        ]
        for payload in cases:
            status, body, _ = self.request("POST", "/signal", payload)
            self.assertEqual(status, 400, payload)
            self.assertRegex(body["error"], r"(SL|TP)")
        self.assertIsNone(self.bridge.next_signal())

    def test_sell_geometry_ok(self):
        status, body, _ = self.request("POST", "/signal",
                                       valid_signal(side="sell", entry=1.1, sl=1.11, tp=1.08))
        self.assertEqual(status, 200, body)

    def test_invalid_fields(self):
        cases = {
            "side": valid_signal(side="long"),
            "order": valid_signal(order="fok"),
            "lots_zero": valid_signal(lots=0),
            "lots_big": valid_signal(lots=101),
            "lots_text": valid_signal(lots="abc"),
            "lots_bool": valid_signal(lots=True),
            "entry_missing": valid_signal(order="stop", entry=0),
            "negative": valid_signal(sl=-1),
            "nan": valid_signal(tp="nan"),
            "inf": valid_signal(entry="inf"),
        }
        for name, payload in cases.items():
            status, _, _ = self.request("POST", "/signal", payload)
            self.assertEqual(status, 400, name)

    def test_comment_is_sanitized(self):
        self.request("POST", "/signal", valid_signal(comment='hi"}{\\<b>' + "x" * 100))
        _, signal, _ = self.request("GET", "/next")
        self.assertRegex(signal["comment"], r"^[A-Za-z0-9 ._\-]*$")
        self.assertLessEqual(len(signal["comment"]), 31)

    def test_body_too_large(self):
        status, body, _ = self.request("POST", "/signal", raw=b"{" + b" " * 11000 + b"}")
        self.assertEqual(status, 413)

    def test_bad_json(self):
        status, body, _ = self.request("POST", "/signal", raw=b"{not json")
        self.assertEqual(status, 400)
        status, body, _ = self.request("POST", "/signal", body=[1, 2])
        self.assertEqual(status, 400)

    # -- queue -------------------------------------------------------------
    def test_empty_queue(self):
        status, body, _ = self.request("GET", "/next")
        self.assertEqual(status, 200)
        self.assertEqual(body, {})

    def test_fifo_and_single_delivery(self):
        _, first, _ = self.request("POST", "/signal", valid_signal())
        _, second, _ = self.request("POST", "/signal", valid_signal(symbol="GBPUSD"))
        self.assertEqual(self.request("GET", "/next")[1]["id"], first["id"])
        self.assertEqual(self.request("GET", "/next")[1]["id"], second["id"])
        self.assertEqual(self.request("GET", "/next")[1], {})

    def test_stale_signal_expires(self):
        _, body, _ = self.request("POST", "/signal", valid_signal())
        self.clock.now += 30.5
        status, signal, _ = self.request("GET", "/next")
        self.assertEqual(status, 200)
        self.assertEqual(signal, {})
        # the expiry is reported so the extension can show it
        _, results, _ = self.request("GET", "/results")
        expired = [r for r in results["results"] if r["id"] == body["id"]]
        self.assertEqual(len(expired), 1)
        self.assertFalse(expired[0]["ok"])
        self.assertIn("expired", expired[0]["message"])

    def test_signal_within_ttl_is_delivered(self):
        _, body, _ = self.request("POST", "/signal", valid_signal())
        self.clock.now += 29
        self.assertEqual(self.request("GET", "/next")[1]["id"], body["id"])

    # -- results -----------------------------------------------------------
    def test_results_storage(self):
        report = {"id": "abcdef012345", "ok": True, "retcode": 10009, "ticket": 123456,
                  "message": "done"}
        status, body, _ = self.request("POST", "/result", report)
        self.assertEqual(status, 200, body)
        _, results, _ = self.request("GET", "/results")
        self.assertEqual(len(results["results"]), 1)
        stored = results["results"][0]
        for name in ("id", "ok", "retcode", "ticket", "message"):
            self.assertEqual(stored[name], report[name])

    def test_rejection_result_is_stored(self):
        report = {"id": "abcdef012345", "ok": False, "retcode": 10016, "ticket": 0,
                  "message": "Invalid stops"}
        self.assertEqual(self.request("POST", "/result", report)[0], 200)
        self.assertFalse(self.request("GET", "/results")[1]["results"][0]["ok"])

    def test_results_keep_last_50(self):
        for i in range(60):
            self.request("POST", "/result", {"id": "%012x" % i, "ok": True, "retcode": 10009,
                                             "ticket": i, "message": ""})
        results = self.request("GET", "/results")[1]["results"]
        self.assertEqual(len(results), 50)
        self.assertEqual(results[-1]["ticket"], 59)

    def test_invalid_result(self):
        for report in ({"id": "zz", "ok": True}, {"id": "abcdef012345", "ok": "yes"},
                       {"id": "abcdef012345", "ok": True, "ticket": -1}):
            self.assertEqual(self.request("POST", "/result", report)[0], 400, report)

    # -- CORS --------------------------------------------------------------
    def test_cors_only_for_extensions(self):
        status, _, headers = self.request("OPTIONS", "/signal", key=None,
                                          headers={"Origin": "chrome-extension://abc"})
        self.assertEqual(status, 204)
        self.assertEqual(headers["Access-Control-Allow-Origin"], "chrome-extension://abc")
        self.assertIn("X-API-Key", headers["Access-Control-Allow-Headers"])
        _, _, headers = self.request("OPTIONS", "/signal", key=None,
                                     headers={"Origin": "https://evil.example"})
        self.assertIsNone(headers.get("Access-Control-Allow-Origin"))


class KeyFileTest(unittest.TestCase):
    def test_key_is_generated_and_reused(self):
        old = os.environ.pop("VIEWLINK_KEY", None)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                path = os.path.join(tmp, ".viewlink_key")
                key, origin = server.load_or_create_key(path)
                self.assertGreaterEqual(len(key), 24)
                self.assertIn("generated", origin)
                if os.name == "posix":
                    self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
                self.assertEqual(server.load_or_create_key(path)[0], key)
                os.environ["VIEWLINK_KEY"] = "from-env"
                self.assertEqual(server.load_or_create_key(path)[0], "from-env")
        finally:
            os.environ.pop("VIEWLINK_KEY", None)
            if old is not None:
                os.environ["VIEWLINK_KEY"] = old


if __name__ == "__main__":
    unittest.main(verbosity=2)
