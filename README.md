# Source change checker

A single Python script that checks a fixed list of feeds and web pages for new items and changed text, and reports a source that could not be read as a failure, never as "no news".

**Why I built this.** A daily digest of mine read a page that had moved behind a login wall. The page answered 200, the extractor found no text, and the digest reported a quiet day. It was not a quiet day. This script is the rule that a source that cannot be read is a failure.

## What it is for

A scheduled check of feeds and pages can fail without telling you. A moved page redirects to a login wall and still answers 200. A feed changes format and parses to zero items. A rate limit returns nothing. Each of these looks like a quiet day.

This script treats all of them as failures. It records the final URL after redirects and fails a source that lands on another host or path. It fails a feed that answers but yields no dated items. For undated pages it hashes the main text and reports when the hash differs from your baseline. It uses the Python standard library only and calls no language model.

## Run it in one minute

You need Python 3.9 or later and network access. There is nothing to install and no account or key.

1. Download `check_sources.py` and `sources.example.json` into the same folder, and open a terminal in that folder. On Windows, type `python` or `py` where this page says `python3`.
2. List the example sources:

```bash
python3 check_sources.py --list
```

3. Run the check. `--since` keeps items dated on or after that day:

```bash
python3 check_sources.py --since 2026-11-16
```

The report is Markdown on standard output. Add `--out report.md` to save it too.

To watch your own sources, copy `sources.example.json` to `sources.json` and edit it. The script reads `sources.json` when it exists, and the example file when it does not. `--sources FILE` reads any other file.

| Field | Meaning |
|---|---|
| `id` | Short unique name, used by `--only` and `--override` |
| `name` | Label shown in the report |
| `url` | The feed or page to fetch |
| `kind` | `rss`, `atom`, or `hash` (an undated page) |
| `hosts` | Host names the source may be served from. A redirect to any other host is refused before it is followed |
| `baseline_hash` | `hash` sources only: the 16-character hash to compare against |
| `keywords` | Optional. Words or phrases; an item whose title or summary contains one is marked as a keyword match. Case does not matter, and a word matches only at word boundaries, so `link` does not match `LinkedIn` |
| `throttle` | Optional. `true` spaces requests to these sources 21 seconds apart, for sites that rate-limit |

Other options: `--only ID[,ID]` runs some sources, and `--override ID=URL` swaps one URL for a test.

Keywords live in the source list, not in the code, so each source can carry its own.

## How to check it worked

The report starts with a count line, then one table row per source:

```text
- Sources: 3; OK 3; **FAILED 0**
- Items since 2026-11-13: 10; keyword matches: 4
- Watched-page hash changes: 1
```

That is a real run of the shipped example list on 2026-11-20 (the hash change is the first run setting its baseline). `FAILED 0` and exit code `0` are the quiet-day result. Anything else is a failure to look at.

The exit code is `0` when every source is OK, `1` when one or more FAILED, and `2` for a usage error. A scheduler can alert on any non-zero exit.

To prove the failure path works, point one source at the wrong host:

```bash
python3 -I check_sources.py --since 2026-11-16 --only github-changelog --override github-changelog=https://example.com/
```

You should see this on standard error, a `SOURCE FAILURES` heading in the report, and exit code `1`:

```text
SOURCE FAILED: github-changelog: final URL host 'example.com' is not expected ['github.blog'] (final URL https://example.com/)
```

To set a hash baseline for a new `hash` source, run once without `baseline_hash`. The row reads `hash 0123456789abcdef (CHANGED (baseline None))`. Copy the 16 characters into `baseline_hash`. The next run reads `UNCHANGED` until the page text changes.

## Limits

- The example list is three public pages chosen only to show the three source kinds. Replace them with your own.
- The example `hash` source ships with no baseline, so its first run reads `CHANGED (baseline None)`. That is expected. Set the baseline as described above.
- Nothing is stored between runs. You choose `--since` each time and you update `baseline_hash` by hand.
- A `hash` source needs a `<main>` element with at least 1,000 characters of text. Any change to that text counts, including a date or a footer line inside `<main>`. It tells you that the page changed, not what changed.
- It reads the HTML the server sends. Pages that build their content with JavaScript are not supported.
- One attempt per source, with a 12 second timeout and no retry. A slow source is reported as FAILED.
- A keyword match means a word from the source's list appeared in the title or summary. It is not a judgement about the item.
- Items with no readable date are skipped and counted as undated. Dates are compared as each publisher wrote them, with no time zone conversion.
- You are responsible for the terms of use and robots rules of the sites you fetch. The script sends a generic User-Agent and no contact details.
- Tested on Python 3.9 and 3.14 on macOS only.
- On macOS, if every source fails with `CERTIFICATE_VERIFY_FAILED`, Python cannot see the system certificates: run the `Install Certificates.command` file in your Python folder once.
- Redirects are followed only to the hosts you list for a source, and only over http or https. A response over 5 MB, or XML that declares entities, is a failure.
- The path check works on whole segments: a source at `/blog` may land on `/blog/2026/post`, never on `/blog-login`.

## Licence

MIT. See [LICENSE](LICENSE).
