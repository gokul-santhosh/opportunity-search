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

Return everything relevant you find, up to 10 results. Include expired
listings — an old notification tells us when the scheme reopens, which is
often worth more than a live listing.

Return ONLY a JSON array, no markdown fences:

[{{
  "kind": "scholarship | college_quota | job | training | award",
  "title": "",
  "provider": "the organisation that runs it",
  "sport": "the sport, or 'any'",
  "min_level": "school | district | state | national | international | null",

  "closes_on": "YYYY-MM-DD or null",
  "deadline_type": "one_off | annual | rolling | relative_to_event | not_found",
  "deadline_evidence": "the exact sentence stating the deadline, word for word, or null",
  "reopens_month": null,
  "recurrence": "what the page says about how often this runs, or null",

  "published_on": "YYYY-MM-DD the page was first published, or null",
  "last_updated_on": "YYYY-MM-DD the page was last updated, or null",

  "amount": "what you get, in plain words",
  "application_fee_inr": null,
  "how_to_apply": "online | offline_post | in_person | email | trial_only | no_application | unknown",
  "apply_url": "the application page if different from source_url",
  "documents": ["what you must bring or upload"],

  "location_city": null,
  "location_state": null,
  "contact_phone": "phone number shown on the page, or null",
  "contact_email": null,

  "source_url": "the page you opened",
  "summary": "two sentences on what this is and who it is for"
}}]


DATES

Only fill "closes_on" if you have seen the actual closing date stated on a
page. If the search result does not show one, search again or open the
notification to find it. If it still is not there, use null.

Do not use 31 December. Do not use the end of the academic year. Do not
infer a date from the scheme name. A null is correct and useful; an
invented date sends someone to a closed application.


DEADLINE_TYPE tells us WHY there is no date, which matters as much as the
date itself:

  one_off             a single closing date, stated on the page
  annual              recurs yearly. give reopens_month if the page says
  rolling             open continuously, no deadline at all
  relative_to_event   tied to something else rather than a calendar date,
                      for example "counselling on the 4th day after 12th
                      results". Use this rather than guessing a date.
  not_found           a deadline probably exists but you could not find it

The difference between "rolling" and "not_found" is the difference between
a correct answer and a gap. Do not use one for the other.


DEADLINE_EVIDENCE must be copied VERBATIM from the page — the actual
sentence, not a paraphrase. This is checked mechanically afterwards. If you
cannot find such a sentence, use null for deadline_evidence AND for
closes_on.


PUBLISHED_ON and LAST_UPDATED_ON: only fill these if the page itself shows
a date — a byline, a "last updated" line, a notification date, or a date
printed on a PDF. Do not use the date of the event described, do not use
the academic year, do not guess from the URL. Null is correct when the page
shows nothing.

If only one date is shown and you cannot tell which it is, put it in
published_on and leave last_updated_on null.


WHAT COUNTS

Scholarships, college admission or trials under sports quota, jobs reserved
for sportspersons, grants, stipends, free training places, sports hostels,
cash awards for medallists.

WHAT DOES NOT COUNT, and must not be substituted when you find little:
general merit or income scholarships open to all students; caste, community,
minority or girl-child welfare schemes; free bicycle, laptop or uniform
distribution. Sports achievement must be part of who qualifies. An empty
result is a correct and useful answer.

Prefer the organisation that runs the scheme over any site reposting it.

Never invent a URL, a phone number, an amount or a date. Null is correct
where you do not know."""


MODEL = "gpt-5.6-luna"     # reasoning model. see REASONING below.
# MODEL = "gpt-5.6-terra"  # stronger, roughly 10x the token cost
# MODEL = "gpt-4o-mini"    # cheapest, but it invents deadlines. see notes.

# How hard the model works before answering: none, low, medium, high, xhigh.
# This is what makes the difference. At "none" the model does one search and
# writes from the snippet — which is why it used to return 2026-12-31 for
# everything, having no real date to report. At "medium" it searches again,
# opens the notification, and finds the actual date or honestly says there
# is none.
REASONING = "medium"


# ============================================================

def search(client, query):
    """One query. Returns (list of results, cost estimate)."""
    r = client.responses.create(
        model=MODEL,
        tools=[{"type": "web_search"}],
        reasoning={"effort": REASONING},
        max_output_tokens=8000,
        input=ASK.format(query=query),
    )

    text = getattr(r, "output_text", "") or ""

    # count searches actually performed, for the cost estimate
    used = sum(1 for item in getattr(r, "output", [])
               if getattr(item, "type", "") == "web_search_call") or 1

    # $10 per 1,000 searches, plus the page content pulled in, billed at the
    # model's input rate. Note output_tokens includes REASONING tokens, which
    # bill at the output rate — that is where the cost of thinking lands.
    IN_RATE, OUT_RATE = 0.20, 1.20        # gpt-5.6-luna, after 30 July 2026
    cost = used * 0.010 + used * (8000 / 1e6) * IN_RATE
    u = getattr(r, "usage", None)
    if u:
        cost += (getattr(u, "input_tokens", 0) / 1e6) * IN_RATE
        cost += (getattr(u, "output_tokens", 0) / 1e6) * OUT_RATE

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

        dated = sum(1 for r in results if r.get("closes_on"))
        rolling = sum(1 for r in results if r.get("deadline_type") == "rolling")
        notfound = sum(1 for r in results if r.get("deadline_type") == "not_found")
        print(f"    {len(results)} results | {dated} dated, {rolling} rolling, "
              f"{notfound} date not found | ${total:.3f} so far")
        for r in results[:3]:
            print(f"      {(r.get('title') or '?')[:62]}")
            print(f"        {r.get('closes_on') or '(no date)':12} "
                  f"{(r.get('deadline_type') or '?'):18} "
                  f"{(r.get('source_url') or '')[:44]}")
        if len(results) > 3:
            print(f"      ... and {len(results)-3} more")
        print()

    import collections
    types = collections.Counter(r.get("deadline_type") or "(missing)"
                                for r in everything)
    fabricated = sum(1 for r in everything if r.get("closes_on") == "2026-12-31")

    print(f"{'='*70}")
    print(f"{len(everything)} results from {len(queries)} queries")
    print(f"${total:.2f} spent")
    print()
    print("deadline types:")
    for t, c in types.most_common():
        print(f"  {t:20} {c}")
    print()
    # The old model filled every unknown deadline with 31 December. If this
    # is above zero, the model is guessing again and the prompt needs work.
    print(f"suspicious 2026-12-31 dates: {fabricated}"
          f"{'   <-- the model is guessing again' if fabricated else '   (good)'}")
    print(f"\n  {json_path}")
    print(f"  {text_path}")


if __name__ == "__main__":
    main()
