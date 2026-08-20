#!/usr/bin/env python3
"""
Reads the results files and writes a readable summary.

Used by the GitHub workflow to fill the run page, and useful on its own:

    python summarise.py                     # print to screen
    python summarise.py --slack             # also post to Slack

Kept as a real file rather than embedded in the workflow YAML. A heredoc
inside YAML inside a shell conditional is three quoting rules at once, and
it silently broke the first time it was written that way.
"""

import collections
import datetime as dt
import glob
import json
import os
import sys
import urllib.request


def load():
    rows = []
    for f in sorted(glob.glob("results-*.json")):
        try:
            data = json.load(open(f))
            rows += data if isinstance(data, list) else [data]
        except Exception as e:
            print(f"could not read {f}: {e}", file=sys.stderr)
    return rows


def split_by_date(rows):
    """Returns (open now, closed, no date)."""
    today = dt.date.today()
    live, closed, undated = [], [], []
    for r in rows:
        d = r.get("closes_on")
        if not d:
            undated.append(r); continue
        try:
            when = dt.datetime.strptime(str(d), "%Y-%m-%d").date()
        except (ValueError, TypeError):
            undated.append(r); continue
        (live if when >= today else closed).append(r)
    return live, closed, undated


def domains(rows):
    c = collections.Counter()
    for r in rows:
        u = r.get("source_url") or ""
        if "//" in u:
            c[u.split("/")[2].replace("www.", "")] += 1
    return c


def markdown(rows):
    live, closed, undated = split_by_date(rows)
    doms = domains(rows)
    out = []

    out.append("## Search results\n")
    out.append(f"**{len(rows)}** results · **{len(live)}** open now · "
               f"**{len(closed)}** closed · **{len(undated)}** with no date · "
               f"**{len(doms)}** domains\n")

    if live:
        out.append("### Open now, soonest first\n")
        out.append("| closes | what | who |")
        out.append("|---|---|---|")
        for r in sorted(live, key=lambda x: x["closes_on"])[:30]:
            t = (r.get("title") or "?")[:70].replace("|", " ")
            p = (r.get("provider") or "?")[:40].replace("|", " ")
            u = r.get("source_url") or ""
            out.append(f"| {r['closes_on']} | [{t}]({u}) | {p} |")
        out.append("")

    if closed:
        # Closed is not useless. It tells you which month a scheme reopens,
        # which is how you prepare documents in advance instead of finding
        # out three weeks late.
        out.append("### Recently closed — worth noting when they reopen\n")
        out.append("| closed | what | who |")
        out.append("|---|---|---|")
        for r in sorted(closed, key=lambda x: x["closes_on"], reverse=True)[:15]:
            t = (r.get("title") or "?")[:70].replace("|", " ")
            p = (r.get("provider") or "?")[:40].replace("|", " ")
            u = r.get("source_url") or ""
            out.append(f"| {r['closes_on']} | [{t}]({u}) | {p} |")
        out.append("")

    if undated:
        out.append("### No deadline published\n")
        for r in undated[:20]:
            t = (r.get("title") or "?")[:70]
            u = r.get("source_url") or ""
            out.append(f"- [{t}]({u}) — {(r.get('provider') or '?')[:40]}")
        if len(undated) > 20:
            out.append(f"- _...and {len(undated)-20} more_")
        out.append("")

    out.append("### Domains\n")
    out.append("| domain | hits |")
    out.append("|---|---|")
    for d, c in doms.most_common(25):
        out.append(f"| {d} | {c} |")
    out.append("")

    empty = sorted({r.get("_query") for r in rows if r.get("_query")})
    out.append(f"_{len(empty)} queries returned something. "
               f"Full results are in the artifact on this page._")
    return "\n".join(out)


def slack(rows, url):
    live, closed, undated = split_by_date(rows)
    lines = [f"*Search finished* — {len(rows)} results, {len(live)} open now, "
             f"{len(closed)} closed, {len(undated)} with no date."]
    for r in sorted(live, key=lambda x: x["closes_on"])[:10]:
        t = (r.get("title") or "?")[:70]
        lines.append(f"• <{r.get('source_url')}|{t}> — closes {r['closes_on']}")
    if len(live) > 10:
        lines.append(f"_...and {len(live)-10} more_")
    req = urllib.request.Request(
        url, data=json.dumps({"text": "\n".join(lines)}).encode(),
        headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=15)


def main():
    rows = load()
    if not rows:
        print("## Search results\n\nNo results file found. "
              "Check the log in the run artifact.")
        return

    text = markdown(rows)
    print(text)

    if "--slack" in sys.argv:
        url = os.environ.get("SLACK_WEBHOOK_URL")
        if url:
            try:
                slack(rows, url)
                print("\n_posted to Slack_", file=sys.stderr)
            except Exception as e:
                print(f"\nslack failed: {type(e).__name__}", file=sys.stderr)


if __name__ == "__main__":
    main()
