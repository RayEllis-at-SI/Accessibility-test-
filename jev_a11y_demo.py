#!/usr/bin/env python3
"""
Jev Accessibility Demo
======================

A small local web app that shows how a layered accessibility checker works:

  Layer 1  Rules   deterministic checks (missing alt, unlabelled inputs, heading skips...)
  Layer 2  Jev     one-shot judgement calls a rule can't make
                   (is this alt text any good? is this link text vague?
                    is this empty-alt image really decorative?)

Run it:
    python3 jev_a11y_demo.py
Then open http://localhost:8765 in a browser.

No installs needed, just Python 3.8+.
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import webbrowser 
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urljoin

# ---------------------------------------------------------------------------
# PUT YOUR JEV KEY HERE
# ---------------------------------------------------------------------------
API_KEY = "apikey_24010b43cb9f1d74f00978b7e4451ebfd25_5188d3b3cdbf8aabde45d010b2531d9d1fac0b9f545ff09dcc9e8b9720c1dece"
# ---------------------------------------------------------------------------

# Falls back to an environment variable if the slot above is left empty.
if not API_KEY or API_KEY.startswith("PASTE_"):
    API_KEY = os.environ.get("TYPESAFE_API_KEY", "")

API_URL = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-latest"
PORT = 8765
MAX_IMAGES = 30   # caps keep demos quick and credit use sane
MAX_LINKS = 40
WORKERS = 6


# ===========================================================================
# Jev
# ===========================================================================

ALT_QUESTIONS = {
    "alt_quality": {
        "type": "choice",
        "instructions": (
            "Judge the alt text in `image.alt` for the image at `image.src`, "
            "given the page context in `image.context`."
        ),
        "criteria": {
            "descriptive": "Conveys what the image shows or its purpose on the page",
            "junk": "A filename, placeholder or generic word such as 'image' or 'photo'",
            "redundant": "Repeats nearby text or starts with 'image of' without adding anything",
            "too_vague": "Real words, but too vague to tell a screen reader user what matters",
        },
    },
    "likely_decorative": {
        "type": "noul",
        "instructions": (
            "Based on `image.src` and `image.context`, is this image purely decorative, "
            "so it should have empty alt text?"
        ),
    },
}

DECORATIVE_ONLY = {"likely_decorative": ALT_QUESTIONS["likely_decorative"]}

LINK_QUESTIONS = {
    "link_vague": {
        "type": "noul",
        "instructions": (
            "Read out of context, does the link text in `link.text` fail to tell a "
            "screen reader user where the link goes?"
        ),
        "criteria": {
            "true": "Vague or generic, e.g. 'click here', 'read more', 'this'",
            "false": "Clear on its own about the destination or action",
        },
    },
}


def ask_jev(state, questions):
    body = json.dumps({"state": state, "model": MODEL, "questions": questions}).encode()
    req = urllib.request.Request(
        API_URL,
        data=body,
        headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
        method="POST",
    )
    started = time.time()
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.load(resp)
            data["_ms"] = int((time.time() - started) * 1000)
            return data
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}", "detail": e.read().decode(errors="replace")[:300]}
    except Exception as e:  # network, timeout, bad JSON
        return {"error": type(e).__name__, "detail": str(e)[:300]}


# ===========================================================================
# HTML parsing
# ===========================================================================

def clean(text):
    return re.sub(r"\s+", " ", text or "").strip()


class PageParser(HTMLParser):
    VOID = {"img", "input", "br", "hr", "meta", "link", "source", "area", "col", "embed", "wbr"}

    def __init__(self, base_url=""):
        super().__init__(convert_charrefs=True)
        self.base = base_url
        self.lang = None
        self.title = None
        self._in_title = False
        self.images = []
        self.links = []
        self.headings = []
        self.inputs = []
        self.buttons = []
        self.label_for = set()
        self.ids = {}
        self._label_depth = 0
        self._link = None
        self._button = None
        self._heading = None
        self._figure_imgs = []
        self._figcaption = None
        self.last_heading = ""
        self.recent_text = ""

    # -- helpers --
    def _add_text(self, text):
        t = clean(text)
        if not t:
            return
        self.recent_text = (self.recent_text + " " + t)[-220:]
        if self._link is not None:
            self._link["text"] += " " + t
        if self._button is not None:
            self._button["text"] += " " + t
        if self._heading is not None:
            self._heading["text"] += " " + t
        if self._figcaption is not None:
            self._figcaption += " " + t

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        if "id" in a and a["id"]:
            self.ids[a["id"]] = self.ids.get(a["id"], 0) + 1

        if tag == "html":
            self.lang = a.get("lang")
        elif tag == "title":
            self._in_title = True
            self.title = ""
        elif tag == "label":
            self._label_depth += 1
            if a.get("for"):
                self.label_for.add(a["for"])
        elif tag == "a" and "href" in a:
            self._link = {
                "href": urljoin(self.base, a["href"]),
                "text": "",
                "aria": a.get("aria-label", ""),
                "img_alts": [],
            }
        elif tag == "button":
            self._button = {"text": "", "aria": a.get("aria-label", "") or a.get("title", "")}
        elif re.fullmatch(r"h[1-6]", tag):
            self._heading = {"level": int(tag[1]), "text": ""}
        elif tag == "figure":
            self._figure_imgs = []
        elif tag == "figcaption":
            self._figcaption = ""
        elif tag == "img":
            img = {
                "src": urljoin(self.base, a.get("src", "")),
                "alt": a.get("alt"),  # None = attribute missing
                "role": a.get("role", ""),
                "aria_hidden": a.get("aria-hidden", "") == "true",
                "in_link": self._link["href"] if self._link else "",
                "heading": self.last_heading,
                "before": self.recent_text[-160:],
                "caption": "",
            }
            self.images.append(img)
            self._figure_imgs.append(img)
            if self._link is not None and img["alt"]:
                self._link["img_alts"].append(img["alt"])
        elif tag in ("input", "select", "textarea"):
            itype = a.get("type", "text").lower() if tag == "input" else tag
            if itype in ("hidden", "submit", "button", "reset", "image"):
                return
            self.inputs.append({
                "tag": tag,
                "type": itype,
                "id": a.get("id", ""),
                "name": a.get("name", ""),
                "named": bool(a.get("aria-label") or a.get("aria-labelledby") or a.get("title")),
                "wrapped": self._label_depth > 0,
                "placeholder": a.get("placeholder", ""),
            })

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag == "label":
            self._label_depth = max(0, self._label_depth - 1)
        elif tag == "a" and self._link is not None:
            self._link["text"] = clean(self._link["text"])
            self.links.append(self._link)
            self._link = None
        elif tag == "button" and self._button is not None:
            self._button["text"] = clean(self._button["text"])
            self.buttons.append(self._button)
            self._button = None
        elif re.fullmatch(r"h[1-6]", tag) and self._heading is not None:
            self._heading["text"] = clean(self._heading["text"])
            self.headings.append(self._heading)
            self.last_heading = self._heading["text"]
            self._heading = None
        elif tag == "figcaption" and self._figcaption is not None:
            cap = clean(self._figcaption)
            for img in self._figure_imgs:
                img["caption"] = cap
            self._figcaption = None

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        self._add_text(data)


# ===========================================================================
# Layer 1: rules
# ===========================================================================

def run_rules(p):
    findings = []

    def add(rule, wcag, status, detail, target=""):
        findings.append({"rule": rule, "wcag": wcag, "status": status, "detail": detail, "target": target})

    if not p.lang:
        add("Page language set", "3.1.1", "fail", "The <html> element has no lang attribute.")
    else:
        add("Page language set", "3.1.1", "pass", f'lang="{p.lang}"')

    if not clean(p.title or ""):
        add("Page has a title", "2.4.2", "fail", "No <title>, or it's empty.")
    else:
        add("Page has a title", "2.4.2", "pass", clean(p.title))

    for img in p.images:
        if img["alt"] is None and not img["aria_hidden"] and img["role"] not in ("presentation", "none"):
            add("Image has alt attribute", "1.1.1", "fail", "Missing alt attribute entirely.", img["src"])

    for link in p.links:
        name = clean(link["aria"]) or link["text"] or " ".join(link["img_alts"])
        if not name:
            add("Link has an accessible name", "2.4.4 / 4.1.2", "fail", "Link has no text, label or image alt.", link["href"])

    for b in p.buttons:
        if not (b["text"] or clean(b["aria"])):
            add("Button has an accessible name", "4.1.2", "fail", "Button has no text or aria-label.")

    for i in p.inputs:
        ok = i["named"] or i["wrapped"] or (i["id"] and i["id"] in p.label_for)
        if not ok:
            hint = f' (placeholder "{i["placeholder"]}" is not a label)' if i["placeholder"] else ""
            add("Form field has a label", "1.3.1 / 4.1.2", "fail",
                f'{i["tag"]} type={i["type"]} name="{i["name"]}" has no label{hint}.')

    h1s = [h for h in p.headings if h["level"] == 1]
    if not h1s:
        add("Page has an h1", "1.3.1", "warn", "No h1 found.")
    elif len(h1s) > 1:
        add("Page has a single h1", "1.3.1", "warn", f"{len(h1s)} h1 elements found.")
    prev = 0
    for h in p.headings:
        if prev and h["level"] > prev + 1:
            add("Heading levels don't skip", "1.3.1", "warn",
                f'h{prev} jumps to h{h["level"]}: "{h["text"][:60]}"')
        prev = h["level"]

    for id_, n in p.ids.items():
        if n > 1:
            add("IDs are unique", "4.1.1", "warn", f'id="{id_}" used {n} times.')

    return findings


# ===========================================================================
# Layer 2: Jev
# ===========================================================================

def image_context(img):
    parts = []
    if img["caption"]:
        parts.append(f'Caption: {img["caption"]}')
    if img["heading"]:
        parts.append(f'Section: {img["heading"]}')
    if img["in_link"]:
        parts.append(f'Image is inside a link to {img["in_link"]}')
    if img["before"]:
        parts.append(f'Nearby text: {img["before"]}')
    return " | ".join(parts) or "No surrounding text"


def judge_image(img):
    ctx = image_context(img)
    if img["alt"] is None:
        return None  # rules already failed it
    state = {"image": {"src": img["src"], "alt": img["alt"], "context": ctx}}
    if img["alt"].strip() == "":
        r = ask_jev(state, DECORATIVE_ONLY)
        out = {"kind": "image", "src": img["src"], "alt": "", "context": ctx, "raw": r}
        if "error" in r:
            out.update(status="error", verdict=r["error"])
            return out
        p_dec = r["answers"]["likely_decorative"]["noul"]
        out["p_decorative"] = p_dec
        if p_dec < 0.4:
            out.update(status="fail", verdict="Empty alt, but Jev thinks this image carries meaning")
        elif p_dec < 0.65:
            out.update(status="warn", verdict="Empty alt. Jev isn't sure it's decorative")
        else:
            out.update(status="pass", verdict="Empty alt, and Jev agrees it looks decorative")
        return out

    r = ask_jev(state, ALT_QUESTIONS)
    out = {"kind": "image", "src": img["src"], "alt": img["alt"], "context": ctx, "raw": r}
    if "error" in r:
        out.update(status="error", verdict=r["error"])
        return out
    q = r["answers"]["alt_quality"]
    p_dec = r["answers"]["likely_decorative"]["noul"]
    out.update(choice=q["choice"], confidence=q["confidence"], p_decorative=p_dec)
    if q["choice"] == "descriptive":
        out.update(status="pass", verdict="Alt text describes the image")
    elif q["choice"] == "junk":
        out.update(status="fail", verdict="Alt text is junk (filename, placeholder or generic)")
    elif q["choice"] == "redundant":
        out.update(status="warn", verdict="Alt text is redundant")
    else:
        out.update(status="warn", verdict="Alt text is too vague to be useful")
    if p_dec >= 0.7 and q["choice"] != "descriptive":
        out["verdict"] += ". Probably decorative, so alt=\"\" may be better"
    return out


def judge_link(link):
    text = clean(link["aria"]) or link["text"] or " ".join(link["img_alts"])
    if not text:
        return None  # rules already failed it
    r = ask_jev({"link": {"text": text, "href": link["href"]}}, LINK_QUESTIONS)
    out = {"kind": "link", "text": text, "href": link["href"], "raw": r}
    if "error" in r:
        out.update(status="error", verdict=r["error"])
        return out
    p = r["answers"]["link_vague"]["noul"]
    out["p_vague"] = p
    if p >= 0.7:
        out.update(status="fail", verdict="Vague out of context")
    elif p >= 0.4:
        out.update(status="warn", verdict="Borderline, could be clearer")
    else:
        out.update(status="pass", verdict="Clear on its own")
    return out


def run_jev(p):
    images = [i for i in p.images if i["alt"] is not None][:MAX_IMAGES]
    seen, links = set(), []
    for l in p.links:
        key = (l["text"], l["aria"], l["href"])
        if key not in seen:
            seen.add(key)
            links.append(l)
    links = links[:MAX_LINKS]

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        img_results = list(pool.map(judge_image, images))
        link_results = list(pool.map(judge_link, links))
    return [r for r in img_results if r], [r for r in link_results if r]


# ===========================================================================
# Scan
# ===========================================================================

def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Jev a11y demo)"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.read(3_000_000).decode(charset, errors="replace"), resp.geturl()


def scan(url=None, html=None):
    started = time.time()
    if url:
        if not re.match(r"https?://", url):
            url = "https://" + url
        html, url = fetch(url)
    parser = PageParser(url or "")
    parser.feed(html)
    rules = run_rules(parser)
    t_rules = int((time.time() - started) * 1000)
    images, links = run_jev(parser)
    calls = len(images) + len(links)
    return {
        "url": url or "(pasted HTML)",
        "title": clean(parser.title or ""),
        "counts": {
            "images": len(parser.images), "links": len(parser.links),
            "headings": len(parser.headings), "inputs": len(parser.inputs),
        },
        "rules": rules,
        "jev_images": images,
        "jev_links": links,
        "jev_calls": calls,
        "rules_ms": t_rules,
        "total_ms": int((time.time() - started) * 1000),
        "capped": len(parser.images) > MAX_IMAGES or len(parser.links) > MAX_LINKS,
    }


# ===========================================================================
# Sample page with planted problems
# ===========================================================================

SAMPLE_HTML = """<!doctype html>
<html>
<head><title>Northwind Outdoor Co. | Spring range</title></head>
<body>
<header>
  <a href="/"><img src="/img/logo.png" alt="image of logo"></a>
  <nav><a href="/shop">Shop</a> <a href="/about">About us</a> <a href="/basket"><img src="/img/basket.svg"></a></nav>
