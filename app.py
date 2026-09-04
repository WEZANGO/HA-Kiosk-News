"""Small, dependency-free configuration UI and RSS news display server for the add-on.

Mirrors HA-Kiosk-Navigation: stdlib-only HTTP server, per-dashboard configs stored
in /data/dashboards.json, ingress auth plus shared access token for direct kiosk
connections, and a display page with the per-dashboard config injected into it.
"""
from __future__ import annotations

import json
import re
import secrets
import xml.etree.ElementTree as ET
from html import escape
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse
from urllib.request import Request, urlopen

DATA_FILE = Path("/data/dashboards.json")
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
    "background": "dark",
    "vignetteOpacity": "5",
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
    values = {"id": identifier, "name": name, "sources": sources, "kind": kind}
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


def aggregate(sources: list[dict], token: str = "") -> dict:
    """Fetch all sources; interleave their stories. Returns {stories, errors}."""
    stories: list[dict] = []
    errors: list[str] = []
    for source in sources:
        try:
            fetched = fetch_feed(source["url"])
        except Exception as error:  # noqa: BLE001 - report per-source, never fail the display
            errors.append(f"{source['name']}: {error}")
            continue
        for position, story in enumerate(fetched):
            stories.append({
                "title": story["title"],
                "summary": story["summary"],
                "image": proxy_image_url(story["image"] or source.get("fallbackImage", ""), token) if (story["image"] or source.get("fallbackImage")) else "",
                "link": story["link"],
                "date": story["date"],
                "source": source["name"],
            })
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
body{max-width:940px;margin:0 auto;padding:28px;font:16px system-ui,sans-serif;background:#0f172a;color:#f8fafc}h1{margin-bottom:4px}p{color:#cbd5e1}section{margin:24px 0;padding:22px;border:1px solid #334155;border-radius:12px;background:#1e293b}form{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}label{display:grid;gap:5px;color:#cbd5e1;font-size:.9rem}input,select,button{padding:10px;border-radius:7px;font:inherit}input,select{border:1px solid #64748b;background:#0f172a;color:white}button{border:0;background:#38bdf8;color:#082f49;font-weight:700;cursor:pointer}.wide{grid-column:1/-1}.row{display:block;border-top:1px solid #334155;padding:15px 0}.row:first-child{border:0}.row strong{font-size:1.05rem}.dash-top{display:flex;align-items:baseline;gap:12px;margin-bottom:8px}.dash-top small{color:#94a3b8;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.link-line{display:flex;align-items:center;gap:10px;margin:6px 0}.link-label{min-width:88px;color:#94a3b8;font-size:.84rem;flex-shrink:0}.link-url{flex:1;color:#7dd3fc;font-size:.86rem;text-decoration:none;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.link-url:hover{text-decoration:underline}.copy{flex-shrink:0;background:#334155;color:#e2e8f0;padding:6px 12px;font-size:.9rem;cursor:pointer;border-radius:6px;border:0}.copy:hover{background:#475569}.dash-actions{display:flex;gap:10px;margin-top:10px}.dash-actions button{padding:8px 18px}.secondary{background:#334155;color:#fff}.danger{background:#b91c1c;color:#fff}.modal-overlay{position:fixed;inset:0;z-index:100;display:grid;justify-items:center;align-items:start;padding:20px;background:rgba(3,7,18,.85);overflow-y:auto}.modal-overlay[hidden]{display:none}.modal-content{width:min(100%,680px);max-height:calc(100vh-40px);overflow:auto;padding:24px;border-radius:14px;background:#1e293b}.modal-content h2{margin-top:0}.source-block{grid-column:1/-1;border:1px solid #334155;border-radius:10px;padding:12px;background:#0b1222;display:grid;gap:10px}.source-block h4{margin:0;color:#e2e8f0}.source-block .source-fields{display:grid;grid-template-columns:1fr 1fr;gap:10px}.source-block .source-fields .wide-field{grid-column:1/-1}.source-remove{background:#7f1d1d;color:#fff;border:0;border-radius:7px;padding:8px 14px;cursor:pointer;font:inherit;justify-self:start}.toast{position:fixed;bottom:24px;left:50%;transform:translateX(-50%);background:#22c55e;color:#052e16;padding:12px 22px;border-radius:10px;font-weight:700;z-index:300;display:flex;gap:8px;align-items:center}#preview-overlay{position:fixed;inset:0;z-index:200;background:rgba(3,7,18,.88);display:grid;place-items:center;padding:24px}#preview-overlay[hidden]{display:none}#preview-wrap{width:min(96vw,1400px);height:min(92vh,1000px);display:flex;flex-direction:column;background:#0f172a;border:1px solid #334155;border-radius:12px;overflow:hidden;box-shadow:0 24px 60px rgba(0,0,0,.5)}#preview-bar{display:flex;align-items:center;justify-content:space-between;padding:10px 16px;background:#1e293b;border-bottom:1px solid #334155;color:#e2e8f0}#preview-frame{flex:1;border:0;width:100%;background:#0b1222}</style></head><body>
<h1>Kiosk News Displays</h1><p>Create named news displays from RSS feeds; point a kiosk or dashboard iframe at the display link. No API keys needed.</p>
<section><h2>Your news displays</h2><div class="new-buttons"><button id="new-full" class="wide-button">＋ Add Full Screen Display</button><button id="new-compact" class="wide-button">＋ Add Card</button></div></section>
<section><h2>Displays</h2><p>Use <em>Full screen</em> for a wall/tablet display and <em>Compact</em> for a dashboard iframe card. Ingress URLs work within Home Assistant; direct URLs require this app's port to be reachable on your LAN.</p><div id="list">Loading…</div></section>
<div id="editor-modal" class="modal-overlay" hidden><div class="modal-content"><h2 id="modal-title">New display</h2><form id="editor"><input id="edit-id" type="hidden"><label>Name<input name="name" required placeholder="Morning headlines"></label><div id="sources" style="grid-column:1/-1;display:grid;gap:12px"></div><button type="button" id="add-source" class="secondary wide">＋ Add another feed</button><h3 class="wide">Presentation</h3><label>Text shown<select name="textContent"><option value="title">Headline only</option><option value="brief">Brief / summary</option></select></label><label>Image position<select name="imagePosition"><option value="full">Full screen image, text over it</option><option value="top">Image top, text below</option><option value="bottom">Image bottom, text above</option><option value="left">Image left, text right</option><option value="right">Image right, text left</option></select></label><label>Text size<select name="textSize"><option value="small">Small</option><option value="medium">Medium</option><option value="large">Large</option><option value="xlarge">Extra large</option></select></label><label>Theme<select name="background"><option value="dark">Dark</option><option value="light">Light</option></select></label><label>Optional title<input name="title" placeholder="e.g. Headlines"></label><label>Title position<select name="titlePosition"><option value="top">Top</option><option value="bottom">Bottom</option></select></label><label>Title font<select name="titleFont"><option value="system">System sans</option><option value="serif">Serif</option><option value="mono">Monospace</option></select></label><label>Show source name<select name="showSource"><option value="true">Yes</option><option value="false">No</option></select></label><label>Show date<select name="showDate"><option value="true">Yes</option><option value="false">No</option></select></label><label>Progress bar<select name="showProgress"><option value="true">Yes</option><option value="false">No</option></select></label><label>Seconds per story<input name="storySeconds" type="number" min="3" max="120" value="15"></label><label>Refresh feeds every (minutes)<input name="refreshInterval" type="number" min="1" max="1440" value="60"></label><label>Max stories<input name="maxStories" type="number" min="1" max="50" value="10"></label><label>Vignette opacity (1-10)<input name="vignetteOpacity" type="number" min="1" max="10" value="5"></label><div class="modal-buttons"><button type="submit">Save</button><button type="button" id="cancel" class="secondary">Cancel</button></div></form></div></div>
<div id="preview-overlay" hidden><div id="preview-wrap"><div id="preview-bar"><strong id="preview-title">Preview</strong><button type="button" id="preview-close" class="secondary">✕ Close</button></div><iframe id="preview-frame" title="Display preview"></iframe></div></div>
<script>const f=document.querySelector('#editor'),list=document.querySelector('#list'),cancel=document.querySelector('#cancel'),modal=document.querySelector('#editor-modal'),modalTitle=document.querySelector('#modal-title'),sources=document.querySelector('#sources');let items=[];
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
function resetForm(){f.reset();field('edit-id','');sources.innerHTML='';modalTitle.textContent='New display'}
function openModal(dashboard,variant){resetForm();
 if(dashboard){for(const[k,v]of Object.entries(dashboard))field(k,v);field('edit-id',dashboard.id);(dashboard.sources||[]).forEach(s=>addSource(s));modalTitle.textContent=`Edit: ${dashboard.name}`}
 else{addSource();f.dataset.variant=variant||'';modalTitle.textContent=variant==='compact'?'New Card':'New Full Screen Display'}
 modal.hidden=false}
function closeModal(){modal.hidden=true;resetForm()}
function showToast(message){const toast=document.createElement('div');toast.className='toast';toast.innerHTML=`<span>✓</span>${message}`;document.body.append(toast);setTimeout(()=>toast.remove(),1300)}
function render(){list.innerHTML=items.length?'':'<p>No displays yet.</p>';for(const d of items){const full=displayLink(`/display/${d.id}`),compact=displayLink(`/card/${d.id}`);const row=document.createElement('div');row.className='row';row.innerHTML=`<div class="dash-top"><strong>${d.name}</strong><small>${d.kind==='compact'?'Card':'Full screen'} · ${(d.sources||[]).length} feed(s)</small></div>${d.kind==='compact'
?`<div class="link-line"><span class="link-label">Card</span><a class="link-url" href="${compact}" target="_blank" rel="noopener">${compact}</a><button class="copy" type="button" data-copy="${compact}">⧉</button></div>${directLink(`/card/${d.id}`)?`<div class="link-line"><span class="link-label">Direct</span><a class="link-url" href="${directLink(`/card/${d.id}`)}" target="_blank" rel="noopener">${directLink(`/card/${d.id}`)}</a><button class="copy" type="button" data-copy="${directLink(`/card/${d.id}`)}" title="Copy direct link (token-authenticated, for kiosks)">⧉</button></div>`:''}`
:`<div class="link-line"><span class="link-label">Full screen</span><a class="link-url" href="${full}" target="_blank" rel="noopener">${full}</a><button class="copy" type="button" data-copy="${full}">⧉</button></div>${directLink(`/display/${d.id}`)?`<div class="link-line"><span class="link-label">Direct</span><a class="link-url" href="${directLink(`/display/${d.id}`)}" target="_blank" rel="noopener">${directLink(`/display/${d.id}`)}</a><button class="copy" type="button" data-copy="${directLink(`/display/${d.id}`)}" title="Copy direct link (token-authenticated, for kiosks)">⧉</button></div>`:''}`}
<div class="dash-actions"><button class="secondary">Edit</button><button class="secondary" data-preview="1">Preview</button><button class="danger">Delete</button></div>`;
 row.querySelector('.secondary[data-preview="1"]').onclick=()=>openPreview(d);
 row.querySelector('.danger').onclick=rowEditDelete(d,row);
 const edit=row.querySelector('.secondary:not([data-preview])');edit.onclick=()=>openModal(d);
 for(const btn of row.querySelectorAll('.copy'))btn.onclick=async()=>{const url=btn.dataset.copy;try{await navigator.clipboard.writeText(url)}catch(e){const ta=document.createElement('textarea');ta.value=url;document.body.append(ta);ta.select();document.execCommand('copy');ta.remove()}btn.textContent='✓';setTimeout(()=>btn.textContent='⧉',1200)};
 list.append(row)}}
// Two-tap delete (native confirm() is blocked inside HA's sandboxed iframe).
function rowEditDelete(d,row){const btn=()=>{};return async function handler(){const del=row.querySelector('.danger');if(del.dataset.armed){del.disabled=true;try{await request(`/api/dashboards/${d.id}`,{method:'DELETE'});showToast('Deleted')}catch(e){showToast(e.message);del.disabled=false;del.dataset.armed='';del.textContent='Delete';return}load()}else{del.dataset.armed='1';del.textContent='Really delete?';setTimeout(()=>{if(del.isConnected&&del.dataset.armed){del.dataset.armed='';del.textContent='Delete'}},3000)}}}
async function load(){items=await request('/api/dashboards');render()}
const previewOverlay=document.querySelector('#preview-overlay');
async function openPreview(d){document.querySelector('#preview-title').textContent=`Preview — ${d.name}`;previewOverlay.hidden=false;document.querySelector('#preview-frame').srcdoc='<p style="font:16px system-ui;color:#94a3b8;padding:20px">Loading…</p>';try{const r=await fetch(withAuth(base+`/api/preview/${d.id}`));const payload=await r.json();if(!r.ok)throw Error(payload.error||'Preview failed');document.querySelector('#preview-frame').srcdoc=payload.html}catch(e){document.querySelector('#preview-frame').srcdoc=`<p style="font:16px system-ui;color:#fca5a5;padding:20px">${e.message}</p>`}}
document.querySelector('#preview-close').onclick=()=>previewOverlay.hidden=true;
document.addEventListener('keydown',e=>{if(e.key==='Escape'&&!previewOverlay.hidden)previewOverlay.hidden=true});
f.onsubmit=async e=>{e.preventDefault();const id=f.elements['edit-id'].value;const data=Object.fromEntries(new FormData(f));if(!id&&f.dataset.variant)data.kind=f.dataset.variant;const path=id?`/api/dashboards/${id}`:'/api/dashboards';await request(path,{method:id?'PUT':'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});showToast(id?'Saved':'Created');closeModal();load()};
cancel.onclick=closeModal;document.querySelector('#add-source').onclick=()=>addSource();document.querySelector('#new-full').onclick=()=>openModal(null,'full');document.querySelector('#new-compact').onclick=()=>openModal(null,'compact');modal.addEventListener('click',e=>{if(e.target===modal)closeModal()});
load().catch(e=>list.textContent=e.message);</script></body></html>"""


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
            feed = aggregate(config.get("sources", []), access_token())
            self.send_html(self.render_display(config, compact, feed["stories"], feed["errors"]))
        except KeyError as error:
            self.send_html(f"<h1>Kiosk News Display</h1><p>{error}</p>", 404)

    def preview(self, identifier: str) -> None:
        try:
            dashboards, index = self.find(identifier)
            config = dashboards[index]
            feed = aggregate(config.get("sources", []), access_token())
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
        if path != "/api/dashboards": return self.send_json({"error": "Not found"}, 404)
        try:
            dashboards = load_dashboards(); item = clean_dashboard(self.payload())
            if any(d["id"] == item["id"] for d in dashboards): raise ValueError("A display with this name already exists.")
            dashboards.append(item); save_dashboards(dashboards); self.send_json(item, HTTPStatus.CREATED)
        except (ValueError, json.JSONDecodeError) as error: self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)

    def do_PUT(self) -> None:
        if not self.authorized(): return self.send_json({"error": "Unauthorized."}, 401)
        match = re.fullmatch(r"/api/dashboards/([a-z0-9-]+)", urlparse(self.path).path.rstrip("/"))
        if not match: return self.send_json({"error": "Not found"}, 404)
        try:
            dashboards, index = self.find(match.group(1)); dashboards[index] = clean_dashboard(self.payload(), dashboards[index]); save_dashboards(dashboards); self.send_json(dashboards[index])
        except (KeyError, ValueError, json.JSONDecodeError) as error: self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)

    def do_DELETE(self) -> None:
        if not self.authorized(): return self.send_json({"error": "Unauthorized."}, 401)
        match = re.fullmatch(r"/api/dashboards/([a-z0-9-]+)", urlparse(self.path).path.rstrip("/"))
        if not match: return self.send_json({"error": "Not found"}, 404)
        try:
            dashboards, index = self.find(match.group(1)); dashboards.pop(index); save_dashboards(dashboards); self.send_json({"ok": True})
        except KeyError as error: self.send_json({"error": str(error)}, HTTPStatus.NOT_FOUND)


if __name__ == "__main__":
    print("Starting Kiosk News Displays on port 8098", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
