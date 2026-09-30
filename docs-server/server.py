"""A tiny web server for reading the labs in a browser.

* Markdown is rendered on every request, so edits show up on refresh.
* Relative links work as written in the .md files: `01-counters.md` renders that lab, `../collector/config.yaml`
  shows the config file, a directory link lists the directory.
* PromQL code blocks get "Grafana" / "Prometheus" buttons per query; shell blocks get a copy button.

The repo is mounted read-only at SITE_ROOT. Only an allowlist of file types is served, and no dotfile except
`.env.example`. In particular `.env` (which holds your Honeycomb key) is never served.
"""

from __future__ import annotations

import html
import json
import os
import re
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

import markdown

ROOT = Path(os.environ.get("SITE_ROOT", "/site")).resolve()
PORT = int(os.environ.get("PORT", "8000"))
# Empty = "same host as this page, standard port", resolved in the browser. Set these only if the tools live
# somewhere else (e.g. behind a reverse proxy with their own hostnames).
LINKS = {
    "control": os.environ.get("CONTROL_URL", ""),
    "grafana": os.environ.get("GRAFANA_URL", ""),
    "prometheus": os.environ.get("PROMETHEUS_URL", ""),
}
TEXT_TYPES = {".yml", ".yaml", ".py", ".json", ".toml", ".txt", ".sh", ".example", ".conf"}
ALLOWED_DOTFILES = {".env.example"}
SKIP_DIRS = {"__pycache__", "node_modules", ".git", ".venv"}
MD_EXTENSIONS = ["fenced_code", "tables", "toc", "sane_lists", "attr_list"]


def safe_path(url_path: str) -> Path | None:
    rel = unquote(url_path).lstrip("/")
    parts = [p for p in rel.split("/") if p]
    for p in parts:
        if p.startswith(".") and p not in ALLOWED_DOTFILES:
            return None
        if p in SKIP_DIRS or p.endswith(".egg-info"):
            return None
    target = (ROOT / "/".join(parts)).resolve()
    if target != ROOT and ROOT not in target.parents:
        return None
    return target


def lab_nav():
    """Sidebar: every labs/*.md, titled by its first heading."""
    items = []
    labs = ROOT / "labs"
    for f in sorted(labs.glob("*.md")):
        title = f.stem
        try:
            for line in f.read_text().splitlines():
                if line.startswith("# "):
                    title = line[2:].strip()
                    break
        except OSError:
            pass
        items.append((f"/labs/{f.name}", title))
    readme = [i for i in items if i[0].endswith("/README.md")]
    score = [i for i in items if i[0].endswith("/scorecard.md")]
    rest = [i for i in items if i not in readme and i not in score]
    return readme, rest, score


def page(title: str, body: str, current: str) -> bytes:
    readme, labs, score = lab_nav()

    def link(href, text):
        cls = ' class="on"' if href == current else ""
        text = re.sub(r"^Lab (\d+):\s*", r"<span class='num'>\1</span>", html.escape(text))
        return f'<a href="{href}"{cls}>{text}</a>'

    nav = "".join(link(h, "Workbook overview") for h, _ in readme)
    nav += "".join(link(h, t) for h, t in labs)
    nav += "".join(link(h, "Scorecard") for h, _ in score)
    nav += '<div class="sep"></div>' + link("/README.md", "Playground README")
    if (ROOT / "VM.md").exists():
        nav += link("/VM.md", "Running on a VM")

    # prev / next across the lab sequence
    seq = [h for h, _ in readme + labs + score]
    pn = ""
    if current in seq:
        i = seq.index(current)
        prev_ = f'<a href="{seq[i-1]}">&larr; previous</a>' if i > 0 else "<span></span>"
        next_ = f'<a href="{seq[i+1]}">next &rarr;</a>' if i + 1 < len(seq) else "<span></span>"
        pn = f'<nav class="pn">{prev_}{next_}</nav>'

    values = {
        "title": html.escape(title), "nav": nav, "body": body, "pn": pn, "links": json.dumps(LINKS),
    }
    out = TEMPLATE
    for k, v in values.items():
        out = out.replace(f"@@{k}@@", v)
    return out.encode()


LIST_ITEM = re.compile(r"^\s{0,3}([*+-]|\d+\.)\s")


def github_lists(text: str) -> str:
    """GitHub starts a list right after a paragraph line; python-markdown needs a blank line first."""
    out, fence, prev = [], False, ""
    for line in text.split("\n"):
        if line.lstrip().startswith("```"):
            fence = not fence
        elif not fence and LIST_ITEM.match(line) and prev.strip() and not LIST_ITEM.match(prev) \
                and not prev.startswith((" ", "\t", "|", ">")):
            out.append("")
        out.append(line)
        prev = line
    return "\n".join(out)


def render_markdown(path: Path) -> tuple[str, str]:
    text = github_lists(path.read_text())
    m = re.search(r"^# (.+)$", text, re.M)
    title = m.group(1) if m else path.name
    return title, markdown.markdown(text, extensions=MD_EXTENSIONS)


def render_text(path: Path, url: str) -> tuple[str, str]:
    body = (f'<p class="filehead"><code>{html.escape(url)}</code> '
            f'<a href="{html.escape(url)}?raw=1">raw</a></p>'
            f'<pre class="file"><code>{html.escape(path.read_text())}</code></pre>')
    return path.name, body


def render_dir(path: Path, url: str) -> tuple[str, str]:
    base = url if url.endswith("/") else url + "/"
    rows = []
    for child in sorted(path.iterdir(), key=lambda c: (not c.is_dir(), c.name)):
        if safe_path(base + child.name) is None:
            continue
        if child.is_file() and child.suffix not in TEXT_TYPES | {".md"} and child.name not in ALLOWED_DOTFILES:
            continue
        name = child.name + ("/" if child.is_dir() else "")
        rows.append(f'<li><a href="{html.escape(base + name)}">{html.escape(name)}</a></li>')
    return url, f"<h1><code>{html.escape(url)}</code></h1><ul class='dir'>{''.join(rows)}</ul>"


class Handler(BaseHTTPRequestHandler):
    server_version = "labs/1.0"

    def log_message(self, fmt, *args):  # quiet
        pass

    def send(self, status, body: bytes, ctype="text/html; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parts = urlsplit(self.path)
        url = parts.path
        if url in ("", "/"):
            self.send_response(HTTPStatus.FOUND)
            self.send_header("Location", "/labs/README.md")
            self.end_headers()
            return
        target = safe_path(url)
        if target is None or not target.exists():
            self.send(404, page("Not found", f"<h1>Not found</h1><p><code>{html.escape(url)}</code></p>", url))
            return
        try:
            if target.is_dir():
                title, body = render_dir(target, url)
            elif target.suffix == ".md":
                title, body = render_markdown(target)
            elif target.suffix in TEXT_TYPES or target.name in ALLOWED_DOTFILES:
                if "raw=1" in parts.query:
                    self.send(200, target.read_bytes(), "text/plain; charset=utf-8")
                    return
                title, body = render_text(target, url)
            else:
                self.send(404, page("Not served", "<h1>Not served</h1>", url))
                return
        except (OSError, UnicodeDecodeError) as exc:
            self.send(500, page("Error", f"<h1>Error</h1><pre>{html.escape(str(exc))}</pre>", url))
            return
        self.send(200, page(title, body, url))


TEMPLATE = (Path(__file__).parent / "template.html").read_text()  # @@name@@ placeholders (CSS/JS use braces)

if __name__ == "__main__":
    print(f"serving {ROOT} on :{PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
