#!/usr/bin/env python3
"""check_sources.py - check a fixed list of web sources for new items and changes.

Deterministic and standard library only. No language model is called.

What it does, per source:
  * fetch with a 12-second total time limit, no retry, a plain generic User-Agent;
  * follow redirects only to the hosts you listed for that source, and record the
    final URL;
  * FAIL if a redirect points at an unexpected host, or the final URL lands outside
    the expected path (soft-404 / login-wall guard);
  * dated sources (RSS, Atom): parse dated items, FAIL when zero dated items parse
    (the "silent zero" guard), keep items dated on or after --since, and tag each
    one that matches the optional keyword list of its source;
  * hash sources (undated pages): hash the extracted main text, FAIL when the text
    is missing or too short, report CHANGED when the hash differs from the
    baseline.

A source marked "throttle": true is fetched at least 21 seconds after the previous
throttled source, for sites that rate-limit. A 429 is a source failure, never
"no news". No personal data is sent in any header.

The source list is a JSON file. By default the script reads sources.json next to
this file, and falls back to sources.example.json when that does not exist.

Usage:
  python3 -I check_sources.py --since 2026-11-16 [--out report.md]
          [--sources FILE] [--only ID[,ID]] [--override ID=URL] [--list]

Exit codes: 0 all sources OK; 1 one or more sources FAILED; 2 usage error.
"""

import argparse
import datetime as dt
import email.utils
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import xml.parsers.expat

USER_AGENT = "Mozilla/5.0 (compatible; source-change-checker/1.0)"
TIMEOUT_SECONDS = 12          # total time for one fetch, connect and read included
MAX_BYTES = 5_000_000         # a larger response is a failure, not something to parse
THROTTLE_GAP_SECONDS = 21
MIN_HASH_TEXT_CHARS = 1000

# The source list lives in a JSON file so you can edit it without touching the code.
# See sources.example.json for the format and README.md for what each field means.
VALID_KINDS = ("rss", "atom", "hash")
REQUIRED_KEYS = ("id", "name", "url", "kind", "hosts")


def default_sources_path():
    here = os.path.dirname(os.path.abspath(__file__))
    own = os.path.join(here, "sources.json")
    return own if os.path.exists(own) else os.path.join(here, "sources.example.json")


def load_sources(path):
    """Read and validate the source list. Raises ValueError with a plain message."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except OSError as e:
        raise ValueError(f"cannot read sources file '{path}': {e.strerror or e}")
    except ValueError as e:
        raise ValueError(f"sources file '{path}' is not valid JSON: {e}")
    sources = data.get("sources") if isinstance(data, dict) else data
    if not isinstance(sources, list) or not sources:
        raise ValueError(f"sources file '{path}' must hold a non-empty list of sources")
    seen = set()
    for n, s in enumerate(sources, 1):
        if not isinstance(s, dict):
            raise ValueError(f"source {n} is not an object")
        missing = [k for k in REQUIRED_KEYS if k not in s]
        if missing:
            raise ValueError(f"source {n} ({s.get('id', 'no id')}) is missing: {', '.join(missing)}")
        sid = s["id"]
        for k in ("id", "name", "url", "kind"):
            if not isinstance(s[k], str) or not s[k].strip():
                raise ValueError(f"source {n} ({sid}): '{k}' must be a non-empty string")
        if s["kind"] not in VALID_KINDS:
            raise ValueError(f"source '{sid}' has kind '{s['kind']}'; use one of {', '.join(VALID_KINDS)}")
        if urllib.parse.urlsplit(s["url"]).scheme not in ("http", "https"):
            raise ValueError(f"source '{sid}': url must start with http:// or https://")
        hosts = s["hosts"]
        if not isinstance(hosts, list) or not hosts or not all(isinstance(h, str) and h for h in hosts):
            raise ValueError(f"source '{sid}' needs a non-empty 'hosts' list of host names")
        s["hosts"] = [h.lower() for h in hosts]
        if "keywords" in s:
            kw = s["keywords"]
            if not isinstance(kw, list) or not all(isinstance(k, str) and k.strip() for k in kw):
                raise ValueError(f"source '{sid}': 'keywords' must be a list of words or phrases")
        if "throttle" in s and not isinstance(s["throttle"], bool):
            raise ValueError(f"source '{sid}': 'throttle' must be true or false")
        if "baseline_hash" in s and not isinstance(s["baseline_hash"], str):
            raise ValueError(f"source '{sid}': 'baseline_hash' must be a string")
        if sid in seen:
            raise ValueError(f"source id '{sid}' is used twice")
        seen.add(sid)
    return sources


def keyword_patterns(src):
    """Word-bounded, case-insensitive patterns for the source's optional keyword list."""
    return [(k, re.compile(r"(?<!\w)" + re.escape(k) + r"(?!\w)", re.I)) for k in src.get("keywords", [])]


