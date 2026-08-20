#!/usr/bin/env python3
"""
Raw harvest. Everything the search returns, nothing dropped.

    python raw.py --dry-run          # show the plan, spend nothing
    python raw.py --broad            # 26 queries, about $2
    python raw.py                    # all 148, about $7
    python raw.py --slack            # also post to Slack as it goes

WHAT THIS DOES NOT DO, deliberately:

  No deduplication. The same URL comes back as many times as the search
  returns it, and you can see which queries found it.

  No domain blocking. Blogs, aggregators, news, forums, all of it.

  No sports filter. If a search returns a bicycle scheme, you see the
  bicycle scheme.

  No scoring gate. Nothing is held back for being low quality.

  Nothing written to `opportunities`. Raw results are not listings, and an
  unfiltered blog post landing in your app tonight would be a bad trade for
  a look at the data. Everything goes to a local file instead.

It DOES record what the filters would have done, in `_flags` on each result.
That is the point of the exercise: decide the filters from what actually comes
back rather than from a guess about what will.

Output:
    raw/harvest-YYYY-MM-DD-HHMM.jsonl    every result, one per line
    raw/harvest-YYYY-MM-DD-HHMM.csv      the same, openable in a spreadsheet
    raw/summary-YYYY-MM-DD-HHMM.md       domains, queries, what got flagged
"""

import argparse, csv, datetime as dt, json, os, re, sys
from urllib.parse import urlparse

import requests, yaml
import llm

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "raw")


PROMPT = """Find anything relevant to student athletes in India, especially
Tamil Nadu, for this search:

    {query}

Return EVERYTHING you find that is even loosely relevant, up to {n} results.
Do not filter for quality, recency or relevance — return the lot, including
blogs, news articles, aggregator listings and pages you are unsure about.
Breadth matters more than precision here.

Include expired and old items. Include things you are not sure qualify.
If a page is a list of several opportunities, return each one separately.

Return ONLY a JSON array, no markdown fences:

[{{
  "kind": "scholarship|college_quota|job|training|award|news|other",
  "title": "",
  "provider": "the organisation, if you can tell",
  "provider_kind": "central_govt|state_govt|psu|private_company|trust_foundation|csr|college|university|federation|media|unknown",
  "sports": ["specific sports, or [] if unclear"],
  "min_level": "school|district|state|national|international|null",
  "closes_on": "YYYY-MM-DD or null",
  "amount_inr": null,
  "how_to_apply": "online|offline_post|in_person|email|trial_only|unknown",
  "apply_url": null,
  "source_url": "the page",
  "summary": "two sentences on what this actually is",
  "evidence": {{"closes_on": "exact sentence stating the deadline, or null"}},
  "other_organisations": ["any other body named on the page"],
  "why_maybe_irrelevant": "if you doubt this belongs, say why — do not drop it"
}}]

Never invent a URL or a date. Null is correct where you do not know."""


# ---------------------------------------------------------------- observers
# These do not drop anything. They record what a filter WOULD have done, so
# you can decide the filters from real data.

SPORT_WORDS = ["sport", "athlete", "sportsperson", "games", "player", "coach",
               "tournament", "championship", "medal", "khelo", "olympic",
               "paralympic", "gradation", "trials", "quota", "stadium",
               "academy", "sdat", "athletics", "badminton", "kabaddi",
               "volleyball", "hockey", "football", "cricket", "basketball",
               "tennis", "swimming", "boxing", "wrestling", "weightlifting",
               "archery", "shooting", "gymnastics", "chess", "silambam"]

NOT_SPORTS = ["free bicycle", "bi-cycle", "cycle scheme", "free laptop",
              "bus pass", "girl child", "pudhumai penn", "single parent",
              "widow", "marriage assistance", "post matric scholarship"]

DEADLINE_WORDS = ["last date", "last day", "closing date", "closes on",
                  "deadline", "due date", "apply before", "apply by",
                  "on or before", "applications close", "final date"]

