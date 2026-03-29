"""
ZionBrowser Core — Portable HTTP engine for Android/Mobile
Em nome do Senhor Jesus Cristo, nosso Salvador.

Extracted from ZionBrowser v2.0 — pure Python stdlib, ZERO deps.
"""

import os
import json
import ssl
import gzip
import zlib
import time
import re
import hashlib
import http.cookiejar
import urllib.request
import urllib.parse
import urllib.error
from html.parser import HTMLParser
from pathlib import Path
from datetime import datetime, timezone

VERSION = "2.0.0"
USER_AGENT = "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"

ZION_DIR = Path.home() / ".zion"
SESSION_DIR = ZION_DIR / "sessions"
CACHE_DIR = ZION_DIR / "cache"

TIMEOUT = 15
MAX_RESPONSE = 2 * 1024 * 1024
MAX_CACHE_SIZE = 5 * 1024 * 1024
CACHE_TTL = 300

for d in [ZION_DIR, SESSION_DIR, CACHE_DIR]:
    d.mkdir(parents=True, exist_ok=True)


class ZionHTMLParser(HTMLParser):
    """Lightweight HTML parser — text, links, forms, meta."""

    def __init__(self):
        super().__init__()
        self.text_parts = []
        self.links = []
        self.forms = []
        self.current_form = None
        self.current_tag = None
        self.skip_tags = {"script", "style", "noscript", "svg", "path"}
        self.in_skip = 0
        self.title = ""
        self.in_title = False
        self.meta = {}
        self.headings = []
        self._link_stack = []
        self.js_redirects = []
        self.in_script = False
        self.script_buf = ""

    def handle_starttag(self, tag, attrs):
        self.current_tag = tag
        a = dict(attrs)
        if tag == "script":
            self.in_script = True
            self.script_buf = ""
            self.in_skip += 1
            return
        if tag in self.skip_tags:
            self.in_skip += 1
            return
        if tag == "title":
            self.in_title = True
        if tag == "meta":
            name = a.get("name", a.get("property", ""))
            content = a.get("content", "")
            if name and content:
                self.meta[name] = content
            equiv = a.get("http-equiv", "").lower()
            if equiv == "refresh" and content:
                m = re.search(r'url\s*=\s*["\']?([^"\';\s]+)', content, re.I)
                if m:
                    self.js_redirects.append(m.group(1))
        if tag == "a":
            href = a.get("href", "")
            if href:
                self._link_stack.append({"href": href, "text": ""})
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self.headings.append({"level": tag, "text": ""})
        if tag == "form":
            self.current_form = {
                "action": a.get("action", ""),
                "method": a.get("method", "GET").upper(),
                "id": a.get("id", ""),
                "name": a.get("name", ""),
                "enctype": a.get("enctype", ""),
                "inputs": [],
            }
        if tag in ("input", "textarea", "select"):
            inp = {
                "tag": tag,
                "type": a.get("type", "text"),
                "name": a.get("name", ""),
                "value": a.get("value", ""),
                "id": a.get("id", ""),
                "placeholder": a.get("placeholder", ""),
                "required": "required" in a or a.get("required") == "true",
            }
            if self.current_form is not None:
                self.current_form["inputs"].append(inp)

    def handle_endtag(self, tag):
        if tag == "script":
            self.in_script = False
            if self.script_buf:
                for pattern in [
                    r'window\.location\s*=\s*["\']([^"\']+)',
                    r'location\.href\s*=\s*["\']([^"\']+)',
                    r'location\.replace\(["\']([^"\']+)',
                ]:
                    m = re.search(pattern, self.script_buf)
                    if m:
                        self.js_redirects.append(m.group(1))
            self.in_skip = max(0, self.in_skip - 1)
            return
        if tag in self.skip_tags:
            self.in_skip = max(0, self.in_skip - 1)
            return
        if tag == "title":
            self.in_title = False
        if tag == "a" and self._link_stack:
            link = self._link_stack.pop()
            self.links.append(link)
        if tag == "form" and self.current_form is not None:
            self.forms.append(self.current_form)
            self.current_form = None

    def handle_data(self, data):
        if self.in_script:
            self.script_buf += data
            return
        if self.in_skip > 0:
            return
        text = data.strip()
        if not text:
            return
        if self.in_title:
            self.title += text
        if self.headings and self.current_tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self.headings[-1]["text"] += text
        if self._link_stack:
            self._link_stack[-1]["text"] += text
        self.text_parts.append(text)

    def get_text(self, max_lines=200):
        full = "\n".join(self.text_parts)
        lines = [l.strip() for l in full.split("\n") if l.strip()]
        deduped = []
        prev = ""
        for line in lines[:max_lines]:
            if line != prev:
                deduped.append(line)
                prev = line
        return "\n".join(deduped)

    def get_links(self, base_url=""):
        resolved = []
        seen = set()
        for link in self.links:
            href = link["href"]
            if href.startswith(("javascript:", "#", "mailto:", "tel:")):
                continue
            if not href.startswith("http"):
                href = urllib.parse.urljoin(base_url, href)
            if href not in seen:
                seen.add(href)
                resolved.append({"url": href, "text": link["text"].strip()})
        return resolved

    def get_forms(self):
        return self.forms


