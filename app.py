"""Small, dependency-free configuration UI and RSS news display server for the add-on.

Mirrors HA-Kiosk-Navigation: stdlib-only HTTP server, per-dashboard configs stored
in /data/dashboards.json, ingress auth plus shared access token for direct kiosk
connections, and a display page with the per-dashboard config injected into it.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import xml.etree.ElementTree as ET
from html import escape
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse
from urllib.request import Request, urlopen

DATA_FILE = Path(os.environ.get("KIOSK_NEWS_DATA", "/data/dashboards.json"))
OPTIONS_FILE = Path("/data/options.json")
DISPLAY_FILE = Path("/app/web/display.html")
PORT = 8098

DEFAULTS = {
    "refreshInterval": "60",        # minutes between RSS re-fetches
    "storySeconds": "15",           # seconds each story is shown
    "maxStories": "10",
    "title": "",
    "titlePosition": "top",         # top | bottom
    "titleSize": "medium",
    "titleFont": "system",
    "textSize": "large",            # headline/brief size
    "textContent": "title",         # title | brief
    "imagePosition": "full",        # full | top | bottom | left | right
    "showSource": "true",
    "showDate": "true",
    "showProgress": "true",
    "progressColor": "#38bdf8",
    "proxyImages": "true",
    "requireImage": "false",        # drop stories without an image
    "minImageRes": "any",           # any | medium (50% of screen) | high (85% of screen)
    "shuffle": "false",
    "swipeNavigation": "true",
    "background": "dark",
    "shadowStyle": "vignette",      # vignette | edge (text-side) | off
    "shadowOpacity": "5",
    "shadowReach": "auto",          # auto (follow text) | 30 | 50 | 75 | 100 (% of screen)
}

SOURCE_NAMES = ["sourceName", "sourceUrl", "sourceImage"]


def load_dashboards() -> list[dict]:
    try:
        value = json.loads(DATA_FILE.read_text())
        return value if isinstance(value, list) else []
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_dashboards(dashboards: list[dict]) -> None:
    temporary = DATA_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(dashboards, indent=2) + "\n")
    temporary.replace(DATA_FILE)


GLOBAL_FEEDS_FILE = DATA_FILE.parent / "global_feeds.json"


def load_global_feeds() -> list[dict]:
    """Named RSS feeds shared across all displays; selectable per dashboard."""
    try:
        value = json.loads(GLOBAL_FEEDS_FILE.read_text())
        return value if isinstance(value, list) else []
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_global_feeds(feeds: list[dict]) -> None:
    temporary = GLOBAL_FEEDS_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(feeds, indent=2) + "\n")
    temporary.replace(GLOBAL_FEEDS_FILE)


def clean_global_feed(payload: dict, existing: dict | None = None) -> dict:
    name = str(payload.get("name", "")).strip()
    url = str(payload.get("url", "")).strip()
    if not name:
        raise ValueError("Feed name is required.")
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError("Feed URLs must start with http:// or https://.")
    image = str(payload.get("fallbackImage", "")).strip()
    if image and not image.lower().startswith(("http://", "https://")):
        raise ValueError("Fallback image URLs must start with http:// or https://.")
    raw_id = str(existing["id"] if existing else (payload.get("id") or name)).lower()
    identifier = re.sub(r"[^a-z0-9-]+", "-", raw_id).strip("-")[:48]
    if not identifier:
        raise ValueError("The feed name does not produce a valid ID.")
    return {"id": identifier, "name": name, "url": url, "fallbackImage": image}


GLOBAL_KEYWORDS_FILE = DATA_FILE.parent / "global_keywords.json"


def load_global_keywords() -> list[str]:
    """Blacklist keywords shared across all displays; hidden stories everywhere."""
    try:
        value = json.loads(GLOBAL_KEYWORDS_FILE.read_text())
        return value if isinstance(value, list) else []
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_global_keywords(keywords: list[str]) -> None:
    temporary = GLOBAL_KEYWORDS_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(keywords, indent=2) + "\n")
    temporary.replace(GLOBAL_KEYWORDS_FILE)


def clean_keywords(value) -> list[str]:
    """Accept a list or comma/newline-separated string; lower-cased, unique."""
    parts = [str(part) for value in ([value] if isinstance(value, str) else value if isinstance(value, list) else []) for part in re.split(r"[,;\n]+", str(value))]
    out: list[str] = []
    for part in parts:
        word = str(part).strip().lower()
        if word and word not in out:
            out.append(word)
    return out


def access_token() -> str:
    """Shared access token for direct (non-ingress) connections — same scheme as
    HA-Kiosk-Navigation. Lives in its own file so HA rewriting options.json on
    restart never regenerates it and breaks saved kiosk links."""
    token_file = OPTIONS_FILE.parent / "access_token"
    try:
        token = token_file.read_text().strip()
        if token:
            return token
    except FileNotFoundError:
        pass
    token = secrets.token_urlsafe(24)
    try:
        token_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = token_file.with_suffix(".tmp")
        temporary.write_text(token + "\n")
        temporary.replace(token_file)
    except OSError:
        return ""
    return token


def clean_dashboard(payload: dict, existing: dict | None = None) -> dict:
    name = str(payload.get("name", "")).strip()
    if not name:
        raise ValueError("Name is required.")
    sources = []
    for index in range(6):
        url = str(payload.get(f"sourceUrl{index}" if index else "sourceUrl", "")).strip()
        if not url:
            continue
        if not url.lower().startswith(("http://", "https://")):
            raise ValueError("Feed URLs must start with http:// or https://.")
        label = str(payload.get(f"sourceName{index}" if index else "sourceName", "")).strip()
        image = str(payload.get(f"sourceImage{index}" if index else "sourceImage", "")).strip()
        if image and not image.lower().startswith(("http://", "https://")):
            raise ValueError("Fallback image URLs must start with http:// or https://.")
        sources.append({"name": label or "News", "url": url, "fallbackImage": image})
    if not sources:
        raise ValueError("At least one RSS feed URL is required.")
    kind = str(payload.get("kind") or (existing or {}).get("kind") or "full").strip().lower()
    if kind not in ("full", "compact"):
        kind = "full"
    raw_id = str(existing["id"] if existing else (payload.get("id") or name)).lower()
    identifier = re.sub(r"[^a-z0-9-]+", "-", raw_id).strip("-")[:48]
    if not identifier:
        raise ValueError("The dashboard name does not produce a valid ID.")
    values = {"id": identifier, "name": name, "sources": sources, "kind": kind,
              "blacklist": clean_keywords(payload.get("blacklist", (existing or {}).get("blacklist", [])))}
    for key, default in DEFAULTS.items():
        values[key] = str(payload.get(key, (existing or {}).get(key, default))) or default
    return values


def fetch_feed(url: str) -> list[dict]:
    """Fetch and parse one RSS/Atom feed into story dicts (stdlib xml only)."""
    request = Request(url, headers={"User-Agent": "Mozilla/5.0 (KioskNews/0.1)"})
    with urlopen(request, timeout=12) as response:
        data = response.read(2_000_000)
    root = ET.fromstring(data)

    def strip_ns(tag: str) -> str:
        return tag.rsplit("}", 1)[-1]

    items = []
    for entry in root.iter():
        tag = strip_ns(entry.tag)
        if tag not in ("item", "entry"):
            continue
        story = {"title": "", "summary": "", "image": "", "link": "", "date": ""}
        best = {"width": -1, "url": ""}  # pick the largest of several offered sizes

        def consider(url: str, width: str = "") -> None:
            try:
                w = int(width or 0)
            except ValueError:
                w = 0
            if url and w >= best["width"]:
                best["width"], best["url"] = w, url

        for child in entry:
            name = strip_ns(child.tag)
            text = (child.text or "").strip()
            if name == "title":
                story["title"] = text
            elif name in ("description", "summary"):
                if not story["summary"]:
                    story["summary"] = re.sub(r"<[^>]+>", " ", text).strip()
            elif name == "link":
                href = child.get("href", "")
                story["link"] = href or text
            elif name in ("pubDate", "published", "updated", "date"):
                if not story["date"]:
                    story["date"] = text
            elif name in ("thumbnail", "image", "picture"):
                consider(child.get("url", ""), child.get("width", ""))
            elif name == "content" and child.get("url"):  # media:content with url attr
                consider(child.get("url", ""), child.get("width", ""))
            elif name in ("encoded", "content"):  # content:encoded
                if not story["summary"]:
                    story["summary"] = re.sub(r"<[^>]+>", " ", text).strip()
            elif name == "enclosure":
                if (child.get("type", "").startswith("image/") or
                        (child.get("url", "").lower().endswith((".jpg", ".jpeg", ".png", ".webp")) and not child.get("type"))):
                    consider(child.get("url", ""), child.get("width", ""))
            elif name == "group":  # media:group wraps thumbnail/content one level down
                for sub in child:
                    sub_name = strip_ns(sub.tag)
                    if sub_name in ("thumbnail", "content") and sub.get("url"):
                        consider(sub.get("url", ""), sub.get("width", ""))
        story["image"] = best["url"]
        story["imageWidth"] = best["width"]  # -1 = unknown; used by min-resolution filtering
        # encoded HTML often carries the image; scan for it
        if not story["image"]:
            match = re.search(r"<img[^>]+src=[\"']([^\"']+)", story["summary"]) if story["summary"] else None
            raw = ""
            for child in entry:
                if strip_ns(child.tag) in ("encoded", "content", "description"):
                    raw += child.text or ""
            match = match or re.search(r"<img[^>]+src=[\"']([^\"']+)", raw)
            if match:
                story["image"] = match.group(1)
        if story["title"]:
            story["summary"] = story["summary"][:600]
            items.append(story)
    return items


def proxy_image_url(url: str, token: str) -> str:
    """Route external images through this add-on so kiosks on an isolated VLAN
    (no internet) still get them. Includes the access token so direct
    connections authorize; through ingress the token is simply ignored."""
    from urllib.parse import quote
    return f"/api/image?url={quote(url, safe='')}&auth={token}"


def aggregate(sources: list[dict], config: dict | None = None) -> dict:
    """Fetch all sources; interleave their stories. Returns {stories, errors}."""
    config = config or {}
    proxy_images = str(config.get("proxyImages", "true")) != "false"
    require_image = str(config.get("requireImage", "false")) == "true"
    token = access_token()
    stories: list[dict] = []
    errors: list[str] = []
    dropped = 0
    blocked = 0
    keywords = clean_keywords(config.get("blacklist", []))
    for source in sources:
        try:
            fetched = fetch_feed(source["url"])
        except Exception as error:  # noqa: BLE001 - report per-source, never fail the display
            errors.append(f"{source['name']}: {error}")
            continue
        for position, story in enumerate(fetched):
            image = story["image"] or source.get("fallbackImage", "")
            if require_image and not image:
                dropped += 1
                continue
            # Blacklist: hide stories whose title or summary contains a keyword.
            haystack = f"{story['title']} {story['summary']}".lower()
            if any(word in haystack for word in keywords):
                blocked += 1
                continue
            stories.append({
                "title": story["title"],
                "summary": story["summary"],
                "image": (proxy_image_url(image, token) if proxy_images else image) if image else "",
                "imageWidth": story.get("imageWidth", -1),  # for device-relative quality filter
                "link": story["link"],
                "date": story["date"],
                "source": source["name"],
            })
    if dropped:
        errors.append(f"{dropped} story/stories hidden by image filter")
    if blocked:
        errors.append(f"{blocked} story/stories hidden by keywords")
    # Interleave so a chatty source doesn't dominate; keep order stable.
    by_source: dict[str, list[dict]] = {}
    for story in stories:
        by_source.setdefault(story["source"], []).append(story)
    interleaved = []
    while any(by_source.values()):
        for queue in list(by_source.values()):
            if queue:
                interleaved.append(queue.pop(0))
    return {"stories": interleaved, "errors": errors}


def admin_page() -> str:
    return r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Kiosk News Displays</title><style>
body{max-width:940px;margin:0 auto;padding:28px;font:16px system-ui,sans-serif;background:#0f172a;color:#f8fafc}h1{margin-bottom:4px}p{color:#cbd5e1}section{margin:24px 0;padding:22px;border:1px solid #334155;border-radius:12px;background:#1e293b}form{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}label{display:grid;gap:5px;color:#cbd5e1;font-size:.9rem}input,select,button{padding:10px;border-radius:7px;font:inherit}input,select{border:1px solid #64748b;background:#0f172a;color:white}button{border:0;background:#38bdf8;color:#082f49;font-weight:700;cursor:pointer}.wide{grid-column:1/-1}.row{display:block;border-top:1px solid #334155;padding:15px 0}.row:first-child{border:0}.row strong{font-size:1.05rem}.dash-top{display:flex;align-items:baseline;gap:12px;margin-bottom:8px}.dash-top small{color:#94a3b8;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.link-line{display:flex;align-items:center;gap:10px;margin:6px 0}.link-label{min-width:88px;color:#94a3b8;font-size:.84rem;flex-shrink:0}.link-url{flex:1;color:#7dd3fc;font-size:.86rem;text-decoration:none;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.link-url:hover{text-decoration:underline}.copy{flex-shrink:0;background:#334155;color:#e2e8f0;padding:6px 12px;font-size:.9rem;cursor:pointer;border-radius:6px;border:0}.copy:hover{background:#475569}.tag{display:inline-flex;align-items:center;gap:6px;background:#0b1222;border:1px solid #334155;border-radius:999px;padding:5px 12px;font-size:.9rem}.tag button{background:none;border:0;color:#94a3b8;cursor:pointer;font-size:1rem;padding:0;line-height:1}.tag button:hover{color:#fca5a5}.tag-input{background:transparent;border:0;color:#f8fafc;font:inherit;outline:none;min-width:140px;flex:1}.dash-actions{display:flex;gap:10px;margin-top:10px}.dash-actions button{padding:8px 18px}.secondary{background:#334155;color:#fff}.danger{background:#b91c1c;color:#fff}.modal-overlay{position:fixed;inset:0;z-index:100;display:grid;justify-items:center;align-items:start;padding:20px;background:rgba(3,7,18,.85);overflow-y:auto}.modal-overlay[hidden]{display:none}.modal-content{width:min(100%,680px);max-height:calc(100vh-40px);overflow:auto;padding:24px;border-radius:14px;background:#1e293b}.modal-content h2{margin-top:0}.source-block{grid-column:1/-1;border:1px solid #334155;border-radius:10px;padding:12px;background:#0b1222;display:grid;gap:10px}.source-block h4{margin:0;color:#e2e8f0}.source-block .source-fields{display:grid;grid-template-columns:1fr 1fr;gap:10px}.source-block .source-fields .wide-field{grid-column:1/-1}.source-remove{background:#7f1d1d;color:#fff;border:0;border-radius:7px;padding:8px 14px;cursor:pointer;font:inherit;justify-self:start}.toast{position:fixed;bottom:24px;left:50%;transform:translateX(-50%);background:#22c55e;color:#052e16;padding:12px 22px;border-radius:10px;font-weight:700;z-index:300;display:flex;gap:8px;align-items:center}#preview-overlay{position:fixed;inset:0;z-index:200;background:rgba(3,7,18,.88);display:grid;place-items:center;padding:24px}#preview-overlay[hidden]{display:none}#preview-wrap{width:min(96vw,1400px);height:min(92vh,1000px);display:flex;flex-direction:column;background:#0f172a;border:1px solid #334155;border-radius:12px;overflow:hidden;box-shadow:0 24px 60px rgba(0,0,0,.5)}#preview-bar{display:flex;align-items:center;justify-content:space-between;padding:10px 16px;background:#1e293b;border-bottom:1px solid #334155;color:#e2e8f0}#preview-frame{flex:1;border:0;width:100%;background:#0b1222}</style></head><body>
<h1>Kiosk News Displays</h1><p>Create named news displays from RSS feeds; point a kiosk or dashboard iframe at the display link. No API keys needed.</p>
<section><h2>Your news displays</h2><div class="new-buttons"><button id="new-full" class="wide-button">＋ Add Full Screen Display</button><button id="new-compact" class="wide-button">＋ Add Card</button></div></section>
<section><h2>Global RSS feeds</h2><p>Reusable feeds available to every display. Add them here once, then tick the ones a display should use.</p><div id="feed-list">Loading…</div><form id="feed-form" style="margin-top:14px;grid-template-columns:repeat(3,minmax(0,1fr))"><input id="feed-edit-id" type="hidden"><label>Name<input id="feed-name" placeholder="BBC News" required></label><label>RSS feed URL<input id="feed-url" type="url" placeholder="https://feeds.bbci.co.uk/news/rss.xml" required></label><label>Fallback image URL (optional)<input id="feed-image" type="url" placeholder="https://…jpg"></label><div style="grid-column:1/-1;display:flex;gap:10px"><button type="submit" id="feed-save">Add feed</button><button type="button" id="feed-cancel" class="secondary" hidden>Cancel edit</button></div></form></section>
<section><h2>Blacklist keywords</h2><p>Stories whose title or summary contains any of these words are hidden on every display. New displays start with a copy of this list; each display can then add its own keywords or import the latest global list without losing its own.</p><div id="keyword-list" style="display:flex;flex-wrap:wrap;gap:8px;margin-top:10px"></div><form id="keyword-form" style="margin-top:14px;display:flex;gap:10px"><input id="keyword-input" style="flex:1" placeholder="Add keyword(s) — comma separated"><button type="submit">Add</button></form></section>
<section><h2>Displays</h2><p>Use <em>Full screen</em> for a wall/tablet display and <em>Compact</em> for a dashboard iframe card. Ingress URLs work within Home Assistant; direct URLs require this app's port to be reachable on your LAN.</p><div id="list">Loading…</div></section>
<div id="editor-modal" class="modal-overlay" hidden><div class="modal-content"><h2 id="modal-title">New display</h2><form id="editor"><input id="edit-id" type="hidden"><label>Name<input name="name" required placeholder="Morning headlines"></label><div id="sources" style="grid-column:1/-1;display:grid;gap:12px"></div><button type="button" id="add-source" class="secondary wide">＋ Add another feed</button><h3 class="wide">Presentation</h3><label>Text shown<select name="textContent"><option value="title">Headline only</option><option value="brief">Brief / summary</option></select></label><label>Image position<select name="imagePosition"><option value="full">Full screen image, text over it</option><option value="top">Image top, text below</option><option value="bottom">Image bottom, text above</option><option value="left">Image left, text right</option><option value="right">Image right, text left</option></select></label><label>Text size<select name="textSize"><option value="small">Small</option><option value="medium">Medium</option><option value="large">Large</option><option value="xlarge">Extra large</option></select></label><label>Theme<select name="background"><option value="dark">Dark</option><option value="light">Light</option></select></label><label>Optional title<input name="title" placeholder="e.g. Headlines"></label><label>Title position<select name="titlePosition"><option value="top">Top</option><option value="bottom">Bottom</option></select></label><label>Title font<select name="titleFont"><option value="system">System sans</option><option value="serif">Serif</option><option value="mono">Monospace</option></select></label><label>Show source name<select name="showSource"><option value="true">Yes</option><option value="false">No</option></select></label><label>Show date<select name="showDate"><option value="true">Yes</option><option value="false">No</option></select></label><label>Progress bar<select name="showProgress"><option value="true">Yes</option><option value="false">No</option></select></label><label>Progress bar color<input name="progressColor" type="color" value="#38bdf8"></label><label>Image loading<select name="proxyImages"><option value="true">Through Home Assistant (works on isolated VLANs)</option><option value="false">Directly from the internet (faster, needs internet on the device)</option></select></label><label>Stories without image<select name="requireImage"><option value="false">Show with fallback image</option><option value="true">Hide entirely</option></select></label><label>Minimum image resolution<select name="minImageRes"><option value="any">Any</option><option value="medium">Medium (50% of screen)</option><option value="high">High (85% of screen)</option></select></label><label>Story order<select name="shuffle"><option value="false">Feed order</option><option value="true">Shuffled</option></select></label><label>Swipe navigation<select name="swipeNavigation"><option value="true">Enabled (swipe left/right to change story)</option><option value="false">Disabled</option></select></label><label>Seconds per story<input name="storySeconds" type="number" min="3" max="120" value="15"></label><label>Refresh feeds every (minutes)<input name="refreshInterval" type="number" min="1" max="1440" value="60"></label><label>Max stories<input name="maxStories" type="number" min="1" max="50" value="10"></label><label>Shadow<select name="shadowStyle"><option value="vignette">Vignette (edges of the whole screen)</option><option value="edge">Text-side (behind title and text only)</option><option value="off">Off</option></select></label><label>Shadow reach (text-side)<select name="shadowReach"><option value="auto">Auto — to just past the text</option><option value="30">30% of screen</option><option value="50">50% of screen</option><option value="75">75% of screen</option><option value="100">Full screen</option></select></label><label>Shadow opacity (1-10)<input name="shadowOpacity" type="number" min="1" max="10" value="5"></label><div class="wide" style="display:grid;gap:8px"><label style="display:flex;align-items:center;gap:12px">Blacklist keywords (stories containing these are hidden)<button type="button" id="import-keywords" class="secondary" style="padding:6px 12px">⤓ Import global</button></label><div id="blacklist-tags" style="display:flex;flex-wrap:wrap;gap:8px;padding:10px;border:1px solid #64748b;border-radius:7px;background:#0f172a"><input id="blacklist-input" class="tag-input" placeholder="Type a keyword and press Enter"></div></div><div class="modal-buttons"><button type="submit">Save</button><button type="button" id="cancel" class="secondary">Cancel</button></div></form></div></div>
<div id="preview-overlay" hidden><div id="preview-wrap"><div id="preview-bar"><strong id="preview-title">Preview</strong><button type="button" id="preview-close" class="secondary">✕ Close</button></div><iframe id="preview-frame" title="Display preview"></iframe></div></div>
<script>const f=document.querySelector('#editor'),list=document.querySelector('#list'),cancel=document.querySelector('#cancel'),modal=document.querySelector('#editor-modal'),modalTitle=document.querySelector('#modal-title'),sources=document.querySelector('#sources');let items=[];let globalFeeds=[];
const base=location.pathname.replace(/\/$/,'');
// Carry the access token into API calls and display links for direct
// (non-ingress) access; through ingress the token is ignored by the server.
const AUTH=new URLSearchParams(location.search).get('auth')||'';
const withAuth=p=>p+(p.includes('?')?'&':'?')+'auth='+encodeURIComponent(AUTH);
// Relative links only: through ingress the path carries a per-session token.
const displayLink=p=>AUTH?withAuth(`${base}/${p.replace(/^\//,'')}`):`${base}/${p.replace(/^\//,'')}`;
const DIRECT_PORT='8098';
const ACCESS_TOKEN='__ACCESS_TOKEN__';
const directLink=p=>ACCESS_TOKEN?`${location.protocol}//${location.hostname}:${DIRECT_PORT}${p}?auth=${encodeURIComponent(ACCESS_TOKEN)}`:'';
async function request(path,options){const r=await fetch(base+withAuth(path),options);const data=await r.json();if(!r.ok)throw Error(data.error||'Request failed');return data}
function field(name,value){const input=f.elements[name];if(input)input.value=value??''}
function sourceBlock(data={}){const div=document.createElement('div');div.className='source-block';const i=sources.children.length;div.innerHTML=`<h4>Feed ${i+1}</h4><div class="source-fields"><label>Name<input name="sourceName${i||''}" placeholder="BBC News" value="${data.name||''}"></label><label>Fallback image URL (for stories without one)<input name="sourceImage${i||''}" placeholder="https://…jpg" value="${data.fallbackImage||''}"></label><label class="wide-field">RSS feed URL<input name="sourceUrl${i||''}" required placeholder="https://feeds.bbci.co.uk/news/rss.xml" value="${data.url||''}"></label></div><button type="button" class="source-remove">Remove feed</button>`;div.querySelector('.source-remove').onclick=()=>{div.remove();renumber()};return div}
function renumber(){[...sources.children].forEach((block,i)=>{block.querySelector('h4').textContent=`Feed ${i+1}`})}
// NOTE: input names stay stable (first block unprefixed, later blocks numbered)
function addSource(data){sources.append(sourceBlock(data))}
function collectSources(){const out=[];for(const block of sources.children){const get=k=>block.querySelector(`input[name^="source${k}"]`)?.value.trim()||'';out.push({name:get('Name'),url:get('Url'),fallbackImage:get('Image')})}return out}
function resetForm(){f.reset();field('edit-id','');sources.innerHTML='';modalTitle.textContent='New display';blacklistTags=[];renderBlacklist()}
function openModal(dashboard,variant){resetForm();
 if(dashboard){for(const[k,v]of Object.entries(dashboard))field(k,v);field('edit-id',dashboard.id);(dashboard.sources||[]).forEach(s=>addSource(s));blacklistTags=[...(dashboard.blacklist||[])];renderBlacklist();modalTitle.textContent=`Edit: ${dashboard.name}`}
 else{addSource();f.dataset.variant=variant||'';blacklistTags=[...globalKeywords];renderBlacklist();modalTitle.textContent=variant==='compact'?'New Card':'New Full Screen Display'}
 renderFeedPicker((dashboard?.sources||[]).map(s=>s.url));
 modal.hidden=false}
function closeModal(){modal.hidden=true;resetForm()}
function showToast(message){const toast=document.createElement('div');toast.className='toast';toast.innerHTML=`<span>✓</span>${message}`;document.body.append(toast);setTimeout(()=>toast.remove(),1300)}
function render(){list.innerHTML=items.length?'':'<p>No displays yet.</p>';for(const d of items){const full=displayLink(`/display/${d.id}`),compact=displayLink(`/card/${d.id}`);const row=document.createElement('div');row.className='row';row.innerHTML=`<div class="dash-top"><strong>${d.name}</strong><small>${d.kind==='compact'?'Card':'Full screen'} · ${(d.sources||[]).length} feed(s)</small></div>${d.kind==='compact'
?`<div class="link-line"><span class="link-label">Card</span><a class="link-url" href="${compact}" target="_blank" rel="noopener">${compact}</a><button class="copy" type="button" data-copy="${compact}">⧉</button></div>${directLink(`/card/${d.id}`)?`<div class="link-line"><span class="link-label">Direct</span><a class="link-url" href="${directLink(`/card/${d.id}`)}" target="_blank" rel="noopener">${directLink(`/card/${d.id}`)}</a><button class="copy" type="button" data-copy="${directLink(`/card/${d.id}`)}" title="Copy direct link (token-authenticated, for kiosks)">⧉</button></div>`:''}`
:`<div class="link-line"><span class="link-label">Full screen</span><a class="link-url" href="${full}" target="_blank" rel="noopener">${full}</a><button class="copy" type="button" data-copy="${full}">⧉</button></div>${directLink(`/display/${d.id}`)?`<div class="link-line"><span class="link-label">Direct</span><a class="link-url" href="${directLink(`/display/${d.id}`)}" target="_blank" rel="noopener">${directLink(`/display/${d.id}`)}</a><button class="copy" type="button" data-copy="${directLink(`/display/${d.id}`)}" title="Copy direct link (token-authenticated, for kiosks)">⧉</button></div>`:''}`}
<div class="dash-actions"><button class="secondary">Edit</button><button class="secondary" data-preview="1">Preview</button><button class="secondary" data-duplicate="1">Duplicate</button><button class="danger">Delete</button></div>`;
 row.querySelector('.secondary[data-preview="1"]').onclick=()=>openPreview(d);
 row.querySelector('.secondary[data-duplicate="1"]').onclick=()=>duplicateRow(d,row);
 row.querySelector('.danger').onclick=rowEditDelete(d,row);
 const edit=row.querySelector('.secondary:not([data-preview])');edit.onclick=()=>openModal(d);
 for(const btn of row.querySelectorAll('.copy'))btn.onclick=async()=>{const url=btn.dataset.copy;try{await navigator.clipboard.writeText(url)}catch(e){const ta=document.createElement('textarea');ta.value=url;document.body.append(ta);ta.select();document.execCommand('copy');ta.remove()}btn.textContent='✓';setTimeout(()=>btn.textContent='⧉',1200)};
 list.append(row)}}
// Two-tap delete (native confirm() is blocked inside HA's sandboxed iframe).
// Duplicate: prefill "<Name> (copy)", let the user rename in the edit modal flow —
// reuse the editor modal so renaming feels natural, then save as a NEW display.
function duplicateRow(d){
 (async()=>{
  // Ask the server to copy with the default name, then open it for renaming.
  let created;
  try{created=await request(`/api/dashboards/${d.id}/duplicate`,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'})}
  catch(e){showToast(e.message);return}
  showToast('Duplicated');await load();
  // Open the copy for renaming; the modal's Save (PUT) keeps its id.
  const fresh=items.find(x=>x.id===created.id);
  if(fresh)openModal(fresh);
 })()
}
function rowEditDelete(d,row){const btn=()=>{};return async function handler(){const del=row.querySelector('.danger');if(del.dataset.armed){del.disabled=true;try{await request(`/api/dashboards/${d.id}`,{method:'DELETE'});showToast('Deleted')}catch(e){showToast(e.message);del.disabled=false;del.dataset.armed='';del.textContent='Delete';return}load()}else{del.dataset.armed='1';del.textContent='Really delete?';setTimeout(()=>{if(del.isConnected&&del.dataset.armed){del.dataset.armed='';del.textContent='Delete'}},3000)}}}
async function load(){items=await request('/api/dashboards');render()}

// ---- Global RSS feeds ----
const feedList=document.querySelector('#feed-list'),feedForm=document.querySelector('#feed-form'),feedEditId=document.querySelector('#feed-edit-id'),feedSaveBtn=document.querySelector('#feed-save'),feedCancelBtn=document.querySelector('#feed-cancel');
function renderFeeds(){feedList.innerHTML=globalFeeds.length?'':'<p style="color:#94a3b8;font-size:.9rem">No global feeds yet — add one below.</p>';for(const g of globalFeeds){const row=document.createElement('div');row.className='row';row.innerHTML=`<div class="dash-top"><strong>${g.name}</strong><small>${g.url}</small></div><div class="dash-actions"><button class="secondary" data-edit-feed="${g.id}">Edit</button><button class="danger" data-del-feed="${g.id}">Delete</button></div>`;row.querySelector('[data-edit-feed]').onclick=()=>{feedEditId.value=g.id;document.querySelector('#feed-name').value=g.name;document.querySelector('#feed-url').value=g.url;document.querySelector('#feed-image').value=g.fallbackImage||'';feedSaveBtn.textContent='Save feed';feedCancelBtn.hidden=false};const del=row.querySelector('[data-del-feed]');del.onclick=async()=>{if(del.dataset.armed){del.disabled=true;try{await request(`/api/feeds/${g.id}`,{method:'DELETE'});showToast('Feed deleted');await loadFeeds()}catch(e){showToast(e.message);del.disabled=false;del.dataset.armed='';del.textContent='Delete'}}else{del.dataset.armed='1';del.textContent='Really delete?';setTimeout(()=>{if(del.isConnected&&del.dataset.armed){del.dataset.armed='';del.textContent='Delete'}},3000)}};feedList.append(row)}}
function resetFeedForm(){feedEditId.value='';feedForm.reset();feedSaveBtn.textContent='Add feed';feedCancelBtn.hidden=true}
feedForm.onsubmit=async e=>{e.preventDefault();const id=feedEditId.value;const body={name:document.querySelector('#feed-name').value,url:document.querySelector('#feed-url').value,fallbackImage:document.querySelector('#feed-image').value};await request(id?`/api/feeds/${id}`:'/api/feeds',{method:id?'PUT':'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});showToast(id?'Feed saved':'Feed added');resetFeedForm();await loadFeeds()};
feedCancelBtn.onclick=resetFeedForm;
async function loadFeeds(){globalFeeds=await request('/api/feeds');renderFeeds()}

// ---- Global blacklist keywords ----
let globalKeywords=[];
function tagChip(word,onRemove){const chip=document.createElement('span');chip.className='tag';chip.append(document.createTextNode(word));const x=document.createElement('button');x.type='button';x.textContent='✕';x.title='Remove';x.onclick=onRemove;chip.append(x);return chip}
const keywordList=document.querySelector('#keyword-list');
function renderKeywords(){keywordList.innerHTML='';if(!globalKeywords.length){keywordList.innerHTML='<p style="color:#94a3b8;font-size:.9rem">No global keywords yet — add one below.</p>';return}for(const word of globalKeywords){keywordList.append(tagChip(word,async()=>{globalKeywords=globalKeywords.filter(w=>w!==word);await saveKeywords()}))}}
async function saveKeywords(){globalKeywords=await request('/api/keywords',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({keywords:globalKeywords})});renderKeywords()}
async function loadKeywords(){globalKeywords=await request('/api/keywords');renderKeywords()}
document.querySelector('#keyword-form').onsubmit=async e=>{e.preventDefault();const input=document.querySelector('#keyword-input');const words=input.value.split(/[,;\n]+/).map(s=>s.trim().toLowerCase()).filter(Boolean);for(const w of words)if(!globalKeywords.includes(w))globalKeywords.push(w);input.value='';await saveKeywords();showToast('Keywords saved')};

// ---- Dashboard editor: per-display blacklist tags ----
let blacklistTags=[];
const blacklistTagsEl=document.querySelector('#blacklist-tags'),blacklistInput=document.querySelector('#blacklist-input');
function renderBlacklist(){[...blacklistTagsEl.querySelectorAll('.tag')].forEach(t=>t.remove());for(const word of blacklistTags){blacklistTagsEl.append(tagChip(word,()=>{blacklistTags=blacklistTags.filter(w=>w!==word);renderBlacklist()}))}}
function addBlacklistWords(value){for(const w of String(value).split(/[,;\n]+/).map(s=>s.trim().toLowerCase()).filter(Boolean))if(!blacklistTags.includes(w))blacklistTags.push(w);renderBlacklist()}
blacklistInput.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===','){e.preventDefault();addBlacklistWords(blacklistInput.value);blacklistInput.value=''}});
document.querySelector('#import-keywords').onclick=async()=>{try{globalKeywords=await request('/api/keywords')}catch(e){showToast(e.message);return}const before=blacklistTags.length;for(const w of globalKeywords)if(!blacklistTags.includes(w))blacklistTags.push(w);renderBlacklist();showToast(blacklistTags.length===before?'Global keywords already imported':`Imported ${blacklistTags.length-before} keyword(s)`)};

// ---- Dashboard editor: global feed picker ----
// Checkbox list of global feeds; ticking one appends it to the dashboard's
// own source list (as a normal editable source — unticking later simply
// removes that row, the global feed itself is untouched).
function renderFeedPicker(selectedUrls=[]){
 let picker=document.querySelector('#feed-picker');if(!picker){picker=document.createElement('div');picker.id='feed-picker';picker.style.gridColumn='1/-1';sources.before(picker)}
 if(!globalFeeds.length){picker.hidden=true;picker.innerHTML='';return}
 picker.hidden=false;
 picker.innerHTML='<h3 style="margin:0 0 6px">Global feeds</h3>'+globalFeeds.map(g=>{const on=selectedUrls.includes(g.url);return `<label style="display:flex;gap:8px;align-items:center;grid-auto-flow:column;justify-content:start;font-size:.95rem"><input type="checkbox" data-feed-id="${g.id}" data-feed-name="${g.name.replace(/"/g,'&quot;')}" data-feed-url="${g.url.replace(/"/g,'&quot;')}" data-feed-image="${(g.fallbackImage||'').replace(/"/g,'&quot;')}" ${on?'checked':''}> ${g.name}</label>`}).join('');
 for(const box of picker.querySelectorAll('input[type=checkbox]'))box.onchange=()=>{
  if(box.checked)addSource({name:box.dataset.feedName,url:box.dataset.feedUrl,fallbackImage:box.dataset.feedImage});
  else{const row=[...sources.children].find(b=>b.querySelector('input[name^="sourceUrl"]')?.value===box.dataset.feedUrl);if(row)row.remove();renumber()}
 }
}
const previewOverlay=document.querySelector('#preview-overlay');
async function openPreview(d){document.querySelector('#preview-title').textContent=`Preview — ${d.name}`;previewOverlay.hidden=false;document.querySelector('#preview-frame').srcdoc='<p style="font:16px system-ui;color:#94a3b8;padding:20px">Loading…</p>';try{const r=await fetch(withAuth(base+`/api/preview/${d.id}`));const payload=await r.json();if(!r.ok)throw Error(payload.error||'Preview failed');document.querySelector('#preview-frame').srcdoc=payload.html}catch(e){document.querySelector('#preview-frame').srcdoc=`<p style="font:16px system-ui;color:#fca5a5;padding:20px">${e.message}</p>`}}
document.querySelector('#preview-close').onclick=()=>previewOverlay.hidden=true;
document.addEventListener('keydown',e=>{if(e.key==='Escape'&&!previewOverlay.hidden)previewOverlay.hidden=true});
f.onsubmit=async e=>{e.preventDefault();const id=f.elements['edit-id'].value;const data=Object.fromEntries(new FormData(f));data.blacklist=blacklistTags;if(!id&&f.dataset.variant)data.kind=f.dataset.variant;const path=id?`/api/dashboards/${id}`:'/api/dashboards';await request(path,{method:id?'PUT':'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});showToast(id?'Saved':'Created');closeModal();load()};
cancel.onclick=closeModal;document.querySelector('#add-source').onclick=()=>addSource();document.querySelector('#new-full').onclick=()=>openModal(null,'full');document.querySelector('#new-compact').onclick=()=>openModal(null,'compact');modal.addEventListener('click',e=>{if(e.target===modal)closeModal()});
load().catch(e=>list.textContent=e.message);loadFeeds().catch(e=>feedList.textContent=e.message);loadKeywords().catch(()=>{});</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        print(fmt % args, flush=True)

    def is_ingress(self) -> bool:
        return bool(self.headers.get("X-Hassio-Ingress") or self.headers.get("X-Forwarded-For") or self.headers.get("X-Forwarded-Host"))

    def authorized(self) -> bool:
        """Ingress requests pass (HA authenticated them); everything else needs
        the shared access token via ?auth=… (or the X-Access-Token header)."""
        if self.is_ingress(): return True
        expected = access_token()
        if not expected: return True  # token unavailable (e.g. dev box): fail open
        supplied = parse_qs(urlparse(self.path).query).get("auth", [""])[0] or self.headers.get("X-Access-Token", "")
        return secrets.compare_digest(supplied, expected)

    def send_json(self, value: object, status: int = 200) -> None:
        data = json.dumps(value).encode()
        self.send_response(status); self.send_header("Content-Type", "application/json"); self.send_header("Cache-Control", "no-store"); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

    def send_html(self, value: str, status: int = 200) -> None:
        data = value.encode()
        self.send_response(status); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Cache-Control", "no-store"); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

    def payload(self) -> dict:
        length = int(self.headers.get("Content-Length", "0")); return json.loads(self.rfile.read(length) or b"{}")

    def find(self, identifier: str) -> tuple[list[dict], int]:
        dashboards = load_dashboards()
        for index, dashboard in enumerate(dashboards):
            if dashboard["id"] == identifier: return dashboards, index
        raise KeyError("Display not found.")

    def render_display(self, config: dict, compact: bool, stories: list[dict], errors: list[str]) -> str:
        injected = json.dumps({**config, "stories": stories, "errors": errors}).replace("<", "\\u003c")
        flags = f"<script>window.KIOSK_NEWS_CONFIG={injected};window.KIOSK_NEWS_COMPACT={str(compact).lower()};</script>"
        return DISPLAY_FILE.read_text().replace("</head>", flags + "</head>", 1)

    def display(self, identifier: str, compact: bool) -> None:
        try:
            dashboards, index = self.find(identifier)
            config = dashboards[index]
            feed = aggregate(config.get("sources", []), config)
            self.send_html(self.render_display(config, compact, feed["stories"], feed["errors"]))
        except KeyError as error:
            self.send_html(f"<h1>Kiosk News Display</h1><p>{error}</p>", 404)

    def preview(self, identifier: str) -> None:
        try:
            dashboards, index = self.find(identifier)
            config = dashboards[index]
            feed = aggregate(config.get("sources", []), config)
            self.send_json({"html": self.render_display(config, config.get("kind") == "compact", feed["stories"], feed["errors"])})
        except KeyError as error:
            self.send_json({"error": str(error)}, 404)

    def proxy_image(self) -> None:
        """Fetch an external image on behalf of the kiosk (the display device
        may have no internet access). Only http(s) URLs are allowed."""
        query = parse_qs(urlparse(self.path).query)
        url = query.get("url", [""])[0]
        if not url.lower().startswith(("http://", "https://")):
            return self.send_json({"error": "Invalid image URL."}, 400)
        request = Request(url, headers={"User-Agent": "Mozilla/5.0 (KioskNews/0.1)"})
        try:
            with urlopen(request, timeout=15) as response:
                data = response.read(20_000_000)
                content_type = response.headers.get("Content-Type", "image/jpeg")
        except OSError:
            return self.send_json({"error": "Image fetch failed."}, 502)
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "public, max-age=900")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        path = unquote(urlparse(self.path).path).rstrip("/") or "/"
        if path == "/health": return self.send_json({"ok": True})
        if not self.authorized(): return self.send_json({"error": "Unauthorized — append ?auth=<access token> (see the add-on's admin page)."}, 401)
        if path == "/":
            page = admin_page().replace("__ACCESS_TOKEN__", access_token())
            return self.send_html(page)
        if path == "/api/dashboards": return self.send_json(load_dashboards())
        if path == "/api/feeds": return self.send_json(load_global_feeds())
        if path == "/api/keywords": return self.send_json(load_global_keywords())
        if path == "/api/image": return self.proxy_image()
        match = re.fullmatch(r"/api/preview/([a-z0-9-]+)", path)
        if match: return self.preview(match.group(1))
        match = re.fullmatch(r"/(display|card)/([a-z0-9-]+)", path)
        if match: return self.display(match.group(2), match.group(1) == "card")
        self.send_json({"error": "Not found"}, 404)

    def do_POST(self) -> None:
        if not self.authorized(): return self.send_json({"error": "Unauthorized."}, 401)
        path = urlparse(self.path).path.rstrip("/")
        match = re.fullmatch(r"/api/preview/([a-z0-9-]+)", path)
        if match: return self.preview(match.group(1))
        match = re.fullmatch(r"/api/dashboards/([a-z0-9-]+)/duplicate", path)
        if match: return self.duplicate(match.group(1))
        if path == "/api/feeds":
            try:
                feeds = load_global_feeds(); item = clean_global_feed(self.payload())
                if any(f["id"] == item["id"] for f in feeds): raise ValueError("A feed with this name already exists.")
                feeds.append(item); save_global_feeds(feeds); self.send_json(item, HTTPStatus.CREATED)
            except (ValueError, json.JSONDecodeError) as error: self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            return
        if path != "/api/dashboards": return self.send_json({"error": "Not found"}, 404)
        try:
            dashboards = load_dashboards(); item = clean_dashboard(self.payload())
            if any(d["id"] == item["id"] for d in dashboards): raise ValueError("A display with this name already exists.")
            dashboards.append(item); save_dashboards(dashboards); self.send_json(item, HTTPStatus.CREATED)
        except (ValueError, json.JSONDecodeError) as error: self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)

    def duplicate(self, identifier: str) -> None:
        """Copy a dashboard under a fresh name/ID supplied in the payload."""
        try:
            dashboards, index = self.find(identifier)
            payload = dict(dashboards[index])
            payload.pop("id", None)
            # Flatten sources into the sourceName/sourceUrl/sourceImage keys
            # clean_dashboard expects; a copy carries every setting over.
            for i, source in enumerate(payload.pop("sources", []) or []):
                payload[f"sourceName{i if i else ''}"] = source.get("name", "")
                payload[f"sourceUrl{i if i else ''}"] = source.get("url", "")
                payload[f"sourceImage{i if i else ''}"] = source.get("fallbackImage", "")
            body = self.payload() or {}
            if body.get("name"): payload["name"] = str(body["name"]).strip()
            # Default name: "<Name> (copy)" / "<Name> (copy 2)" …
            if "name" not in body or not str(body.get("name", "")).strip():
                base = dashboards[index]["name"]
                names = {d["name"] for d in dashboards}
                candidate = f"{base} (copy)"
                n = 2
                while candidate in names:
                    candidate = f"{base} (copy {n})"; n += 1
                payload["name"] = candidate
            item = clean_dashboard(payload)
            if any(d["id"] == item["id"] for d in dashboards):
                # Explicit name chosen but its ID collides — ask for another.
                return self.send_json({"error": "A display with this name already exists."}, HTTPStatus.BAD_REQUEST)
            dashboards.insert(index + 1, item); save_dashboards(dashboards)
            self.send_json(item, HTTPStatus.CREATED)
        except KeyError as error:
            self.send_json({"error": str(error)}, HTTPStatus.NOT_FOUND)
        except (ValueError, json.JSONDecodeError) as error:
            self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)

    def do_PUT(self) -> None:
        if not self.authorized(): return self.send_json({"error": "Unauthorized."}, 401)
        path = urlparse(self.path).path.rstrip("/")
        if path == "/api/keywords":
            try:
                keywords = clean_keywords(self.payload().get("keywords", []))
                save_global_keywords(keywords); self.send_json(keywords)
            except (json.JSONDecodeError, AttributeError) as error: self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            return
        match = re.fullmatch(r"/api/feeds/([a-z0-9-]+)", path)
        if match:
            try:
                feeds = load_global_feeds(); index = next((i for i, f in enumerate(feeds) if f["id"] == match.group(1)), None)
                if index is None: raise KeyError("Feed not found.")
                feeds[index] = clean_global_feed(self.payload(), feeds[index]); save_global_feeds(feeds); self.send_json(feeds[index])
            except (KeyError, ValueError, json.JSONDecodeError) as error: self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            return
        match = re.fullmatch(r"/api/dashboards/([a-z0-9-]+)", path)
        if not match: return self.send_json({"error": "Not found"}, 404)
        try:
            dashboards, index = self.find(match.group(1)); dashboards[index] = clean_dashboard(self.payload(), dashboards[index]); save_dashboards(dashboards); self.send_json(dashboards[index])
        except (KeyError, ValueError, json.JSONDecodeError) as error: self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)

    def do_DELETE(self) -> None:
        if not self.authorized(): return self.send_json({"error": "Unauthorized."}, 401)
        path = urlparse(self.path).path.rstrip("/")
        match = re.fullmatch(r"/api/feeds/([a-z0-9-]+)", path)
        if match:
            try:
                feeds = load_global_feeds(); index = next((i for i, f in enumerate(feeds) if f["id"] == match.group(1)), None)
                if index is None: raise KeyError("Feed not found.")
                feeds.pop(index); save_global_feeds(feeds); self.send_json({"ok": True})
            except KeyError as error: self.send_json({"error": str(error)}, HTTPStatus.NOT_FOUND)
            return
        match = re.fullmatch(r"/api/dashboards/([a-z0-9-]+)", path)
        if not match: return self.send_json({"error": "Not found"}, 404)
        try:
            dashboards, index = self.find(match.group(1)); dashboards.pop(index); save_dashboards(dashboards); self.send_json({"ok": True})
        except KeyError as error: self.send_json({"error": str(error)}, HTTPStatus.NOT_FOUND)


if __name__ == "__main__":
    print("Starting Kiosk News Displays on port 8098", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