</header>
<h1>Spring range is here</h1>
<img src="/img/divider-wave.svg" alt="">
<p>Our new waterproof jackets are tested on Dartmoor in February, which is the closest thing to a crime.</p>
<figure>
  <img src="/img/IMG_4032.jpg" alt="IMG_4032.jpg">
  <figcaption>The design team on a field test at Haytor, soaked to the bone</figcaption>
</figure>
<h3>Sales so far</h3>
<p>Q3 revenue grew 12% over Q2, driven by jackets and boots.</p>
<img src="/img/chart-q3.png" alt="chart">
<img src="/img/hero-boots.jpg" alt="">
<p>The Trailblazer boot, now in three colours. <a href="/boots/trailblazer">Read more</a></p>
<h2>Meet the team</h2>
<img src="/img/ceo.jpg" alt="Priya Shah, CEO, laughing on a muddy hillside in a red jacket">
<p>Priya founded Northwind in 2014. <a href="/team">click here</a> to meet everyone.</p>
<h2>Stay in touch</h2>
<form>
  <input type="email" name="email" placeholder="Email address">
  <label for="pref">How often?</label>
  <select id="pref" name="pref"><option>Weekly</option><option>Monthly</option></select>
  <button></button>
</form>
<footer>
  <a href="/report.pdf">Download the 2026 accessibility statement (PDF, 2MB)</a>
  <a href="/support">Contact support</a>
  <a href="/terms">here</a>
  <span id="footer-note">Made in Cornwall</span><span id="footer-note">2026</span>