class ResponseCache:
    def __init__(self):
        pass

    def _key(self, url):
        return hashlib.md5(url.encode()).hexdigest()

    def get(self, url):
        key = self._key(url)
        cache_file = CACHE_DIR / f"{key}.json"
        if cache_file.exists():
            try:
                data = json.loads(cache_file.read_text())
                if time.time() - data.get("ts", 0) < CACHE_TTL:
                    return data.get("status"), data.get("headers", {}), data.get("body", ""), data.get("url", url)
            except Exception:
                pass
        return None

    def put(self, url, status, headers, body):
        if status == 200 and len(body) < 500_000:
            key = self._key(url)
            data = {"url": url, "status": status, "headers": dict(headers), "body": body, "ts": time.time()}
            try:
                (CACHE_DIR / f"{key}.json").write_text(json.dumps(data))
            except Exception:
                pass

    def clear(self):
        for f in CACHE_DIR.glob("*.json"):
            f.unlink(missing_ok=True)


class ZionHTTP:
    """Ultra-lightweight HTTP client with cookie persistence and caching."""

    RETRY_CODES = {429, 500, 502, 503, 504}
    MAX_RETRIES = 3
    RETRY_BACKOFF = [1, 2, 4]

    def __init__(self, session_name="default"):
        self.session_name = session_name
        self.cookie_file = SESSION_DIR / f"{session_name}_cookies.txt"
        self.session_file = SESSION_DIR / f"{session_name}_session.json"

        self.cookie_jar = http.cookiejar.MozillaCookieJar(str(self.cookie_file))
        if self.cookie_file.exists():
            try:
                self.cookie_jar.load(ignore_discard=True, ignore_expires=True)
            except Exception:
                pass

        self.ssl_ctx = ssl.create_default_context()
        self.ssl_ctx.minimum_version = ssl.TLSVersion.TLSv1_2

        cookie_proc = urllib.request.HTTPCookieProcessor(self.cookie_jar)
        https_handler = urllib.request.HTTPSHandler(context=self.ssl_ctx)
        self.opener = urllib.request.build_opener(cookie_proc, https_handler)

        self.headers = {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9,pt-BR;q=0.8",
            "Accept-Encoding": "identity",
            "Connection": "keep-alive",
            "DNT": "1",
            "Upgrade-Insecure-Requests": "1",
        }

        self.session = self._load_session()
        self.cache = ResponseCache()

    def _load_session(self):
        if self.session_file.exists():
            try:
                return json.loads(self.session_file.read_text())
            except Exception:
                pass
        return {"csrf": {}, "auth": {}, "last_url": "", "referer": ""}

    def _save(self):
        try:
            self.cookie_jar.save(ignore_discard=True, ignore_expires=True)
        except Exception:
            pass
        try:
            self.session_file.write_text(json.dumps(self.session, indent=2, default=str))
        except Exception:
            pass

    def _decompress(self, data, encoding):
        if encoding == "gzip":
            try:
                return gzip.decompress(data)
            except Exception:
                pass
        elif encoding == "deflate":
            try:
                return zlib.decompress(data, -zlib.MAX_WBITS)
            except Exception:
                try:
                    return zlib.decompress(data)
                except Exception:
                    pass
        return data

    def request(self, url, method="GET", data=None, headers=None, json_data=None, use_cache=True):
        if method == "GET" and use_cache:
            cached = self.cache.get(url)
            if cached:
                return cached

        h = dict(self.headers)
        if self.session.get("referer"):
            h["Referer"] = self.session["referer"]
        if headers:
            h.update(headers)

        if json_data is not None:
            data = json.dumps(json_data).encode("utf-8")
            h["Content-Type"] = "application/json"
        elif data and isinstance(data, dict):
            data = urllib.parse.urlencode(data).encode("utf-8")
            if method == "POST":
                h["Content-Type"] = "application/x-www-form-urlencoded"

        last_result = None
        for attempt in range(self.MAX_RETRIES):
            result = self._do_request(url, method, data, h)
            status = result[0]
            if status not in self.RETRY_CODES:
                return result
            last_result = result
            if attempt < self.MAX_RETRIES - 1:
                delay = self.RETRY_BACKOFF[min(attempt, len(self.RETRY_BACKOFF) - 1)]
                if status == 429:
                    retry_after = result[1].get("Retry-After", "")
                    if str(retry_after).isdigit():
                        delay = min(int(retry_after), 10)
                time.sleep(delay)
        return last_result

    def _do_request(self, url, method, data, h):
        req = urllib.request.Request(url, data=data, headers=h, method=method)
        try:
            resp = self.opener.open(req, timeout=TIMEOUT)
            status = resp.getcode()
            rh = dict(resp.headers)
            final_url = resp.geturl()
            body_raw = resp.read(MAX_RESPONSE)
            enc = rh.get("Content-Encoding", "").lower()
            if enc:
                body_raw = self._decompress(body_raw, enc)
            charset = "utf-8"
            ct = rh.get("Content-Type", "")
            if "charset=" in ct:
                charset = ct.split("charset=")[-1].split(";")[0].strip()
            body = body_raw.decode(charset, errors="replace")
            self.session["last_url"] = final_url
            self.session["referer"] = final_url
            self._save()
            if method == "GET" and status == 200:
                self.cache.put(url, status, rh, body)
            return status, rh, body, final_url
        except urllib.error.HTTPError as e:
            body = ""
            try:
                raw = e.read(MAX_RESPONSE)
                enc = e.headers.get("Content-Encoding", "").lower()
                if enc:
                    raw = self._decompress(raw, enc)
                body = raw.decode("utf-8", errors="replace")
            except Exception:
                pass
            self._save()
            return e.code, dict(e.headers), body, url
        except urllib.error.URLError as e:
            return 0, {}, f"Connection error: {e.reason}", url
        except Exception as e:
            return 0, {}, f"Error: {e}", url

    def get(self, url, **kw):
        return self.request(url, "GET", **kw)

    def post(self, url, data=None, **kw):
        return self.request(url, "POST", data=data, **kw)