DATE_TOKEN = re.compile(
    r"\d{1,2}[./\-]\d{1,2}[./\-]\d{2,4}"
    r"|\d{1,2}(?:st|nd|rd|th)?\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)"
    r"|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{1,2}"
    r"|\d{4}-\d{2}-\d{2}", re.I)


def domain_of(url):
    h = urlparse(url or "").netloc.lower()
    return h[4:] if h.startswith("www.") else h


def observe(item, blocklist, seen_urls, board_urls):
    """Record what filters would have done. Drop nothing."""
    url = item.get("source_url") or ""
    dom = domain_of(url)
    blob = " ".join(str(item.get(k) or "") for k in
                    ("title", "summary", "source_url", "provider")).lower()

    would_block = any(dom == b or dom.endswith("." + b) for b in blocklist)
    notsport = (any(bad in blob for bad in NOT_SPORTS)
                or not any(w in blob for w in SPORT_WORDS))

    q = (item.get("evidence") or {}).get("closes_on") or ""
    quote_ok = bool(DATE_TOKEN.search(q.lower())) and \
               any(w in q.lower() for w in DEADLINE_WORDS)

    pts = 0
    if url.startswith("http"): pts += 2
    if re.search(r"\.(gov|nic)\.in$", dom): pts += 3
    elif dom.endswith((".ac.in", ".edu.in", ".org", ".org.in")): pts += 1
    if quote_ok: pts += 3
    elif q: pts -= 4
    if item.get("closes_on"):
        try:
            when = dt.datetime.strptime(str(item["closes_on"]), "%Y-%m-%d").date()
            age = (dt.date.today() - when).days
            pts += 3 if age < 0 else (-2 if age <= 120 else (-5 if age <= 500 else -12))
        except ValueError:
            pass
    if item.get("provider"): pts += 1
    if item.get("how_to_apply") not in (None, "unknown"): pts += 1

    return {
        "domain": dom,
        "would_block": would_block,
        "would_drop_notsport": notsport,
        "duplicate_in_run": url in seen_urls,
        "already_on_board": url in board_urls,
        "would_score": pts,
        "would_publish": pts >= 8,
        "quote_reads_as_deadline": quote_ok,
    }


# ---------------------------------------------------------------- slack

