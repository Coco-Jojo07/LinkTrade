// ViewLink Local - content script.
// Reads the ViewLink indicator values from the TradingView chart legend and
// answers {type: "VL_READ"} messages from the popup. It never sends orders.
(function () {
  "use strict";

  if (window.__viewlinkContentLoaded) {
    return;
  }
  window.__viewlinkContentLoaded = true;

  var INDICATOR_KEYWORD = "viewlink";
  var EXPECTED_VALUES = 4; // Entry, SL, TP, Type
  var SYMBOL_RE = /^[A-Za-z0-9._#\-]{1,24}$/;

  // ---------------------------------------------------------------------
  // DOM selectors. TradingView changes its markup regularly: when the
  // extension stops reading values, these three functions are the only
  // place that should need an update.
  // ---------------------------------------------------------------------

  // Every indicator (and the main series) has one legend item per pane.
  function findLegendItems() {
    return Array.prototype.slice.call(
      document.querySelectorAll('[data-name="legend-source-item"]')
    );
  }

  // Title node of a legend item, e.g. "ViewLink Buy Market 0 0 0 14 1.5 2".
  function getLegendTitle(item) {
    var node = item.querySelector('[data-name="legend-source-title"]');
    return node ? (node.textContent || "").trim() : "";
  }

  // Value nodes of a legend item, one per visible plot (classes are hashed,
  // e.g. "valueValue-l31H9iuA", hence the substring match).
  function getLegendValueNodes(item) {
    return Array.prototype.slice.call(item.querySelectorAll('[class*="valueValue"]'));
  }

  // ---------------------------------------------------------------------
  // Parsing helpers
  // ---------------------------------------------------------------------

  // Convert a legend string such as "\u22121 234.50" into a number, or NaN.
  function parseLegendNumber(text) {
    if (typeof text !== "string") {
      return NaN;
    }
    var s = text
      .replace(/[\u2212\u2012\u2013\u2014\uFE63\uFF0D]/g, "-") // unicode minus signs
      .replace(/[\s\u00A0\u202F\u2009\u2007]/g, "") // spaces, incl. thin/no-break
      .replace(/[\u2018\u2019']/g, ""); // apostrophe thousands separators
    var hasComma = s.indexOf(",") !== -1;
    var hasDot = s.indexOf(".") !== -1;
    if (hasComma && hasDot) {
      // The separator appearing last is the decimal one.
      if (s.lastIndexOf(",") > s.lastIndexOf(".")) {
        s = s.replace(/\./g, "").replace(",", ".");
      } else {
        s = s.replace(/,/g, "");
      }
    } else if (hasComma) {
      // "1,234" or "1,234,567" -> thousands; "1,23456" -> decimal comma.
      s = /^-?\d{1,3}(,\d{3})+$/.test(s) ? s.replace(/,/g, "") : s.replace(",", ".");
    }
    if (!/^-?\d+(\.\d+)?$/.test(s)) {
      return NaN;
    }
    return parseFloat(s);
  }

  // "FX:EURUSD 1.08 +0.1% Unnamed" -> "EURUSD".
  function symbolFromTitle(title) {
    var first = (title || "").trim().split(/\s+/)[0] || "";
    var parts = first.split(":");
    var symbol = parts[parts.length - 1].replace(/[^A-Za-z0-9._#\-]/g, "");
    return SYMBOL_RE.test(symbol) ? symbol.toUpperCase() : "";
  }

  function findViewLinkItem() {
    var items = findLegendItems();
    for (var i = 0; i < items.length; i++) {
      if (getLegendTitle(items[i]).toLowerCase().indexOf(INDICATOR_KEYWORD) !== -1) {
        return { item: items[i], legendCount: items.length };
      }
    }
    return { item: null, legendCount: items.length };
  }

  function readValues(item) {
    var texts = getLegendValueNodes(item)
      .map(function (node) {
        return (node.textContent || "").trim();
      })
      .filter(function (text) {
        return text.length > 0;
      });
    if (texts.length < EXPECTED_VALUES) {
      return { error: "Found " + texts.length + " values in the ViewLink legend, expected 4 " +
        "(Entry, SL, TP, Type). Make sure all 4 plots are shown in the status line." };
    }
    var numbers = texts.slice(0, EXPECTED_VALUES).map(parseLegendNumber);
    var names = ["Entry", "SL", "TP", "Type"];
    for (var i = 0; i < numbers.length; i++) {
      if (!isFinite(numbers[i])) {
        return { error: names[i] + ' value "' + texts[i] + '" is not a number. ' +
          "Wait for the chart to load or move the cursor away from the chart." };
      }
    }
    return { values: numbers };
  }

  function readChart() {
    var found = findViewLinkItem();
    if (!found.item) {
      var hint = found.legendCount === 0
        ? "No chart legend found. Open a chart on TradingView (the DOM may also have changed)."
        : 'No indicator whose title contains "ViewLink" was found in the chart legend. ' +
          "Add the ViewLink Signal indicator to the chart.";
      return { ok: false, error: hint };
    }
    var read = readValues(found.item);
    if (read.error) {
      return { ok: false, error: read.error };
    }
    var v = read.values;
    var type = Math.round(v[3]);
    if ([1, 2, 3, -1, -2, -3].indexOf(type) === -1) {
      return { ok: false, error: "Invalid Type code " + v[3] + " (expected 1, 2, 3, -1, -2 or -3)." };
    }
    return {
      ok: true,
      data: {
        symbol: symbolFromTitle(document.title),
        entry: v[0],
        sl: v[1],
        tp: v[2],
        type: type
      }
    };
  }

  chrome.runtime.onMessage.addListener(function (message, sender, sendResponse) {
    if (!message || message.type !== "VL_READ") {
      return false;
    }
    try {
      sendResponse(readChart());
    } catch (err) {
      sendResponse({ ok: false, error: "Unexpected error while reading the chart: " + err.message });
    }
    return false;
  });
})();
