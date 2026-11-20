#!/usr/bin/env python3
"""Daily price job for reventi-prices.

Three commands, run in this order by .github/workflows/publish.yml:

  previous   download the files that are published now (the baseline for the checks)
  build      fetch TCGCSV and the ECB rates and write the new files
  validate   check the new files; a problem makes the job fail before the deploy

Standard library only. See README.md for the file format.
"""
from __future__ import annotations

import argparse
import gzip
import http.client
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

VERSION = 1
# TCGCSV blocks generic or missing User-Agents (https://tcgcsv.com/docs#usage-guidelines).
USER_AGENT = "reventi-prices/1.0.0 (+https://github.com/facit/reventi-prices)"
TCGCSV = "https://tcgcsv.com"
# TCGplayer categories: 79 is Star Wars: Unlimited, 92 is Cyberpunk TCG.
CATEGORIES = (79, 92)
ECB_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
ECB_NS = "{http://www.ecb.int/vocabulary/2002-08-01/eurofxref}"
PAGES_URL = "https://facit.github.io/reventi-prices"
FX_PATH = "fx/eur.json"

# TCGCSV asks for 100 ms between requests; its FAQ example uses 250 ms.
SPACING_S = 0.25
RETRY_DELAYS_S = (2, 5, 15)
RETRY_STATUSES = {429, 500, 502, 503, 504}
TIMEOUT_S = 30

# The page at the root of the site: what the files are, and the source lines.
INDEX_HTML = """<!doctype html>
<meta charset="utf-8">
<title>reventi-prices</title>
<h1>reventi-prices</h1>
<p>Daily price files for the SWU Scan and DeckWare apps.
Format and rules: <a href="https://github.com/facit/reventi-prices">github.com/facit/reventi-prices</a>.</p>
<ul>
<li><a href="tcgplayer/79.json">tcgplayer/79.json</a>: Star Wars: Unlimited</li>
<li><a href="tcgplayer/92.json">tcgplayer/92.json</a>: Cyberpunk TCG</li>
<li><a href="fx/eur.json">fx/eur.json</a>: euro foreign exchange reference rates</li>
</ul>
<p>Card prices: TCGplayer data through <a href="https://tcgcsv.com">TCGCSV</a>. Thank you, TCGCSV.</p>
<p>Exchange rates: source European Central Bank. Reference rates, for information purposes only.</p>
"""


class JobError(Exception):
    """A problem that must fail the job."""


class HttpStatusError(JobError):
    def __init__(self, url: str, status: int):
        super().__init__(f"{url}: HTTP {status}")
        self.status = status


def game_path(category: int) -> str:
    return f"tcgplayer/{category}.json"


def published_paths() -> list[str]:
    return [game_path(category) for category in CATEGORIES] + [FX_PATH]


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- network


def http_get(url: str, sleep=time.sleep) -> bytes:
    """GET with our User-Agent. Retries server errors, throttling and network errors."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"})
    for delay in (*RETRY_DELAYS_S, None):
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
                body = response.read()
                if response.headers.get("Content-Encoding") == "gzip":
                    body = gzip.decompress(body)
                return body
        except urllib.error.HTTPError as error:
            error.close()
            if error.code not in RETRY_STATUSES or delay is None:
                raise HttpStatusError(url, error.code) from error
            reason = f"HTTP {error.code}"
        except (OSError, http.client.HTTPException) as error:
            if delay is None:
                raise JobError(f"{url}: {error}") from error
            reason = str(error)
        print(f"retry in {delay} s: {url}: {reason}", file=sys.stderr)
        sleep(delay)
    raise AssertionError("unreachable")


class Tcgcsv:
    """Access to TCGCSV: one request at a time, with a pause between requests, and a count."""

    def __init__(self, get=http_get, sleep=time.sleep):
        self._get = get
        self._sleep = sleep
        self.requests = 0

    def get(self, path: str) -> bytes:
        if self.requests:
            self._sleep(SPACING_S)
        self.requests += 1
        return self._get(f"{TCGCSV}/{path}")


# ---------------------------------------------------------------- parsing


def parse_last_updated(data: bytes) -> str | None:
    """TCGCSV's last-updated.txt ("2026-11-19T20:04:34+0000") as UTC ISO 8601, or None."""
    try:
        stamp = datetime.strptime(data.decode("utf-8").strip(), "%Y-%m-%dT%H:%M:%S%z")
    except ValueError:
        return None
    return stamp.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_envelope(data: bytes, what: str) -> list:
    """The results of a TCGCSV response: {"success": true, "errors": [], "results": [...]}."""
    try:
        doc = json.loads(data)
    except ValueError as error:
        raise JobError(f"{what}: the response is not JSON ({error})") from error
    if not isinstance(doc, dict) or doc.get("success") is not True:
        raise JobError(f"{what}: success is not true")
    results = doc.get("results")
    if not isinstance(results, list):
        raise JobError(f"{what}: there is no results list")
    total = doc.get("totalItems")
    if total is not None and total != len(results):
        raise JobError(f"{what}: totalItems is {total} but there are {len(results)} results")
    return results


