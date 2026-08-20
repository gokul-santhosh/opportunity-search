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
    # scholarships and grants
    "sports scholarship India last date apply",
    "sports scholarship Tamil Nadu last date",
    "sports scholarship opportunities students apply",
    "athlete scholarship India applications open",
    "sports grant athletes India apply",
    "sports sponsorship athletes India apply",
    "athlete financial assistance scheme India apply",
    "sports stipend monthly athletes India",
    "cash award medal winners sportspersons scheme",
    "sports equipment kit support athletes India",
    "tournament travel expenses athletes India support",

    # college and admission
    "sports quota admission last date",
    "sports quota admission trials notification",
    "sports quota seat college India apply",
    "sports quota fee concession college",
    "sports quota fee waiver university India",
    "sports quota engineering admission Tamil Nadu",
    "sports quota MBBS admission India",
    "college sports trials selection notification India",

    # jobs
    "sports quota recruitment notification last date",
    "sports quota job vacancy India apply online",
    "sportspersons recruitment government India notification",
    "sports quota bank recruitment last date",
    "sports quota railway recruitment notification",
    "sports quota police army recruitment India",
    "sports quota PSU vacancy sportspersons",

    # training and residential
    "sports hostel admission India selection trials",
    "free sports academy admission India trials",
    "residential sports school admission India",
    "sports coaching centre free admission India",

    # the words the documents themselves use
    "meritorious sportspersons scheme India apply",
    "eminent sportspersons recruitment notification",
    "gradation certificate sportspersons application",
    "sports quota reservation government jobs India",

    # who else funds this
    "CSR sports programme athletes India apply",
    "sports trust foundation scholarship India apply",
    "corporate sports sponsorship young athletes India",
    "state sports council scheme athletes apply",

    # para and school
    "para athlete support scheme India apply",
    "para sports scholarship India applications",
    "school sports scholarship India apply",
    "junior athlete support programme India",

    # when things reopen
    "sports scholarship India applications open next",
    "sports quota recruitment upcoming notification India",
]


# ============================================================
# What to ask for. Edit freely.
# ============================================================

ASK = """Search the web for: {query}

This is for student athletes in India, especially Tamil Nadu, from
low-income families.

Return everything relevant you find, up to 25 results. Include expired
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

DATES: only fill in "closes_on" if the page states an actual closing date.
If it does not, use null. Do not use 31 December, do not use the end of the
academic year, do not infer a date from the scheme name. A null date is
correct and useful; an invented one sends someone to a closed application.

If the opportunity has no deadline because it runs continuously, or because
people are selected rather than applying, set "closes_on" to null and say so
in the summary.

Never invent a URL either."""


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
