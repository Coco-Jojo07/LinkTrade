#!/usr/bin/env python3
"""Static checks for the extension and the repository.

- manifest.json is valid JSON and matches the expected MV3 contract
- every file referenced by the manifest / popup exists
- no inline script or inline event handler in popup.html
- no external URL loaded by the extension pages
- no emoji in any source file
"""

import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXT = os.path.join(ROOT, "extension")
errors = []


def check(condition, message):
    if not condition:
        errors.append(message)


def check_manifest():
    with open(os.path.join(EXT, "manifest.json"), encoding="utf-8") as fh:
        manifest = json.load(fh)
    check(manifest.get("manifest_version") == 3, "manifest_version must be 3")
    for key in ("name", "version", "description"):
        check(isinstance(manifest.get(key), str) and manifest[key], "missing %s" % key)
    check(re.match(r"^\d+(\.\d+){0,3}$", manifest.get("version", "")), "invalid version format")
    check(sorted(manifest.get("permissions", [])) == ["activeTab", "storage"],
          "permissions must be exactly activeTab, storage")
    check(sorted(manifest.get("host_permissions", [])) ==
          ["http://127.0.0.1:8787/*", "https://*.tradingview.com/*"],
          "unexpected host_permissions")
    popup = manifest.get("action", {}).get("default_popup")
    check(popup == "popup.html", "action.default_popup must be popup.html")
    for cs in manifest.get("content_scripts", []):
        for js in cs.get("js", []):
            check(os.path.isfile(os.path.join(EXT, js)), "missing content script %s" % js)
    check(os.path.isfile(os.path.join(EXT, popup or "")), "missing popup file")


def check_popup_html():
    with open(os.path.join(EXT, "popup.html"), encoding="utf-8") as fh:
        html = fh.read()
    for match in re.finditer(r"<script\b([^>]*)>(.*?)</script>", html, re.S | re.I):
        check("src=" in match.group(1), "inline <script> in popup.html")
        check(match.group(2).strip() == "", "inline script content in popup.html")
    check(not re.search(r"\son[a-z]+\s*=", html, re.I), "inline event handler in popup.html")
    for ref in re.findall(r'(?:src|href)="([^"]+)"', html):
        check(not re.match(r"^[a-z]+:", ref), "external resource in popup.html: %s" % ref)
        check(os.path.isfile(os.path.join(EXT, ref)), "popup.html references missing %s" % ref)


EMOJI_RE = re.compile("[\\U0001F000-\\U0001FAFF\\u2600-\\u27BF\\u2B00-\\u2BFF\\uFE0F]")


def check_no_emoji():
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in (".git", "node_modules", "__pycache__", ".pytest_cache")]
        for name in files:
            if not name.endswith((".py", ".js", ".json", ".html", ".css", ".pine", ".mq5", ".md")):
                continue
            path = os.path.join(base, name)
            with open(path, encoding="utf-8") as fh:
                for lineno, line in enumerate(fh, 1):
                    check(not EMOJI_RE.search(line), "emoji in %s:%d" % (os.path.relpath(path, ROOT), lineno))


def main():
    check_manifest()
    check_popup_html()
    check_no_emoji()
    if errors:
        for e in errors:
            print("FAIL: " + e)
        sys.exit(1)
    print("Extension manifest and static checks passed.")


if __name__ == "__main__":
    main()