</footer>
</body>
</html>"""


# ===========================================================================
# Web UI
# ===========================================================================

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Jev Accessibility Demo</title>
<style>
:root{--bg:#f6f5f2;--card:#fff;--ink:#1d1d1f;--mute:#6b6b70;--line:#e3e1dc;
--pass:#1f7a4d;--passbg:#e6f4ec;--warn:#9a6200;--warnbg:#fdf1dc;--fail:#b3261e;--failbg:#fbe7e5;
--rule:#3b5bdb;--jev:#8a3ffc;--err:#555;--errbg:#eee}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
main{max-width:1080px;margin:0 auto;padding:28px 16px 60px}
h1{font-size:26px;margin:0 0 4px}
.sub{color:var(--mute);margin:0 0 22px}
.layers{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:20px}
.layer{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;border-top:4px solid}
.layer.r{border-top-color:var(--rule)}.layer.j{border-top-color:var(--jev)}
.layer b{display:block;margin-bottom:2px}.layer p{margin:0;color:var(--mute);font-size:14px}
.box{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin-bottom:20px}
.row{display:flex;gap:8px;flex-wrap:wrap}
input[type=text]{flex:1;min-width:220px;padding:10px 12px;border:1px solid var(--line);border-radius:8px;font:inherit}
textarea{width:100%;min-height:110px;margin-top:10px;padding:10px;border:1px solid var(--line);border-radius:8px;font:13px/1.4 ui-monospace,Menlo,monospace;display:none}
button{padding:10px 16px;border-radius:8px;border:1px solid var(--ink);background:var(--ink);color:#fff;font:inherit;cursor:pointer}
button.ghost{background:transparent;color:var(--ink);border-color:var(--line)}
button:disabled{opacity:.5;cursor:wait}
.status{margin-top:10px;color:var(--mute);font-size:14px;min-height:20px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin-bottom:20px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px}
.stat .n{font-size:24px;font-weight:700}.stat .l{color:var(--mute);font-size:13px}
h2{font-size:18px;margin:26px 0 10px;display:flex;align-items:center;gap:8px}
.tag{font-size:11px;font-weight:700;letter-spacing:.04em;text-transform:uppercase;color:#fff;padding:2px 7px;border-radius:4px}
.tag.r{background:var(--rule)}.tag.j{background:var(--jev)}
.item{background:var(--card);border:1px solid var(--line);border-left:5px solid var(--line);border-radius:8px;padding:10px 14px;margin-bottom:8px}
.item.pass{border-left-color:var(--pass)}.item.warn{border-left-color:var(--warn)}.item.fail{border-left-color:var(--fail)}.item.error{border-left-color:var(--err)}
.head{display:flex;justify-content:space-between;gap:10px;align-items:baseline;flex-wrap:wrap}
.pill{font-size:12px;font-weight:700;padding:2px 8px;border-radius:99px;white-space:nowrap}
.pill.pass{color:var(--pass);background:var(--passbg)}.pill.warn{color:var(--warn);background:var(--warnbg)}
.pill.fail{color:var(--fail);background:var(--failbg)}.pill.error{color:var(--err);background:var(--errbg)}
.meta{color:var(--mute);font-size:13px;word-break:break-all}
code{font:13px ui-monospace,Menlo,monospace;background:#f0eee9;padding:1px 5px;border-radius:4px}
.bar{height:6px;background:#eee;border-radius:3px;overflow:hidden;margin-top:6px;max-width:260px}
.bar i{display:block;height:100%;background:var(--jev)}
details{margin-top:6px}summary{cursor:pointer;color:var(--mute);font-size:13px}
pre{background:#f0eee9;padding:10px;border-radius:6px;overflow:auto;font-size:12px;max-height:240px}
.filters{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px}
.filters button{padding:5px 11px;font-size:13px}
.filters button.on{background:var(--ink);color:#fff;border-color:var(--ink)}
.empty{color:var(--mute);font-style:italic}
#results{display:none}
@media (max-width:640px){.layers{grid-template-columns:1fr}}
</style>
</head>
<body>
<main>
  <h1>Jev Accessibility Demo</h1>
  <p class="sub">Rules catch what's missing. Jev judges whether what's there is any good.</p>

  <div class="layers">
    <div class="layer r"><b>Layer 1 · Rules</b><p>Deterministic, instant, free. Missing alt attributes, unlabelled fields, heading skips, missing page language. Either it's there or it isn't.</p></div>
    <div class="layer j"><b>Layer 2 · Jev</b><p>One-shot judgement calls a rule can't make. Is this alt text useful? Is "Read more" vague? Is this empty-alt image really decorative? Returns a label and a probability, not an essay.</p></div>
  </div>

  <div class="box">
    <div class="row">
      <input id="url" type="text" placeholder="https://example.com/page" aria-label="Page URL">
      <button id="go">Scan URL</button>
      <button id="sample" class="ghost">Try the sample page</button>
      <button id="toggle" class="ghost">Paste HTML instead</button>
    </div>
    <textarea id="html" aria-label="HTML to scan" placeholder="Paste page HTML here, then press Scan HTML"></textarea>
    <div class="status" id="status" role="status"></div>
  </div>

  <section id="results">
    <div class="row" style="justify-content:space-between;align-items:center;margin-bottom:12px">
      <div><b id="r-title"></b><div class="meta" id="r-url"></div></div>
      <button id="export" class="ghost">Export report (HTML)</button>
    </div>
    <div class="stats" id="stats"></div>
    <div class="filters" id="filters">
      <button data-f="all" class="ghost on">All</button>
      <button data-f="fail" class="ghost">Fails</button>
      <button data-f="warn" class="ghost">Warnings</button>
      <button data-f="pass" class="ghost">Passes</button>
    </div>
    <h2><span class="tag r">Rules</span> Structural checks</h2>
    <div id="rules"></div>
    <h2><span class="tag j">Jev</span> Image alt text</h2>
    <div id="imgs"></div>
    <h2><span class="tag j">Jev</span> Link text</h2>
    <div id="links"></div>
  </section>
</main>

<script>
const $ = s => document.querySelector(s);
let last = null, filter = "all";
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const pct = p => Math.round(p * 100) + "%";

$("#toggle").onclick = () => {
  const t = $("#html"), show = t.style.display !== "block";
  t.style.display = show ? "block" : "none";
  $("#toggle").textContent = show ? "Use a URL instead" : "Paste HTML instead";
  $("#go").textContent = show ? "Scan HTML" : "Scan URL";
};

async function run(payload) {
  for (const b of document.querySelectorAll("button")) b.disabled = true;
  $("#status").textContent = "Running rules, then asking Jev about each image and link...";
  try {
    const r = await fetch("/api/scan", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload)});
    const d = await r.json();
    if (d.error) throw new Error(d.error);
    last = d; render(d);
    $("#status").textContent = `Done in ${(d.total_ms/1000).toFixed(1)}s. Rules took ${d.rules_ms}ms, Jev handled ${d.jev_calls} judgements.` + (d.capped ? " (Large page, so Jev checks were capped.)" : "");
  } catch (e) {
    $("#status").textContent = "Error: " + e.message;
  } finally {
    for (const b of document.querySelectorAll("button")) b.disabled = false;
  }
}

$("#go").onclick = () => {
  const htmlMode = $("#html").style.display === "block";
  if (htmlMode) { if ($("#html").value.trim()) run({html: $("#html").value}); }
  else if ($("#url").value.trim()) run({url: $("#url").value.trim()});
};
$("#url").addEventListener("keydown", e => { if (e.key === "Enter") $("#go").click(); });
$("#sample").onclick = () => run({sample: true});

$("#filters").onclick = e => {
  const f = e.target.dataset.f; if (!f) return;
  filter = f;
  for (const b of $("#filters").children) b.classList.toggle("on", b.dataset.f === f);
  if (last) render(last);
};

function item(status, label, title, body, raw) {
  if (filter !== "all" && status !== filter) return "";
  return `<div class="item ${status}"><div class="head"><b>${title}</b><span class="pill ${status}">${label}</span></div>${body}` +
    (raw ? `<details><summary>What Jev sent back</summary><pre>${esc(JSON.stringify(raw, null, 2))}</pre></details>` : "") + "</div>";
}
const LBL = {pass: "Pass", warn: "Warning", fail: "Fail", error: "Error"};

function render(d) {
  $("#results").style.display = "block";
  $("#r-title").textContent = d.title || "(no title)";
  $("#r-url").textContent = d.url;
  const all = [...d.rules, ...d.jev_images, ...d.jev_links];
  const n = s => all.filter(x => x.status === s).length;
  const jevCatches = [...d.jev_images, ...d.jev_links].filter(x => x.status === "fail" || x.status === "warn").length;
  $("#stats").innerHTML = [
    [n("fail"), "Fails"], [n("warn"), "Warnings"], [n("pass"), "Passes"],
    [jevCatches, "Issues only Jev could flag"], [d.jev_calls, "Jev calls"]
  ].map(([v, l]) => `<div class="stat"><div class="n">${v}</div><div class="l">${l}</div></div>`).join("");

  $("#rules").innerHTML = d.rules.map(r => item(r.status, LBL[r.status],
    `${esc(r.rule)} <span class="meta">WCAG ${esc(r.wcag)}</span>`,
    `<div>${esc(r.detail)}</div>` + (r.target ? `<div class="meta">${esc(r.target)}</div>` : ""))).join("") || `<p class="empty">Nothing to show.</p>`;

  $("#imgs").innerHTML = d.jev_images.map(i => {
    let body = `<div>alt=<code>${esc(i.alt === "" ? '""' : i.alt)}</code></div><div class="meta">${esc(i.src)}</div><div class="meta">Context given to Jev: ${esc(i.context)}</div>`;
    if (i.choice) body += `<div style="margin-top:4px">Jev label: <code>${esc(i.choice)}</code> at ${pct(i.confidence)} confidence</div><div class="bar"><i style="width:${pct(i.confidence)}"></i></div>`;
    if (i.p_decorative != null) body += `<div class="meta" style="margin-top:4px">Chance it's decorative: ${pct(i.p_decorative)}</div>`;
    return item(i.status, LBL[i.status], esc(i.verdict), body, i.raw);
  }).join("") || `<p class="empty">No images with alt attributes to judge.</p>`;

  $("#links").innerHTML = d.jev_links.map(l => {
    let body = `<div class="meta">${esc(l.href)}</div>`;
    if (l.p_vague != null) body += `<div style="margin-top:4px">Chance it's vague: ${pct(l.p_vague)}</div><div class="bar"><i style="width:${pct(l.p_vague)}"></i></div>`;
    return item(l.status, LBL[l.status], `"${esc(l.text)}" · ${esc(l.verdict)}`, body, l.raw);
  }).join("") || `<p class="empty">No links to judge.</p>`;
}

$("#export").onclick = () => {
  if (!last) return;
  const clone = document.documentElement.cloneNode(true);
  clone.querySelector(".box")?.remove();
  clone.querySelector("#export")?.remove();
  clone.querySelector("#filters")?.remove();
  clone.querySelectorAll("script").forEach(s => s.remove());
  const stamp = document.createElement("p");
  stamp.className = "sub";
  stamp.textContent = `Exported ${new Date().toLocaleString("en-GB")}. Status: ${$("#status").textContent}`;
  clone.querySelector("#results").before(stamp);
  const blob = new Blob(["<!doctype html>\n" + clone.outerHTML], {type: "text/html"});
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "jev-a11y-report.html";
  a.click();
};
</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, PAGE, "text/html; charset=utf-8")
        else:
            self._send(404, "Not found", "text/plain")

    def do_POST(self):
        if self.path != "/api/scan":
            return self._send(404, "Not found", "text/plain")
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length) or b"{}")
            if payload.get("sample"):
                result = scan(html=SAMPLE_HTML)
                result["url"] = "Built-in sample page (problems planted on purpose)"
            elif payload.get("url"):
                result = scan(url=payload["url"])
            elif payload.get("html"):
                result = scan(html=payload["html"])
            else:
                raise ValueError("Give a URL or some HTML.")
            self._send(200, json.dumps(result), "application/json")
        except urllib.error.URLError as e:
            self._send(200, json.dumps({"error": f"Couldn't fetch that page: {e.reason}"}), "application/json")
        except Exception as e:
            self._send(200, json.dumps({"error": str(e)}), "application/json")

    def log_message(self, fmt, *args):
        pass  # keep the terminal quiet


def main():
    if not API_KEY:
        sys.exit("No Jev key. Paste it into API_KEY at the top of this file.")
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://localhost:{PORT}"
    print(f"Jev accessibility demo running at {url}  (Ctrl+C to stop)")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
