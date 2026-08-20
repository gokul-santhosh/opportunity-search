#!/usr/bin/env python3
"""
Just the search. Nothing else.

No database, no scoring, no filtering, no deduplication, no classes.
Loops your queries, calls OpenAI, writes what comes back to a file.

    pip install openai
    export OPENAI_API_KEY="sk-proj-..."

    python search.py                 # all queries below
    python search.py "one query"     # just that one
    python search.py --n 3           # first 3 queries only

Writes results-YYYY-MM-DD-HHMM.json and .txt next to this file.
"""

import json
import os
import sys
import datetime as dt
from openai import OpenAI


# ============================================================
# EDIT THIS. One query per line.
# ============================================================

QUERIES = [
    "sports scholarship India last date apply",
    "sports scholarship Tamil Nadu last date",
    "sports quota recruitment notification last date",
    "sports quota admission trials last date",
    "sports hostel admission India selection trials",
    "athlete financial assistance scheme India apply",
    "Khelo India scholarship notification apply",
    "Sports Authority of India scholarship last date",
    "SDAT Tamil Nadu scheme sportspersons apply",
    "railway sports quota recruitment notification",
    "bank sports quota recruitment sportspersons",
    "GoSports Foundation athlete support apply",
    "Reliance Foundation scholarship last date",
    "Tata Trusts sports grant athlete",
    "badminton scholarship India apply",
    "kabaddi sports quota recruitment India",
    "athletics scholarship India apply last date",
    "para athlete support scheme India",
]


# ============================================================
# What to ask for. Edit freely.
# ============================================================

ASK = """Search the web for: {query}

This is for student athletes in India, especially Tamil Nadu, from
low-income families.

Return everything relevant you find, up to 10 results. Include expired
listings — an old notification tells us when the scheme reopens.

Return ONLY a JSON array, no markdown fences:

[{{
  "title": "",
  "provider": "the organisation running it",
  "kind": "scholarship | college_quota | job | training | award",
  "sport": "the sport, or 'any'",
  "closes_on": "YYYY-MM-DD or null",
  "min_level": "school|district|state|national|international|null",
  "amount": "what you get, in plain words",
  "how_to_apply": "online | by post | in person | email | trials only | unknown",
  "source_url": "the page you opened",
  "summary": "two sentences"
}}]

Never invent a URL or a date. Null is correct where you do not know."""


MODEL = "gpt-4o-mini"      # about 1.1 cents per query
# MODEL = "gpt-4o"         # better reading, roughly 5x the cost


# ============================================================

def search(client, query):
    """One query. Returns (list of results, cost estimate)."""
    r = client.responses.create(
        model=MODEL,
        tools=[{"type": "web_search"}],
        max_output_tokens=6000,
        input=ASK.format(query=query),
    )

    text = getattr(r, "output_text", "") or ""

    # count searches actually performed, for the cost estimate
    used = sum(1 for item in getattr(r, "output", [])
               if getattr(item, "type", "") == "web_search_call") or 1

    # $10 per 1,000 searches, plus 8,000 tokens of search content per call
    # billed at the model's input rate — that second part is the one that
    # surprises people on the invoice
    cost = used * 0.010 + used * (8000 / 1e6) * 0.15
    u = getattr(r, "usage", None)
    if u:
        cost += (getattr(u, "input_tokens", 0) / 1e6) * 0.15
        cost += (getattr(u, "output_tokens", 0) / 1e6) * 0.60

    # strip markdown fences if the model added them despite being asked not to
    clean = text.strip()
    if clean.startswith("```"):
        clean = clean.split("```")[1]
        if clean.startswith("json"):
            clean = clean[4:]
    clean = clean.strip()

    try:
        out = json.loads(clean)
    except json.JSONDecodeError:
        # find the array inside whatever came back
        start, end = clean.find("["), clean.rfind("]")
        if start >= 0 and end > start:
            try:
                out = json.loads(clean[start:end + 1])
            except json.JSONDecodeError:
                print(f"    could not read the response, saving raw text")
                return [{"_unparsed": text[:2000], "source_url": None}], cost
        else:
            return [{"_unparsed": text[:2000], "source_url": None}], cost

    if isinstance(out, dict):
        out = [out]
    return [o for o in out if isinstance(o, dict)], cost


def main():
    if not os.environ.get("OPENAI_API_KEY"):
        sys.exit("Set OPENAI_API_KEY first:\n"
                 '  export OPENAI_API_KEY="sk-proj-..."')

    args = [a for a in sys.argv[1:]]
    queries = QUERIES
    if args and args[0] == "--n":
        queries = QUERIES[:int(args[1])]
    elif args:
        queries = [" ".join(args)]

    client = OpenAI()
    stamp = dt.datetime.now().strftime("%Y-%m-%d-%H%M")
    here = os.path.dirname(os.path.abspath(__file__))
    json_path = os.path.join(here, f"results-{stamp}.json")
    text_path = os.path.join(here, f"results-{stamp}.txt")

    print(f"{len(queries)} queries, {MODEL}, roughly "
          f"${len(queries) * 0.045:.2f}\n")

    everything, total = [], 0.0

    for i, q in enumerate(queries, 1):
        print(f"[{i}/{len(queries)}] {q}")
        try:
            results, cost = search(client, q)
        except Exception as e:
            print(f"    failed: {type(e).__name__}: {e}\n")
            continue

        total += cost
        for r in results:
            r["_query"] = q
        everything.extend(results)

        # write after every query, so a crash costs nothing already paid for
        with open(json_path, "w") as f:
            json.dump(everything, f, indent=2, default=str)

        with open(text_path, "a") as f:
            f.write(f"\n{'='*70}\n{q}\n{'='*70}\n")
            for r in results:
                f.write(f"\n{r.get('title','?')}\n"
                        f"  {r.get('provider','?')}\n"
                        f"  closes: {r.get('closes_on') or 'not stated'}\n"
                        f"  {r.get('source_url','?')}\n"
                        f"  {r.get('summary','')}\n")

        print(f"    {len(results)} results | ${total:.3f} so far")
        for r in results[:3]:
            print(f"      {(r.get('title') or '?')[:60]}")
            print(f"        {r.get('closes_on') or 'no date':12} "
                  f"{(r.get('source_url') or '')[:56]}")
        if len(results) > 3:
            print(f"      ... and {len(results)-3} more")
        print()

    print(f"{'='*70}")
    print(f"{len(everything)} results from {len(queries)} queries")
    print(f"${total:.2f} spent")
    print(f"\n  {json_path}")
    print(f"  {text_path}")


if __name__ == "__main__":
    main()
