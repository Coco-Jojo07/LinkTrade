// ViewLink Local - popup logic.
// Reads the chart via the content script, lets the user check and edit the
// values, and sends ONE signal to the local bridge per click on Execute.
(function () {
  "use strict";

  var DEFAULTS = {
    bridgeUrl: "http://127.0.0.1:8787",
    apiKey: "",
    symbolSuffix: "",
    defaultLots: 0.01
  };
  var RESULT_TIMEOUT_MS = 10000;
  var RESULT_POLL_MS = 500;
  var FETCH_TIMEOUT_MS = 4000;
  var SYMBOL_RE = /^[A-Za-z0-9._#\-]{1,24}$/;
  var BRIDGE_URL_RE = /^http:\/\/(127\.0\.0\.1|localhost)(:\d{1,5})?\/?$/;
  var TYPE_MAP = {
    "1": ["buy", "market"], "2": ["buy", "limit"], "3": ["buy", "stop"],
    "-1": ["sell", "market"], "-2": ["sell", "limit"], "-3": ["sell", "stop"]
  };

  var settings = Object.assign({}, DEFAULTS);
  var armed = false;   // true after a valid chart read
  var sending = false; // true while a signal is in flight

  function $(id) {
    return document.getElementById(id);
  }

  // ------------------------------------------------------------------ UI
  function setStatus(kind, text) {
    var el = $("status");
    el.className = "status " + kind;
    el.textContent = text;
  }

  function setReport(kind, text) {
    var el = $("report");
    if (!text) {
      el.className = "status hidden";
      el.textContent = "";
      return;
    }
    el.className = "status " + kind;
    el.textContent = text;
  }

  function refreshButtons() {
    $("executeBtn").disabled = !armed || sending;
    $("readBtn").disabled = sending;
  }

  // ------------------------------------------------------------ settings
  function loadSettings() {
    return new Promise(function (resolve) {
      chrome.storage.local.get(DEFAULTS, function (stored) {
        settings = Object.assign({}, DEFAULTS, stored || {});
        $("bridgeUrl").value = settings.bridgeUrl;
        $("apiKey").value = settings.apiKey;
        $("symbolSuffix").value = settings.symbolSuffix;
        $("defaultLots").value = settings.defaultLots;
        resolve();
      });
    });
  }

  function saveSettings() {
    var next = {
      bridgeUrl: $("bridgeUrl").value.trim().replace(/\/+$/, ""),
      apiKey: $("apiKey").value.trim(),
      symbolSuffix: $("symbolSuffix").value.trim(),
      defaultLots: parseFloat($("defaultLots").value)
    };
    var error = null;
    if (!BRIDGE_URL_RE.test(next.bridgeUrl)) {
      error = "Bridge URL must be http://127.0.0.1:<port> (local only).";
    } else if (next.symbolSuffix && !/^[A-Za-z0-9._#\-]{1,8}$/.test(next.symbolSuffix)) {
      error = "Symbol suffix may only contain letters, digits and . _ # -";
    } else if (!(next.defaultLots > 0 && next.defaultLots <= 100)) {
      error = "Default lots must be > 0 and <= 100.";
    }
    if (error) {
      $("saveStatus").textContent = error;
      return;
    }
    chrome.storage.local.set(next, function () {
      settings = next;
      $("saveStatus").textContent = chrome.runtime.lastError
        ? "Save failed: " + chrome.runtime.lastError.message
        : "Saved.";
    });
  }

  // ---------------------------------------------------------- chart read
  function queryActiveTab() {
    return new Promise(function (resolve) {
      chrome.tabs.query({ active: true, currentWindow: true }, function (tabs) {
        resolve(tabs && tabs[0]);
      });
    });
  }

  function sendReadMessage(tabId) {
    return new Promise(function (resolve) {
      chrome.tabs.sendMessage(tabId, { type: "VL_READ" }, function (response) {
        if (chrome.runtime.lastError || !response) {
          resolve({ ok: false, error: "Cannot reach the TradingView page. Reload the TradingView " +
            "tab (F5) after installing or updating the extension, then try again." });
          return;
        }
        resolve(response);
      });
    });
  }

  function applySuffix(symbol) {
    var suffix = settings.symbolSuffix || "";
    if (!symbol || !suffix) {
      return symbol;
    }
    return symbol.slice(-suffix.length) === suffix ? symbol : symbol + suffix;
  }

  function fillForm(data) {
    var mapping = TYPE_MAP[String(data.type)];
    $("symbol").value = applySuffix(data.symbol);
    $("side").value = mapping[0];
    $("order").value = mapping[1];
    $("entry").value = data.entry;
    $("sl").value = data.sl;
    $("tp").value = data.tp;
    if (!$("lots").value) {
      $("lots").value = settings.defaultLots;
    }
  }

  async function readChart() {
    armed = false;
    refreshButtons();
    setReport("", "");
    setStatus("info", "Reading chart...");
    var tab = await queryActiveTab();
    if (!tab || !/^https:\/\/([a-z0-9-]+\.)*tradingview\.com\//i.test(tab.url || "")) {
      setStatus("error", "The active tab is not a TradingView chart. Open your chart and try again.");
      return;
    }
    var response = await sendReadMessage(tab.id);
    if (!response.ok) {
      setStatus("error", response.error);
      return;
    }
    fillForm(response.data);
    armed = true;
    refreshButtons();
    var warn = response.data.symbol ? "" : " Symbol not detected: type it manually.";
    setStatus(response.data.symbol ? "ok" : "warn",
      "Chart read. Check every value, then click Execute." + warn);
  }

  // ------------------------------------------------------- order sending
  function collectSignal() {
    var num = function (id) {
      var raw = $(id).value.trim();
      return raw === "" ? 0 : Number(raw);
    };
    return {
      symbol: $("symbol").value.trim(),
      side: $("side").value,
      order: $("order").value,
      lots: num("lots"),
      entry: num("entry"),
      sl: num("sl"),
      tp: num("tp"),
      comment: "ViewLink"
    };
  }

  // Mirrors the bridge validation so the user gets immediate feedback.
  function validateSignal(s) {
    if (!SYMBOL_RE.test(s.symbol)) {
      return "Symbol must be 1 to 24 characters: letters, digits, . _ # -";
    }
    var fields = ["lots", "entry", "sl", "tp"];
    for (var i = 0; i < fields.length; i++) {
      var v = s[fields[i]];
      if (!isFinite(v) || v < 0) {
        return fields[i] + " must be a finite, non-negative number.";
      }
    }
    if (!(s.lots > 0 && s.lots <= 100)) {
      return "Lots must be > 0 and <= 100.";
    }
    if (s.order !== "market" && !(s.entry > 0)) {
      return "Entry price is required for " + s.order + " orders.";
    }
    var buy = s.side === "buy";
    if (s.entry > 0 && s.sl > 0 && (buy ? s.sl >= s.entry : s.sl <= s.entry)) {
      return s.side + ": SL must be " + (buy ? "below" : "above") + " entry.";
    }
    if (s.entry > 0 && s.tp > 0 && (buy ? s.tp <= s.entry : s.tp >= s.entry)) {
      return s.side + ": TP must be " + (buy ? "above" : "below") + " entry.";
    }
    if (s.sl > 0 && s.tp > 0 && (buy ? s.sl >= s.tp : s.sl <= s.tp)) {
      return s.side + ": SL must be " + (buy ? "below" : "above") + " TP.";
    }
    return null;
  }

  async function bridgeFetch(path, options) {
    var controller = new AbortController();
    var timer = setTimeout(function () { controller.abort(); }, FETCH_TIMEOUT_MS);
    var init = Object.assign({ cache: "no-store", signal: controller.signal }, options || {});
    init.headers = Object.assign({ "X-API-Key": settings.apiKey }, init.headers || {});
    try {
      var resp = await fetch(settings.bridgeUrl + path, init);
      var body = null;
      try {
        body = await resp.json();
      } catch (e) {
        body = null;
      }
      return { status: resp.status, body: body };
    } catch (err) {
      throw new Error("Bridge unreachable at " + settings.bridgeUrl +
        ". Is bridge/server.py running?");
    } finally {
      clearTimeout(timer);
    }
  }

  function describeHttpError(resp) {
    if (resp.status === 401) {
      return "API key rejected by the bridge. Copy the key printed by server.py into Settings.";
    }
    var detail = resp.body && resp.body.error ? resp.body.error : "HTTP " + resp.status;
    return "Bridge refused the signal: " + detail;
  }

  function sleep(ms) {
    return new Promise(function (resolve) { setTimeout(resolve, ms); });
  }

  async function waitForResult(id) {
    var deadline = Date.now() + RESULT_TIMEOUT_MS;
    while (Date.now() < deadline) {
      await sleep(RESULT_POLL_MS);
      var resp;
      try {
        resp = await bridgeFetch("/results");
      } catch (err) {
        continue; // transient; keep trying until the deadline
      }
      var list = resp.body && Array.isArray(resp.body.results) ? resp.body.results : [];
      for (var i = list.length - 1; i >= 0; i--) {
        if (list[i].id === id) {
          return list[i];
        }
      }
    }
    return null;
  }

  function showResult(id, result) {
    if (!result) {
      setReport("warn", "No execution report after " + RESULT_TIMEOUT_MS / 1000 + " s for signal " +
        id + ". Check that the EA is running on MT5 and look at its Experts log. The signal " +
        "expires automatically if it is not picked up.");
    } else if (result.ok) {
      setReport("ok", "Executed. Ticket " + result.ticket + " (retcode " + result.retcode + "). " +
        (result.message || ""));
    } else {
      setReport("error", "Rejected: " + (result.message || "no message") +
        (result.retcode ? " (retcode " + result.retcode + ")" : ""));
    }
  }

  async function execute(event) {
    event.preventDefault();
    if (!armed || sending) {
      return; // double-click guard
    }
    var signal = collectSignal();
    var error = validateSignal(signal);
    if (error) {
      setReport("error", error);
      return;
    }
    if (!settings.apiKey) {
      setReport("error", "No API key. Open Settings and paste the key printed by server.py.");
      return;
    }
    sending = true;
    armed = false; // a new chart read is required for the next order
    refreshButtons();
    setReport("info", "Sending " + signal.side + " " + signal.order + " " + signal.lots + " " +
      signal.symbol + "...");
    try {
      var resp = await bridgeFetch("/signal", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(signal)
      });
      if (resp.status !== 200 || !resp.body || !resp.body.ok) {
        setReport("error", describeHttpError(resp));
        return;
      }
      setReport("info", "Signal " + resp.body.id + " queued. Waiting for MT5...");
      showResult(resp.body.id, await waitForResult(resp.body.id));
    } catch (err) {
      setReport("error", err.message);
    } finally {
      sending = false;
      refreshButtons();
      setStatus("info", "Click Read chart before sending another order.");
    }
  }

  // ------------------------------------------------------------- startup
  document.addEventListener("DOMContentLoaded", async function () {
    $("readBtn").addEventListener("click", readChart);
    $("tradeForm").addEventListener("submit", execute);
    $("saveBtn").addEventListener("click", saveSettings);
    await loadSettings();
    $("lots").value = settings.defaultLots;
    readChart();
  });
})();