class SourceFailure(Exception):
    pass


class _EntityGuard:
    """Refuse XML that declares entities (entity-expansion guard).

    The check is done by the XML parser itself, not by searching the bytes, so it
    holds whatever encoding the document declares (UTF-16 included).
    """

    @staticmethod
    def check(body):
        def refuse(*_args):
            raise SourceFailure("XML declares entities; refused")

        probe = xml.parsers.expat.ParserCreate()
        probe.EntityDeclHandler = refuse
        try:
            probe.Parse(body, True)
        except xml.parsers.expat.ExpatError:
            pass  # malformed XML is reported by the real parse that follows


class _GuardedRedirect(urllib.request.HTTPRedirectHandler):
    """Check every redirect hop BEFORE it is requested: http(s) only, expected hosts only."""

    def __init__(self, hosts):
        self.hosts = hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urllib.parse.urlsplit(newurl)
        if target.scheme not in ("http", "https"):
            raise SourceFailure(f"redirect to a non-web address refused ({newurl})")
        if (target.hostname or "").lower() not in self.hosts:
            raise SourceFailure(
                f"redirect to host '{target.hostname}' refused, expected {self.hosts} (redirect target {newurl})"
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url, hosts):
    """GET, following redirects only to expected hosts. Returns (status, final_url, body_bytes).

    The time limit covers the whole fetch: a server that trickles one byte at a time
    cannot keep the run alive past TIMEOUT_SECONDS.
    """
    if urllib.parse.urlsplit(url).scheme not in ("http", "https"):
        raise SourceFailure(f"only http and https URLs are fetched ({url})")
    opener = urllib.request.build_opener(_GuardedRedirect(hosts))
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/rss+xml, application/atom+xml, application/xml, text/html;q=0.9, */*;q=0.5",
        },
    )
    deadline = time.monotonic() + TIMEOUT_SECONDS
    try:
        with opener.open(req, timeout=TIMEOUT_SECONDS) as resp:
            chunks, size = [], 0
            while True:
                if time.monotonic() > deadline:
                    raise SourceFailure(f"timeout after {TIMEOUT_SECONDS}s (slow response)")
                chunk = resp.read1(65536) if hasattr(resp, "read1") else resp.read(65536)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_BYTES:
                    raise SourceFailure(f"response is larger than {MAX_BYTES} bytes; refused")
                chunks.append(chunk)
            return resp.status, resp.geturl(), b"".join(chunks)
    except urllib.error.HTTPError as e:
        final = e.geturl() or url
        if e.code == 429:
            raise SourceFailure(f"HTTP 429 rate limited at {final} (a failure, not 'no news')")
        raise SourceFailure(f"HTTP {e.code} at {final}")
    except urllib.error.URLError as e:
        raise SourceFailure(f"network error: {e.reason}")
    except TimeoutError:
        raise SourceFailure(f"timeout after {TIMEOUT_SECONDS}s")
    except OSError as e:
        raise SourceFailure(f"network error: {e}")


def _norm_path(path):
    return re.sub(r"/{2,}", "/", urllib.parse.unquote(path)).rstrip("/")


def check_final_url(src, requested_url, final_url):
    req = urllib.parse.urlsplit(requested_url)
    fin = urllib.parse.urlsplit(final_url)
    if (fin.hostname or "").lower() not in src["hosts"]:
        raise SourceFailure(
            f"final URL host '{fin.hostname}' is not expected {src['hosts']} (final URL {final_url})"
        )
    want, got = _norm_path(req.path), _norm_path(fin.path)
    # Whole path segments only: /blog must not accept /blog-login or /blogs.
    if not (got == want or got.startswith(want + "/")):
        raise SourceFailure(
            f"final URL path '{fin.path}' left the expected path '{req.path}' (soft-404 or login wall? final URL {final_url})"
        )


def strip_tags(text):
    text = re.sub(r"<(script|style|noscript)\b[^>]*>.*?</\1>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    return " ".join(html.unescape(text).split())


def clean_title(text):
    """Feed titles are text, not markup: unescape once, then drop any tags that remain."""
    return strip_tags(html.unescape(text))


def parse_iso_date(raw):
    """ISO 8601 as feeds write it, on Python 3.9 as well as 3.11+.

    Accepts a trailing Z or z, +hhmm without a colon, and fractional seconds of any
    length. Returns None when the text is not a date.
    """
    s = raw.strip()
    if not s:
        return None
    s = re.sub(r"[zZ]$", "+00:00", s)
    s = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", s)
    s = re.sub(r"(\.\d{1,6})\d+", r"\1", s)
    s = re.sub(r"\.(\d{1,5})(?=[+-]|$)", lambda m: "." + m.group(1).ljust(6, "0"), s)
    try:
        return dt.datetime.fromisoformat(s)
    except ValueError:
        return None


def _as_utc(when):
    """Naive dates are treated as UTC, so items from one feed always compare."""
    return when if when.tzinfo else when.replace(tzinfo=dt.timezone.utc)


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _child_text(el, name):
    for c in el:
        if _local(c.tag) == name:
            return (c.text or "").strip()
    return ""


def parse_rss(body):
    _EntityGuard.check(body)
    try:
        root = ET.fromstring(body)
    except ET.ParseError as e:
        raise SourceFailure(f"RSS did not parse: {e}")
    if _local(root.tag) != "rss":
        raise SourceFailure(f"expected an <rss> root, got <{_local(root.tag)}>")
    items, undated = [], 0
    for it in root.iter():
        if _local(it.tag) != "item":
            continue
        title = _child_text(it, "title")
        # Prefer the plain RSS <link> over a namespaced one such as <atom:link rel="self">.
        link = ""
        for c in it:
            if c.tag == "link":
                link = (c.text or "").strip()
                break
        if not link:
            link = _child_text(it, "link")
        raw_date = _child_text(it, "pubDate") or _child_text(it, "date")
        when = None
        if raw_date:
            try:
                when = email.utils.parsedate_to_datetime(raw_date)
            except (TypeError, ValueError, IndexError):
                when = parse_iso_date(raw_date)
        if when is None:
            undated += 1
            continue
        summary = strip_tags(_child_text(it, "description"))[:600]
        items.append({"title": clean_title(title), "url": link, "when": _as_utc(when), "summary": summary})
    return items, undated


def parse_atom(body):
    _EntityGuard.check(body)
    try:
        root = ET.fromstring(body)
    except ET.ParseError as e:
        raise SourceFailure(f"Atom did not parse: {e}")
    if _local(root.tag) != "feed":
        raise SourceFailure(f"expected an Atom <feed> root, got <{_local(root.tag)}>")
    items, undated = [], 0
    for en in root:
        if _local(en.tag) != "entry":
            continue
        title = _child_text(en, "title")
        link = ""
        for c in en:
            if _local(c.tag) == "link" and c.get("rel", "alternate") == "alternate":
                link = c.get("href", "")
                break
        raw_date = _child_text(en, "published") or _child_text(en, "updated")
        when = parse_iso_date(raw_date) if raw_date else None
        if when is None:
            undated += 1
            continue
        summary = strip_tags(_child_text(en, "content") or _child_text(en, "summary"))[:600]
        items.append({"title": clean_title(title), "url": link, "when": _as_utc(when), "summary": summary})
    return items, undated


def extract_main_text(body):
    text = body.decode("utf-8", "replace")
    m = re.search(r"<main\b.*?</main>", text, re.S | re.I)
    if not m:
        raise SourceFailure("no <main> element found (page format changed or soft-404)")
    return strip_tags(m.group(0))


def tag_item(item, patterns):
    hay = f"{item['title']} {item['summary']}"
    matched = [label for label, rx in patterns if rx.search(hay)]
    return {**item, "keyword_match": bool(matched), "terms": matched}


def run_source(src, since, throttle_clock):
    res = {"src": src, "status": "OK", "http": None, "final_url": None, "error": None,
           "parsed": 0, "undated": 0, "newest": None, "items": [], "hash": None, "hash_state": None}
    url = src["url"]
    throttled = bool(src.get("throttle"))
    if throttled:
        last = throttle_clock.get("last")
        if last is not None:
            wait = THROTTLE_GAP_SECONDS - (time.monotonic() - last)
            if wait > 0:
                time.sleep(wait)
    try:
        try:
            status, final_url, body = fetch(url, src["hosts"])
        finally:
            if throttled:
                throttle_clock["last"] = time.monotonic()
        res["http"], res["final_url"] = status, final_url
        check_final_url(src, url, final_url)
        if src["kind"] == "hash":
            main = extract_main_text(body)
            if len(main) < MIN_HASH_TEXT_CHARS:
                raise SourceFailure(f"extracted main text is only {len(main)} chars (< {MIN_HASH_TEXT_CHARS})")
            digest = hashlib.sha256(main.encode("utf-8")).hexdigest()[:16]
            res["hash"] = digest
            res["parsed"] = len(main)
            base = src.get("baseline_hash")
            res["hash_state"] = "UNCHANGED" if digest == base else f"CHANGED (baseline {base})"
            return res
        parser = {"rss": parse_rss, "atom": parse_atom}[src["kind"]]
        items, undated = parser(body)
        res["parsed"], res["undated"] = len(items), undated
        if not items:
            raise SourceFailure(
                f"answered HTTP {status} but parsed ZERO dated items ({undated} undated) - format change or soft-404"
            )
        res["newest"] = max(i["when"] for i in items).date()
        patterns = keyword_patterns(src)
        kept = [tag_item(i, patterns) for i in items if i["when"].date() >= since]
        kept.sort(key=lambda i: i["when"], reverse=True)
        res["items"] = kept
    except SourceFailure as e:
        res["status"], res["error"] = "FAILED", str(e)
    except Exception as e:  # any unexpected parser crash is a loud failure too
        res["status"], res["error"] = "FAILED", f"unexpected {type(e).__name__}: {e}"
    return res


def md_escape(s):
    return s.replace("|", "\\|").replace("\n", " ")


def md_url(u):
    return u.replace("|", "%7C").replace(")", "%29").replace(" ", "%20")


def render(results, since, started, sources_file):
    failed = [r for r in results if r["status"] == "FAILED"]
    changed = [r for r in results if r["hash_state"] and r["hash_state"].startswith("CHANGED")]
    all_items = [i for r in results for i in r["items"]]
    matched = [i for i in all_items if i["keyword_match"]]
    out = []
    out.append(f"# Source change check: since {since.isoformat()}")
    out.append("")
    out.append(f"- Run started: {started.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    out.append(f"- Script: `check_sources.py` (no model call); sources file: `{sources_file}`")
    out.append(f"- Sources: {len(results)}; OK {len(results) - len(failed)}; **FAILED {len(failed)}**")
    out.append(f"- Items since {since.isoformat()}: {len(all_items)}; keyword matches: {len(matched)}")
    out.append(f"- Watched-page hash changes: {len(changed)}")
    out.append("")
    if failed:
        out.append("## !!! SOURCE FAILURES - this run is NOT a quiet day !!!")
        out.append("")
        for r in failed:
            out.append(f"- **FAILED `{r['src']['id']}`** ({md_escape(r['src']['name'])}): {md_escape(r['error'])}")
        out.append("")
    if changed:
        out.append("## Watched page CHANGED")
        out.append("")
        for r in changed:
            out.append(f"- `{r['src']['id']}` {r['src']['url']}: hash {r['hash']} {r['hash_state']}")
        out.append("")
    out.append("## Sources")
    out.append("")
    out.append("| Source | Status | HTTP | Final URL | Dated items parsed | Newest | Items since |")
    out.append("|---|---|---|---|---|---|---|")
    for r in results:
        s = r["src"]
        if s["kind"] == "hash":
            parsed = f"hash {r['hash']} ({r['hash_state']})" if r["hash"] else "-"
            newest, since_n = "undated", "-"
        else:
            parsed = f"{r['parsed']}" + (f" (+{r['undated']} undated)" if r["undated"] else "")
            newest = r["newest"].isoformat() if r["newest"] else "-"
            since_n = str(len(r["items"]))
        out.append(f"| `{s['id']}` {md_escape(s['name'])} | **{r['status']}** | {r['http'] or '-'} | "
                   f"{md_escape(r['final_url'] or s['url'])} | {parsed} | {newest} | {since_n} |")
    out.append("")
    out.append(f"## Items since {since.isoformat()}")
    out.append("")
    if not all_items:
        out.append("No new dated items in the window.")
    else:
        out.append("| Date | Source | Keyword match | Matched terms | Title |")
        out.append("|---|---|---|---|---|")
        for r in results:
            for i in r["items"]:
                km = "**YES**" if i["keyword_match"] else "no"
                out.append(f"| {i['when'].date().isoformat()} | `{r['src']['id']}` | {km} | "
                           f"{', '.join(i['terms']) or '-'} | [{md_escape(i['title'])}]({md_url(i['url'])}) |")
    out.append("")
    out.append(f"Exit status: {'1 (one or more sources FAILED)' if failed else '0 (all sources OK)'}")
    out.append("")
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Check a fixed list of web sources for new items and changes.")
    ap.add_argument("--since", required=False, help="keep items dated on or after YYYY-MM-DD (publisher-local date)")
    ap.add_argument("--out", help="also write the Markdown report to this file")
    ap.add_argument("--only", help="comma-separated source ids to run")
    ap.add_argument("--override", action="append", default=[], metavar="ID=URL",
                    help="replace a source URL (testing; host and path checks still apply)")
    ap.add_argument("--list", action="store_true", help="list source ids and exit")
    ap.add_argument("--sources", metavar="FILE",
                    help="JSON source list (default: sources.json next to this script, else sources.example.json)")
    a = ap.parse_args(argv)

    sources_path = a.sources or default_sources_path()
    try:
        loaded = load_sources(sources_path)
    except ValueError as e:
        ap.error(str(e))

    if a.list:
        for s in loaded:
            print(f"{s['id']}\t{s['kind']}\t{s['url']}")
        return 0
    if not a.since:
        ap.error("--since is required")
    try:
        since = dt.date.fromisoformat(a.since)
    except ValueError:
        ap.error("--since must be YYYY-MM-DD")

    sources = [dict(s) for s in loaded]
    by_id = {s["id"]: s for s in sources}
    for ov in a.override:
        sid, _, url = ov.partition("=")
        if sid not in by_id or not url:
            ap.error(f"bad --override '{ov}' (ids: {', '.join(by_id)})")
        by_id[sid]["url"] = url
        by_id[sid]["name"] += " [URL OVERRIDDEN]"
    if a.only is not None:
        wanted = [x.strip() for x in a.only.split(",") if x.strip()]
        if not wanted:
            ap.error("--only needs at least one source id")
        unknown = [w for w in wanted if w not in by_id]
        if unknown:
            ap.error(f"unknown --only ids: {unknown}")
        sources = [by_id[w] for w in wanted]
    if a.out:
        out_dir = os.path.dirname(os.path.abspath(a.out))
        if not os.path.isdir(out_dir):
            ap.error(f"--out folder does not exist: {out_dir}")

    started = dt.datetime.now(dt.timezone.utc).astimezone()
    throttle_clock = {}
    results = [run_source(s, since, throttle_clock) for s in sources]
    report = render(results, since, started, os.path.basename(sources_path))
    sys.stdout.write(report)
    if a.out:
        try:
            with open(a.out, "w", encoding="utf-8") as fh:
                fh.write(report)
        except OSError as e:
            print(f"cannot write --out file '{a.out}': {e.strerror or e}", file=sys.stderr)
            return 2
    failed = [r for r in results if r["status"] == "FAILED"]
    for r in failed:
        print(f"SOURCE FAILED: {r['src']['id']}: {r['error']}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