def parse_ecb(data: bytes) -> tuple[str, dict[str, float]]:
    """The date and the rates (1 EUR = rate units of the currency) of the ECB daily XML."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as error:
        raise JobError(f"ECB: the response is not XML ({error})") from error
    day = root.find(f"./{ECB_NS}Cube/{ECB_NS}Cube[@time]")
    if day is None:
        raise JobError("ECB: there is no dated Cube element")
    try:
        rates = {cube.attrib["currency"]: float(cube.attrib["rate"]) for cube in day.findall(f"{ECB_NS}Cube")}
    except (KeyError, ValueError) as error:
        raise JobError(f"ECB: a rate is not readable ({error})") from error
    return day.attrib["time"], rates


# ---------------------------------------------------------------- build


def build_game(category: int, client: Tcgcsv, last_updated: str | None, generated_at: str) -> dict:
    """One game's file: the header, TCGCSV's group list and TCGCSV's price rows, all unchanged."""
    groups_path = f"tcgplayer/{category}/groups"
    groups = parse_envelope(client.get(groups_path), f"category {category} groups")
    prices = {}
    for group in groups:
        group_id = group.get("groupId") if isinstance(group, dict) else None
        if not isinstance(group_id, int):
            raise JobError(f"category {category} groups: a group has no integer groupId")
        try:
            rows = parse_envelope(client.get(f"tcgplayer/{category}/{group_id}/prices"), f"group {group_id} prices")
        except HttpStatusError as error:
            if error.status != 404:
                raise
            # TCGCSV has no price file for a group that has no products yet.
            print(f"group {group_id}: no price file (HTTP 404), written as empty", file=sys.stderr)
            rows = []
        prices[str(group_id)] = rows
    return {
        "version": VERSION,
        "generatedAt": generated_at,
        "source": {"name": "TCGCSV", "url": f"{TCGCSV}/{groups_path}", "lastUpdated": last_updated},
        "category": category,
        "groups": groups,
        "prices": prices,
    }


def build_fx(data: bytes, generated_at: str) -> dict:
    date, rates = parse_ecb(data)
    return {
        "version": VERSION,
        "generatedAt": generated_at,
        "source": {"name": "European Central Bank", "url": ECB_URL},
        "date": date,
        "base": "EUR",
        "rates": rates,
    }