class Slack:
    """
    Posts a digest per query, not one message per result. 148 queries times
    10 results is 1,500 messages, which is not a report, it is a denial of
    service on your own channel.
    """

    def __init__(self, url):
        self.url = url
        self.on = bool(url)
        self.posted = 0

    def post(self, text, blocks=None):
        if not self.on:
            return
        try:
            payload = {"text": text}
            if blocks:
                payload["blocks"] = blocks
            r = requests.post(self.url, json=payload, timeout=15)
            if r.status_code != 200:
                print(f"    slack: {r.status_code} {r.text[:100]}")
            else:
                self.posted += 1
        except Exception as e:
            print(f"    slack failed ({type(e).__name__}) — continuing")

    def digest(self, query, items):
        if not self.on or not items:
            return
        lines = [f"*{query}* — {len(items)} results"]
        for it in items[:8]:
            dl = it.get("closes_on") or "no date"
            dom = domain_of(it.get("source_url"))
            title = (it.get("title") or "untitled")[:70]
            lines.append(f"• <{it.get('source_url')}|{title}> — {dl} · {dom}")
        if len(items) > 8:
            lines.append(f"_...and {len(items)-8} more_")
        self.post("\n".join(lines))


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--broad", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--slack", action="store_true",
                    help="post digests to SLACK_WEBHOOK_URL as the run goes")
    args = ap.parse_args()

    cfg = yaml.safe_load(open(os.path.join(HERE, "queries.yml")))
    st = cfg["settings"]
    blocklist = cfg.get("blocked_domains", [])

    queries = [(q, "broad") for q in cfg["broad"]]
    if not args.broad:
        queries += [(q, "organisations") for q in cfg["organisations"]]
        queries += [(q, "sports") for q in cfg["sports"]]
        queries += [(q, "tamil") for q in cfg["tamil"]]
    if args.limit:
        queries = queries[:args.limit]

    per = llm.WEB_SEARCH_PER_CALL + (llm.SEARCH_CONTENT_TOKENS / 1e6) * 0.15
    print(f"\nRAW HARVEST — {len(queries)} queries")
    print(f"  nothing filtered, nothing deduplicated, nothing written to the board")
    print(f"  search fee ~${len(queries)*per:.2f}, realistic total ~${len(queries)*per*4:.2f}")

    slack = Slack(os.environ.get("SLACK_WEBHOOK_URL") if args.slack else None)
    if args.slack and not slack.on:
        print("  SLACK_WEBHOOK_URL not set — continuing without Slack")

    if args.dry_run:
        for i, (q, g) in enumerate(queries, 1):
            print(f"  {i:3}. [{g:13}] {q}")
        print(f"\nDry run. Nothing searched, nothing spent.")
        return

    client = llm.client()
    bud = llm.Budget()
    bud.check()

    # read the board only to FLAG overlaps, never to skip them
    board_urls = set()
    base, key = os.environ.get("SUPABASE_URL", "").rstrip("/"), os.environ.get("SUPABASE_SERVICE_KEY", "")
    if base and key:
        try:
            r = requests.get(f"{base}/rest/v1/opportunities",
                             headers={"apikey": key, "Authorization": f"Bearer {key}"},
                             params={"select": "source_url", "limit": "20000"}, timeout=30)
            board_urls = {x["source_url"] for x in r.json()} if r.ok else set()
            print(f"  {len(board_urls)} URLs already on the board (flagged, not skipped)")
        except Exception:
            pass

    os.makedirs(OUT, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y-%m-%d-%H%M")
    jsonl_path = os.path.join(OUT, f"harvest-{stamp}.jsonl")
    csv_path = os.path.join(OUT, f"harvest-{stamp}.csv")

    slack.post(f"*Raw harvest started* — {len(queries)} queries, "
               f"nothing filtered. Results land in `{os.path.basename(jsonl_path)}`.")

    all_rows, seen_urls, spent = [], set(), 0.0

    for i, (q, group) in enumerate(queries, 1):
        if spent >= st["cost_ceiling_usd"]:
            print(f"\n  ceiling reached at ${spent:.2f}")
            slack.post(f"Stopped at the ${st['cost_ceiling_usd']} ceiling, "
                       f"after {i-1} of {len(queries)} queries.")
            break

        print(f"[{i}/{len(queries)}] [{group}] {q[:56]}")
        llm.journal("query", {"q": q, "group": group})

        try:
            text, ti, to, used = llm.search(
                client, PROMPT.format(query=q, n=st["results_per_query"]),
                st["max_searches_per_query"])
        except Exception as e:
            print(f"    failed: {type(e).__name__}: {e}")
            continue

        spent += bud.record(f"raw:{group}", llm.SEARCH_MODEL, ti, to, used)

        # salvage complete objects from a truncated array rather than losing
        # a query that was already paid for
        cleaned = re.sub(r"^```(?:json)?|```$", "", (text or "").strip(), flags=re.M).strip()
        m = re.search(r"\[.*\]", cleaned, re.S)
        body = m.group(0) if m else cleaned
        try:
            found = json.loads(body)
        except json.JSONDecodeError:
            found = []
            for chunk in re.findall(r"\{(?:[^{}]|\{[^{}]*\})*\}", body, re.S):
                try:
                    o = json.loads(chunk)
                    if isinstance(o, dict):
                        found.append(o)
                except json.JSONDecodeError:
                    pass
        if isinstance(found, dict):
            found = [found]
        found = [f for f in found if isinstance(f, dict)]

        llm.journal("result", {"q": q, "n": len(found), "items": found})

        rows = []
        for seq, it in enumerate(found, 1):
            flags = observe(it, blocklist, seen_urls, board_urls)
            if it.get("source_url"):
                seen_urls.add(it["source_url"])
            rows.append({"run": stamp, "query": q, "group": group, "seq": seq,
                         **it, "_flags": flags})

        # write immediately. a crash after this point costs nothing.
        with open(jsonl_path, "a") as f:
            for r in rows:
                f.write(json.dumps(r, default=str) + "\n")
        all_rows.extend(rows)

        f = sum(1 for r in rows if r["_flags"]["would_block"])
        ns = sum(1 for r in rows if r["_flags"]["would_drop_notsport"])
        dup = sum(1 for r in rows if r["_flags"]["duplicate_in_run"])
        print(f"    {len(rows)} results (would block {f}, would drop {ns}, "
              f"dupes {dup}) | ${spent:.3f}")

        slack.digest(q, found)

    # ---------- spreadsheet ----------
    if all_rows:
        cols = ["run", "query", "group", "seq", "kind", "title", "provider",
                "provider_kind", "min_level", "closes_on", "amount_inr",
                "how_to_apply", "source_url", "apply_url", "summary",
                "why_maybe_irrelevant"]
        flagcols = ["domain", "would_block", "would_drop_notsport",
                    "duplicate_in_run", "already_on_board", "would_score",
                    "would_publish"]
        with open(csv_path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(cols + flagcols)
            for r in all_rows:
                w.writerow([str(r.get(c, "") or "")[:400] for c in cols] +
                           [r["_flags"].get(c, "") for c in flagcols])

    # ---------- summary ----------
    doms, queries_seen = {}, {}
    for r in all_rows:
        d = r["_flags"]["domain"]
        doms[d] = doms.get(d, 0) + 1
        queries_seen[r["query"]] = queries_seen.get(r["query"], 0) + 1

    would_block = sum(1 for r in all_rows if r["_flags"]["would_block"])
    would_drop = sum(1 for r in all_rows if r["_flags"]["would_drop_notsport"])
    dupes = sum(1 for r in all_rows if r["_flags"]["duplicate_in_run"])
    good = sum(1 for r in all_rows if r["_flags"]["would_publish"])
    dated = sum(1 for r in all_rows if r.get("closes_on"))

    lines = [
        f"# Raw harvest {stamp}", "",
        f"- **{len(all_rows)}** results from **{len([q for q,_ in queries])}** queries",
        f"- **{len(set(r.get('source_url') for r in all_rows))}** unique URLs "
        f"across **{len(doms)}** domains",
        f"- **{dated}** carried a deadline",
        f"- **${spent:.2f}** spent", "",
        "## What the filters would have done, had they been on", "",
        f"- would have blocked as aggregator or social: **{would_block}**",
        f"- would have dropped as not sports-related: **{would_drop}**",
        f"- duplicates within this run: **{dupes}**",
        f"- would have scored 8+ and published: **{good}**", "",
        "## Domains, by hits", "",
        "| domain | hits | currently blocked |", "|---|---|---|",
    ]
    for d, c in sorted(doms.items(), key=lambda x: -x[1])[:40]:
        blocked_now = any(d == b or d.endswith("." + b) for b in blocklist)
        lines.append(f"| {d} | {c} | {'yes' if blocked_now else ''} |")
    lines += ["", "## Queries that returned nothing", ""]
    empty = [q for q, _ in queries if q not in queries_seen]
    lines += [f"- {q}" for q in empty] or ["- none"]

    summary_path = os.path.join(OUT, f"summary-{stamp}.md")
    open(summary_path, "w").write("\n".join(lines))

    print(f"\n{'='*62}")
    print(f"{len(all_rows)} results | {len(doms)} domains | ${spent:.2f}")
    print(f"  would have blocked   {would_block}")
    print(f"  would have dropped   {would_drop}")
    print(f"  duplicates in run    {dupes}")
    print(f"  would have published {good}")
    print(f"\n  {jsonl_path}")
    print(f"  {csv_path}")
    print(f"  {summary_path}")

    slack.post(f"*Raw harvest finished* — {len(all_rows)} results from "
               f"{len(doms)} domains, ${spent:.2f} spent.\n"
               f"Would have blocked {would_block}, dropped {would_drop} as "
               f"not-sports, {dupes} duplicates, {good} would have published.\n"
               f"Files: `{os.path.basename(csv_path)}`")


if __name__ == "__main__":
    main()
