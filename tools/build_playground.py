#!/usr/bin/env python3
"""Regenerate tools/playground-standalone.html: the settings playground with
web/display.html embedded, so it works from file:// with no server.

Run after any change to web/display.html or tools/playground.html:
    python3 tools/build_playground.py
"""
import pathlib

root = pathlib.Path(__file__).resolve().parent.parent
display = (root / "web" / "display.html").read_text()
playground = (root / "tools" / "playground.html").read_text()

# The playground fetches ../web/display.html; embed the template instead and
# serve it from memory. Only the fetch block changes — settings logic is shared.
import re

marker = "  let tpl;\n  try { tpl = await (await fetch('../web/display.html')).text(); }\n  catch (e) {\n    $('frame').srcdoc = `<p style=\"font:14px system-ui;color:#f87171;padding:20px\">Could not load ../web/display.html (${e.message}). Serve the repo root over http, e.g. <code>python3 -m http.server</code>, and open /tools/playground.html — or use the standalone build: tools/playground-standalone.html</p>`;\n    return;\n  }\n"
embedded = """  let tpl = window.EMBEDDED_DISPLAY || "";
  if (!tpl) {
    try { tpl = await (await fetch('../web/display.html')).text(); }
    catch (e) {
      $('frame').srcdoc = '<p style="font:14px system-ui;color:#f87171;padding:20px">Could not load ../web/display.html — serve the repo root over http for the served variant</p>';
      return;
    }
  }
"""
if marker not in playground:
    raise SystemExit("fetch/catch block not found in tools/playground.html — layout changed?")
playground = playground.replace(marker, embedded, 1)
# Embed the display template as a JSON string on window.EMBEDDED_DISPLAY,
# before the main script runs.
payload = (
    "<script>window.EMBEDDED_DISPLAY = "
    + __import__("json").dumps(display)
    .replace("<", "\\u003c")
    + ";</script>\n</head>"
)
playground = playground.replace("</head>", payload, 1)

out = root / "tools" / "playground-standalone.html"
out.write_text(playground)
print(f"wrote {out} ({out.stat().st_size} bytes)")