def write_json(path: Path, doc: dict) -> None:
    """Compact JSON: no spaces and no line breaks, so the file is small before and after gzip."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")


def read_json(path: Path):
    """The parsed file, or None if it is missing, empty or not JSON."""
    try:
        return json.loads(path.read_bytes())
    except (OSError, ValueError):
        return None


def is_reusable(previous, category: int, last_updated: str | None) -> bool:
    """True if the published file was made from the TCGCSV build that is online now.

    TCGCSV asks clients to sync only when last-updated.txt is newer than their last pull.
    """
    return (
        last_updated is not None
        and isinstance(previous, dict)
        and previous.get("version") == VERSION
        and previous.get("category") == category
        and isinstance(previous.get("source"), dict)
        and previous["source"].get("lastUpdated") == last_updated
    )


def build(out_dir: Path, previous_dir: Path, force: bool = False, get=http_get, sleep=time.sleep) -> None:
    started = time.monotonic()
    generated_at = utc_now()
    client = Tcgcsv(get, sleep)
    last_updated = parse_last_updated(client.get("last-updated.txt"))
    if last_updated is None:
        print("TCGCSV last-updated.txt is not readable; lastUpdated will be null", file=sys.stderr)

    states = {}
    for category in CATEGORIES:
        rel = game_path(category)
        if not force and is_reusable(read_json(previous_dir / rel), category, last_updated):
            (out_dir / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(previous_dir / rel, out_dir / rel)
            states[rel] = "reused, TCGCSV has no newer build"
        else:
            write_json(out_dir / rel, build_game(category, client, last_updated, generated_at))
            states[rel] = "fetched"

    if "fetched" in states.values() and parse_last_updated(client.get("last-updated.txt")) != last_updated:
        raise JobError("TCGCSV made a new build during the sync; the files can be mixed. Run the job again.")

    write_json(out_dir / FX_PATH, build_fx(get(ECB_URL), generated_at))
    states[FX_PATH] = "fetched"
    (out_dir / "index.html").write_text(INDEX_HTML, encoding="utf-8")

    lines = [f"{summary_line(out_dir, rel)}, {states[rel]}" for rel in published_paths()]
    lines.append(f"TCGCSV requests: {client.requests}. Run time: {time.monotonic() - started:.1f} s.")
    print("\n".join(lines))
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as summary:
            summary.write("".join(f"- {line}\n" for line in lines))


def summary_line(out_dir: Path, rel: str) -> str:
    data = (out_dir / rel).read_bytes()
    doc = json.loads(data)
    if rel == FX_PATH:
        content = f"{len(doc['rates'])} rates of {doc['date']}"
    else:
        rows = sum(len(rows) for rows in doc["prices"].values())
        content = f"{len(doc['groups'])} groups, {rows} price rows"
    return f"{rel}: {content}, {len(data)} bytes ({len(gzip.compress(data, 6))} bytes with gzip)"


def fetch_previous(previous_dir: Path, get=http_get) -> None:
    """Download the published files. A 404 means that there is no file yet (first publish)."""
    for rel in published_paths():
        target = previous_dir / rel
        try:
            data = get(f"{PAGES_URL}/{rel}")
        except HttpStatusError as error:
            if error.status != 404:
                raise
            target.unlink(missing_ok=True)
            print(f"{rel}: not published yet (HTTP 404)")
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        print(f"{rel}: {len(data)} bytes")


# ---------------------------------------------------------------- validation


def allowed_group_drop(previous_count: int) -> int:
    """How many groups a game can lose from one day to the next.

    TCGplayer sometimes removes or merges a group (a duplicate, a set filed twice). The published
    file is the baseline, so a strict rule would stop all updates until a person steps in. A drop
    of one group, or of 10 % for a large game, passes. A larger drop looks like a broken group
    list and fails.
    """
    return max(1, previous_count // 10)


def is_utc_time(value) -> bool:
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        return False
    return True


def check_header(doc: dict) -> list[str]:
    problems = []
    if doc.get("version") != VERSION:
        problems.append(f"version is not {VERSION}")
    if not is_utc_time(doc.get("generatedAt")):
        problems.append("generatedAt is not a UTC time")
    if not isinstance(doc.get("source"), dict) or not isinstance(doc["source"].get("url"), str):
        problems.append("source.url is missing")
    return problems


def check_game(doc, category: int) -> list[str]:
    if not isinstance(doc, dict):
        return ["the file is not a JSON object"]
    problems = check_header(doc)
    if doc.get("category") != category:
        problems.append(f"category is not {category}")
    groups, prices = doc.get("groups"), doc.get("prices")
    if not isinstance(groups, list) or not groups:
        return problems + ["there are no groups"]
    if not isinstance(prices, dict) or any(not isinstance(rows, list) for rows in prices.values()):
        return problems + ["prices is not an object of row lists"]
    ids = [group.get("groupId") if isinstance(group, dict) else None for group in groups]
    if any(not isinstance(group_id, int) for group_id in ids) or len(set(ids)) != len(ids):
        problems.append("a groupId is missing, not an integer, or used twice")
    elif {str(group_id) for group_id in ids} != set(prices):
        problems.append("the prices keys are not the same as the groups")
    rows = [row for group_rows in prices.values() for row in group_rows]
    if not rows:
        problems.append("there are no price rows")
    if any(not isinstance(row, dict) or not isinstance(row.get("productId"), int) for row in rows):
        problems.append("a price row has no integer productId")
    return problems


def check_drop(doc, previous) -> list[str]:
    """Compare a valid new game file with the published one. No published file: no problems."""
    if not isinstance(previous, dict) or not isinstance(previous.get("groups"), list):
        return []
    problems = []
    before, now = len(previous["groups"]), len(doc["groups"])
    allowed = allowed_group_drop(before)
    if now < before - allowed:
        problems.append(f"{now} groups, but the published file has {before} (a drop of {allowed} is allowed)")
    # A group that had prices and now has none is a failed fetch, not a market change.
    old_prices = previous.get("prices") if isinstance(previous.get("prices"), dict) else {}
    emptied = sorted(group_id for group_id, rows in doc["prices"].items() if not rows and old_prices.get(group_id))
    if emptied:
        problems.append(f"groups with prices in the published file and none now: {', '.join(emptied)}")
    return problems


def check_fx(doc) -> list[str]:
    if not isinstance(doc, dict):
        return ["the file is not a JSON object"]
    problems = check_header(doc)
    if doc.get("base") != "EUR":
        problems.append("base is not EUR")
    try:
        datetime.strptime(doc.get("date"), "%Y-%m-%d")
    except (TypeError, ValueError):
        problems.append("date is not a YYYY-MM-DD date")
    rates = doc.get("rates")
    if not isinstance(rates, dict) or not rates:
        return problems + ["there are no rates"]
    if any(isinstance(rate, bool) or not isinstance(rate, (int, float)) or rate <= 0 for rate in rates.values()):
        problems.append("a rate is not a positive number")
    if "USD" not in rates:
        problems.append("the USD rate is missing")
    return problems


def validate(out_dir: Path, previous_dir: Path, allow_drop: bool = False) -> list[str]:
    """Every problem in the new files, as "<file>: <problem>". An empty list means: publish."""
    problems = []
    for rel in published_paths():
        path = out_dir / rel
        if not path.is_file() or path.stat().st_size == 0:
            problems.append(f"{rel}: the file is missing or empty")
            continue
        try:
            doc = json.loads(path.read_bytes())
        except ValueError as error:
            problems.append(f"{rel}: the JSON does not parse ({error})")
            continue
        if rel == FX_PATH:
            found = check_fx(doc)
        else:
            found = check_game(doc, int(Path(rel).stem))
            if not found and not allow_drop:
                found = check_drop(doc, read_json(previous_dir / rel))
        problems += [f"{rel}: {problem}" for problem in found]
    return problems


# ---------------------------------------------------------------- command line


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Daily price job for reventi-prices.")
    parser.add_argument("--out", type=Path, default=Path("public"), help="folder for the new files")
    parser.add_argument("--previous", type=Path, default=Path("previous"), help="folder for the published files")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("previous", help="download the files that are published now")
    build_parser = commands.add_parser("build", help="fetch the sources and write the new files")
    build_parser.add_argument("--force", action="store_true", help="fetch TCGCSV even if it has no newer build")
    validate_parser = commands.add_parser("validate", help="check the new files")
    validate_parser.add_argument("--allow-drop", action="store_true", help="skip the comparison with the published files")
    args = parser.parse_args(argv)

    try:
        if args.command == "previous":
            fetch_previous(args.previous)
        elif args.command == "build":
            build(args.out, args.previous, args.force)
        else:
            problems = validate(args.out, args.previous, args.allow_drop)
            if problems:
                print("\n".join(problems), file=sys.stderr)
                return 1
            print("All files are valid.")
    except JobError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
