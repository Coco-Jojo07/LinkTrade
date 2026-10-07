// End-to-end test of the Chrome extension against the real bridge.
//
// - Loads extension/ unpacked in Chromium (Playwright).
// - Serves a fake TradingView legend (tests/fixtures) on https://www.tradingview.com/...
//   through request interception, so the real content script is injected.
// - Starts bridge/server.py on 127.0.0.1:8787 and plays the role of the EA.
//
// Run:  NODE_PATH="$(npm root -g)" node tests/extension_e2e.js
"use strict";

const { chromium } = require("playwright");
const { spawn } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");
const crypto = require("crypto");
const assert = require("assert");

const ROOT = path.resolve(__dirname, "..");
const EXT_DIR = path.join(ROOT, "extension");
const BRIDGE = "http://127.0.0.1:8787";
const KEY = "e2e-test-key";
const TV_URL = "https://www.tradingview.com/chart/";

function extensionIdFromPath(dir) {
  // Chromium derives unpacked extension ids from the SHA-256 of the absolute path.
  const hex = crypto.createHash("sha256").update(dir).digest("hex").slice(0, 32);
  return hex.replace(/./g, (c) => String.fromCharCode(97 + parseInt(c, 16)));
}

function fixture(name, values) {
  let html = fs.readFileSync(path.join(__dirname, "fixtures", name), "utf8");
  for (const [k, v] of Object.entries(values || {})) {
    html = html.replace("__" + k.toUpperCase() + "__", v);
  }
  return html;
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function waitForBridge() {
  for (let i = 0; i < 50; i++) {
    try {
      const r = await fetch(BRIDGE + "/health");
      if (r.ok) return;
    } catch (e) { /* not up yet */ }
    await sleep(100);
  }
  throw new Error("bridge did not start");
}

// Minimal fake EA: polls /next and answers according to `mode`.
function startFakeEA() {
  const ea = { mode: "ok", received: [], stop: false };
  (async () => {
    while (!ea.stop) {
      if (ea.mode !== "off") {
        const r = await fetch(BRIDGE + "/next", { headers: { "X-API-Key": KEY } });
        const sig = await r.json();
        if (sig.id) {
          ea.received.push(sig);
          const ok = ea.mode === "ok";
          await fetch(BRIDGE + "/result", {
            method: "POST",
            headers: { "X-API-Key": KEY, "Content-Type": "application/json" },
            body: JSON.stringify({ id: sig.id, ok, retcode: ok ? 10009 : 10016,
              ticket: ok ? 123456 : 0, message: ok ? "done" : "Invalid stops" })
          });
        }
      }
      await sleep(200);
    }
  })();
  return ea;
}

async function main() {
  const bridge = spawn("python3", [path.join(ROOT, "bridge", "server.py")], {
    env: Object.assign({}, process.env, { VIEWLINK_KEY: KEY, VIEWLINK_PORT: "8787" }),
    stdio: ["ignore", "ignore", "inherit"]
  });
  const userDir = fs.mkdtempSync(path.join(os.tmpdir(), "vl-e2e-"));
  let context;
  let ea;
  let currentFixture = { name: "tv_legend.html", values: {} };
  const errors = [];
  try {
    await waitForBridge();
    ea = startFakeEA();

    context = await chromium.launchPersistentContext(userDir, {
      channel: "chromium",
      headless: true,
      args: ["--disable-extensions-except=" + EXT_DIR, "--load-extension=" + EXT_DIR]
    });
    await context.route("https://www.tradingview.com/**", (route) =>
      route.fulfill({ contentType: "text/html", body: fixture(currentFixture.name, currentFixture.values) }));

    // In a test the popup runs as a normal tab, so "the active tab" would be the popup
    // itself. Redirect the active-tab query to the TradingView tab unless ?real=1.
    await context.addInitScript(() => {
      if (location.protocol === "chrome-extension:" && !location.search.includes("real=1")) {
        const original = chrome.tabs.query.bind(chrome.tabs);
        chrome.tabs.query = (q, cb) => original({ url: "https://www.tradingview.com/*" }, cb);
      }
    });

    const extId = extensionIdFromPath(EXT_DIR);
    const popupUrl = "chrome-extension://" + extId + "/popup.html";

    async function openTv(name, values) {
      currentFixture = { name, values };
      for (const p of context.pages()) if (p.url().startsWith(TV_URL)) await p.close();
      const tv = await context.newPage();
      await tv.goto(TV_URL);
      await sleep(300); // content script runs at document_idle
      return tv;
    }

    async function openPopup(query) {
      const popup = await context.newPage();
      popup.on("pageerror", (e) => errors.push("pageerror: " + e.message));
      popup.on("console", (m) => {
        // Chrome logs every non-2xx response; the 401 of step 8 is expected.
        if (m.type() === "error" && !m.text().startsWith("Failed to load resource")) {
          errors.push("console: " + m.text());
        }
      });
      await popup.goto(popupUrl + (query || ""));
      return popup;
    }

    const text = (page, sel) => page.locator(sel).innerText();

    // 1. Popup loads; non-TradingView active tab gives a clear error.
    let popup = await openPopup("?real=1");
    await popup.waitForFunction(() => document.getElementById("status").className.includes("error"));
    assert.match(await text(popup, "#status"), /not a TradingView chart/);
    assert.ok(await popup.locator("#executeBtn").isDisabled());
    console.log("ok - popup loads and reports a non-TradingView tab");

    // 2. Chart without the indicator: clear error, Execute stays disabled.
    await openTv("tv_no_indicator.html");
    await popup.goto(popupUrl);
    await popup.waitForFunction(() => document.getElementById("status").className.includes("error"));
    assert.match(await text(popup, "#status"), /ViewLink/);
    assert.ok(await popup.locator("#executeBtn").isDisabled());
    console.log("ok - missing indicator reported, Execute disabled");

    // 3. Valid read with unicode minus, thin spaces and thousands separators.
    await openTv("tv_legend.html", { entry: "1\u202F234.50", sl: "1,200.00", tp: "1,300.25",
      type: "\u22122.00000" });
    await popup.goto(popupUrl);
    await popup.waitForFunction(() => document.getElementById("status").className.includes("ok"));
    assert.equal(await popup.inputValue("#symbol"), "EURUSD");
    assert.equal(await popup.inputValue("#entry"), "1234.5");
    assert.equal(await popup.inputValue("#sl"), "1200");
    assert.equal(await popup.inputValue("#tp"), "1300.25");
    assert.equal(await popup.inputValue("#side"), "sell");
    assert.equal(await popup.inputValue("#order"), "limit");
    assert.ok(!(await popup.locator("#executeBtn").isDisabled()));
    console.log("ok - legend values parsed (sell limit from type -2)");

    // 4. Settings saved, then an incoherent order is blocked client side.
    await popup.locator("#settings summary").click();
    await popup.fill("#apiKey", KEY);
    await popup.fill("#symbolSuffix", ".m");
    await popup.click("#saveBtn");
    await popup.waitForFunction(() => document.getElementById("saveStatus").textContent === "Saved.");
    // sell limit with SL 1200 below entry 1234.5 is incoherent
    await popup.click("#executeBtn");
    assert.match(await text(popup, "#report"), /SL must be above entry/);
    assert.equal(ea.received.length, 0);
    console.log("ok - incoherent SL/TP blocked before sending");

    // 5. Coherent buy limit, double click sends exactly one signal, report shown.
    await openTv("tv_legend.html", { entry: "1.08000", sl: "1.07500", tp: "1.09000", type: "2" });
    await popup.goto(popupUrl);
    await popup.waitForFunction(() => document.getElementById("status").className.includes("ok"));
    assert.equal(await popup.inputValue("#symbol"), "EURUSD.m");
    await popup.fill("#lots", "0.10");
    await popup.locator("#executeBtn").dblclick();
    await popup.waitForFunction(() => /Executed|Rejected|No execution/.test(
      document.getElementById("report").textContent), null, { timeout: 12000 });
    assert.match(await text(popup, "#report"), /Executed\. Ticket 123456/);
    assert.equal(ea.received.length, 1, "double click must send a single signal");
    const sig = ea.received[0];
    assert.deepStrictEqual([sig.symbol, sig.side, sig.order, sig.lots, sig.entry, sig.sl, sig.tp],
      ["EURUSD.m", "buy", "limit", 0.1, 1.08, 1.075, 1.09]);
    assert.ok(await popup.locator("#executeBtn").isDisabled(), "a new read is required");
    console.log("ok - signal executed once, ticket displayed");

    // 6. EA rejection is displayed.
    ea.mode = "reject";
    await popup.click("#readBtn");
    await popup.waitForFunction(() => document.getElementById("status").className.includes("ok"));
    await popup.click("#executeBtn");
    await popup.waitForFunction(() => /Rejected/.test(document.getElementById("report").textContent),
      null, { timeout: 12000 });
    assert.match(await text(popup, "#report"), /Invalid stops/);
    console.log("ok - EA rejection displayed");

    // 7. No EA: explicit timeout after 10 s.
    ea.mode = "off";
    await popup.click("#readBtn");
    await popup.waitForFunction(() => document.getElementById("status").className.includes("ok"));
    await popup.click("#executeBtn");
    await popup.waitForFunction(() => /No execution report/.test(
      document.getElementById("report").textContent), null, { timeout: 13000 });
    console.log("ok - timeout reported when the EA does not answer");

    // 8. Wrong API key.
    await popup.locator("#settings summary").click().catch(() => {});
    await popup.fill("#apiKey", "wrong");
    await popup.click("#saveBtn");
    await popup.click("#readBtn");
    await popup.waitForFunction(() => document.getElementById("status").className.includes("ok"));
    await popup.click("#executeBtn");
    await popup.waitForFunction(() => /API key rejected/.test(
      document.getElementById("report").textContent));
    console.log("ok - wrong API key reported");

    assert.deepStrictEqual(errors, [], "no page errors in the popup");
    console.log("ok - no JavaScript errors in the popup");
    console.log("\nAll extension end-to-end checks passed.");
  } finally {
    if (ea) ea.stop = true;
    if (context) await context.close();
    bridge.kill();
    fs.rmSync(userDir, { recursive: true, force: true });
  }
}

main().catch((err) => {
  console.error("FAILED:", err && err.stack ? err.stack : err);
  process.exit(1);
});