class ZionPage:
    """Parsed page with easy access to text, links, forms."""

    def __init__(self, status, headers, body, url=""):
        self.status = status
        self.headers = headers if isinstance(headers, dict) else {}
        self.body = body or ""
        self.url = url
        self._parsed = False
        self._parser = None

    def _ensure_parsed(self):
        if self._parsed:
            return
        self._parser = ZionHTMLParser()
        ct = self.headers.get("Content-Type", "")
        if "html" in ct or "xml" in ct or (not ct and "<html" in self.body[:500].lower()):
            try:
                self._parser.feed(self.body)
            except Exception:
                pass
        self._parsed = True

    @property
    def title(self):
        self._ensure_parsed()
        return self._parser.title or "(no title)"

    @property
    def text(self):
        self._ensure_parsed()
        t = self._parser.get_text()
        if not t.strip() and self.body:
            ct = self.headers.get("Content-Type", "")
            if "json" in ct or "text/plain" in ct or "html" not in ct:
                return self.body.strip()
        return t

    @property
    def links(self):
        self._ensure_parsed()
        return self._parser.get_links(self.url)

    @property
    def forms(self):
        self._ensure_parsed()
        return self._parser.get_forms()

    @property
    def meta(self):
        self._ensure_parsed()
        return self._parser.meta

    @property
    def js_redirects(self):
        self._ensure_parsed()
        return self._parser.js_redirects

    @property
    def is_json(self):
        ct = self.headers.get("Content-Type", "")
        return "json" in ct

    def json(self):
        try:
            return json.loads(self.body)
        except Exception:
            return None

    def find_links(self, pattern):
        c = re.compile(pattern, re.I)
        return [l for l in self.links if c.search(l["url"]) or c.search(l.get("text", ""))]

    def find_form(self, pattern=None, index=0):
        if pattern:
            for f in self.forms:
                if (re.search(pattern, f["action"], re.I) or
                        re.search(pattern, f.get("id", ""), re.I)):
                    return f
        if self.forms and index < len(self.forms):
            return self.forms[index]
        return None
