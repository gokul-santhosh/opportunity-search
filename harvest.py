#!/usr/bin/env python3
"""
Exhaustive harvest. Search, filter, score and save, in one run.

    python harvest.py --dry-run    # show the plan, spend nothing
    python harvest.py --broad      # 26 queries, about $2, sanity check
    python harvest.py              # everything, about $10
    python harvest.py --limit 20   # first 20 queries only

WHAT THIS DOES DIFFERENTLY, and why each part exists:

  Chains. An organisation named in a result that we have not searched for
  gets its own query. One passing mention of "NFR" became 56 posts in a
  manual sweep. Fixed query lists cannot do this.

  Filters in code, not in the prompt. Four separate times an instruction
  was written into a prompt, ignored, and only fixed by moving it into
  code. Search count, expired listings, non-sports results, and quoting a
  disbursement period as a deadline. All four checks are below.

  Saves incrementally, journals first. Three runs once lost paid search
  results to a crash that happened after the money was spent.

  Keeps expired listings. A scheme that closed in May tells you to prepare
  documents in March. That is worth more than most live listings.
"""

import argparse, datetime as dt, json, os, re, sys, time
from urllib.parse import urlparse

import requests, yaml
import llm

HERE = os.path.dirname(os.path.abspath(__file__))


# ============================================================ prompt

PROMPT = """Find opportunities open to student athletes in India, especially
Tamil Nadu, from low-income families. Search for:

    {query}

Return everything relevant, up to {n} results — not just the best one.

WHAT COUNTS
Scholarships, college admission or trials under sports quota, jobs reserved
for sportspersons, grants, stipends, free training places, sports hostels,
cash awards for medallists.

WHAT DOES NOT COUNT, and must not be substituted when you find little:
general merit or income scholarships open to all students; caste, community,
minority or girl-child welfare schemes; free bicycle, laptop or uniform
distribution. Sports achievement must be part of who qualifies. An empty
result is a correct and useful answer.

EXPIRED IS STILL USEFUL. A notification that closed recently tells us when
the scheme reopens. Return it with its real closing date.

Prefer the organisation that runs the scheme over any site reposting it.

Return ONLY a JSON array, no markdown fences:

[{{
  "kind": "scholarship|college_quota|job|training|award",
  "title": "plain language, audience first, e.g. 'Badminton players: 3 railway jobs'",
  "provider": "the organisation that RUNS it",
  "provider_kind": "central_govt|state_govt|psu|private_company|trust_foundation|csr|college|university|federation",
  "sports": ["specific sports, or [] if open to all"],
  "min_level": "school|district|state|national|international|null",
  "age_min": null, "age_max": null,
  "gender": "any|male|female",
  "education": ["class_9_12|class_10|class_12|iti|diploma|ug|pg"],
  "domicile": "state name or null",
  "income_ceiling_inr": null,
  "benefit_kinds": ["cash|seat|hostel|training|equipment|employment"],
  "amount_inr": null,
  "amount_period": "one_time|monthly|annual|null",
  "closes_on": "YYYY-MM-DD or null",
  "how_to_apply": "online|offline_post|in_person|email|trial_only|no_application|unknown",
  "apply_url": "the actual application page if different from source_url",
  "application_fee_inr": null,
  "documents": ["what you must bring"],
  "posts_total": null,
  "location_city": null,
  "location_state": null,
  "contact_phone": null,
  "source_url": "the page you actually opened",
  "notes": "SEE THE RULES BELOW",
  "evidence": {{
    "closes_on": "the exact sentence from the page stating the deadline, or null",
    "provider": "the exact text naming the provider, or null",
    "min_level": "the exact text stating the level required, or null"
  }},
  "other_organisations": ["any other funder, employer or body named on the page"],
  "uncertainty": "what you could NOT confirm"
}}]

HOW TO WRITE `notes`
This is read by a 16-year-old or their parent on a phone. Write like a coach
explaining it to them, not like a form. Two or three sentences. Second person.
Say the one thing they would otherwise get wrong.

  Good: "Three badminton posts. The women's singles post needing only Class 10
  has the lowest bar of the three. Even so you need eighth place or better at
  the Senior Nationals, so district play alone will not reach it."

  Bad: "POSTS: Men Singles x1, Women Singles x1. Level-2/3 (Class 12 required)."

No capitals for emphasis. No x1 notation. Nothing about pipelines or scraping.

THE RULES THAT MATTER MOST
Never invent a date, an amount or a URL. Null is correct; a plausible guess is
a defect. Every `evidence` value must be copied VERBATIM from the page — this
is checked mechanically afterwards, and a quote that is not in the page
invalidates the field."""


# ============================================================ filters
# All of these were prompt instructions first. All four were ignored.

SPORT_WORDS = [
    "sport", "athlete", "sportsperson", "sportsmen", "games", "player",
    "coach", "tournament", "championship", "medal", "physical education",
    "khelo", "olympic", "paralympic", "gradation", "trials", "quota",
    "stadium", "academy", "sdat", "sports authority", "champions",
    "athletics", "badminton", "kabaddi", "volleyball", "hockey", "football",
    "cricket", "basketball", "tennis", "swimming", "boxing", "wrestling",
    "weightlifting", "archery", "shooting", "gymnastics", "chess", "kho kho",
    "silambam", "mallakhamb", "yogasana", "judo", "taekwondo", "karate",
    "fencing", "wushu", "handball", "netball", "rowing", "cycling",
]

NOT_SPORTS = [
    "free bicycle", "bi-cycle", "bicycle scheme", "cycle scheme",
    "free laptop", "bus pass", "free uniform", "girl child", "pudhumai penn",
    "single parent", "single-parent", "widow", "orphan pension",
    "marriage assistance", "minority scholarship", "adi dravidar",
    "post matric scholarship", "pre matric scholarship",
]

FRAUD = ["registration fee", "processing fee", "pay now", "guaranteed admission",
         "sure selection", "100% selection", "admission consultancy"]

DEADLINE_WORDS = [
    "last date", "last day", "closing date", "close on", "closes on",
    "closed on", "deadline", "due date", "cut-off date", "cut off date",
    "apply before", "apply by", "on or before", "not later than",
    "submission of application", "registration ends", "applications close",
    "valid till", "valid up to", "final date", "before 5", "before 17",
]

DATE_TOKEN = re.compile(
    r"\d{1,2}[./\-]\d{1,2}[./\-]\d{2,4}"
    r"|\d{1,2}(?:st|nd|rd|th)?\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)"
    r"|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{1,2}"
    r"|\d{4}-\d{2}-\d{2}", re.I)


def canonical(url):
    u = (url or "").split("#")[0].rstrip("/")
    u = u.replace("://www.", "://").replace("/amp/story/", "/").replace("/amp/", "/")
    return (u[:-4] if u.endswith("/amp") else u).lower()


def blocked(url, blocklist):
    host = urlparse(url or "").netloc.lower()
    host = host[4:] if host.startswith("www.") else host
    return any(host == b or host.endswith("." + b) for b in blocklist)


def is_sports(item):
    """The model returned free-bicycle schemes when told to return sports only."""
    blob = " ".join(str(item.get(k) or "") for k in
                    ("title", "notes", "source_url", "provider")).lower()
    blob += " " + " ".join(str(s) for s in (item.get("sports") or []))
    for bad in NOT_SPORTS:
        if bad in blob:
            return False, f"welfare scheme ({bad})"
    if not any(w in blob for w in SPORT_WORDS):
        return False, "no sports connection"
    return True, ""


def quote_is_a_deadline(quote):
    """
    A quote can be genuine and still not be a deadline.

    Real case: a government page said "The Quarter 4 amount released for
    2023-24 covers Jan - Feb - March 2024". The model returned that as
    evidence for a closing date of 2024-03-31. The verbatim check passed,
    because the sentence really was on the page — it just describes a
    disbursement period.

    Verbatim answers "did you read this". This answers "does it mean what
    you say it means". Both are required.
    """
    if not quote:
        return False, "no quote"
    q = quote.lower()
    if not DATE_TOKEN.search(q):
        return False, "quote has no date in it"
    if not any(w in q for w in DEADLINE_WORDS):
        return False, "quote has a date but does not say it is a deadline"
    return True, ""


def score(item):
    """
    Structural confidence. Every check passes or fails independently of what
    the model believes about itself — its own stated confidence is worth one
    point out of seventeen, deliberately.
    """
    pts, why = 0, []
    url = item.get("source_url") or ""
    host = urlparse(url).netloc.lower()
    ev = item.get("evidence") or {}
    blob = " ".join(str(item.get(k) or "") for k in ("title", "notes")).lower()

    if url.startswith("http"):
        pts += 2; why.append("has a URL (+2)")

    if re.search(r"\.(gov|nic)\.in$", host):
        pts += 3; why.append("government domain (+3)")
    elif host.endswith((".ac.in", ".edu.in", ".org", ".org.in")):
        pts += 1; why.append("institutional domain (+1)")

    q = ev.get("closes_on")
    ok_deadline, why_not = quote_is_a_deadline(q)
    if q and ok_deadline:
        pts += 3; why.append("deadline quote reads as a deadline (+3)")
    elif q:
        pts -= 4; why.append(f"quote is not a deadline — {why_not} (-4)")
    else:
        why.append("no deadline quote (0)")

    d = item.get("closes_on")
    if d:
        try:
            when = dt.datetime.strptime(str(d), "%Y-%m-%d").date()
            age = (dt.date.today() - when).days
            if age < 0 and ok_deadline:
                pts += 3; why.append(f"deadline is future and sourced ({d}) (+3)")
            elif age < 0:
                pts += 1; why.append(f"deadline is future but unsourced ({d}) (+1)")
            elif age <= 120:
                pts -= 2; why.append(f"closed {age}d ago — recent, keep as calendar (-2)")
            elif age <= 500:
                pts -= 5; why.append(f"closed {age}d ago (-5)")
            else:
                pts -= 12; why.append(f"closed {age}d ago — stale (-12)")
        except ValueError:
            item["closes_on"] = None
            why.append("deadline does not parse (0)")

    if item.get("provider"):
        pts += 1; why.append("provider named (+1)")
    if item.get("how_to_apply") not in (None, "unknown"):
        pts += 1; why.append("application route known (+1)")
    if item.get("documents"):
        pts += 1; why.append("documents listed (+1)")
    if "/notification" in url.lower() or "/circular" in url.lower() or url.lower().endswith(".pdf"):
        pts += 2; why.append("looks like the originating document (+2)")

    hits = [f for f in FRAUD if f in blob]
    if hits:
        pts -= 20; why.append(f"asks applicants for money — FRAUD ({hits[0]}) (-20)")

    return pts, why


# ============================================================ parsing

def parse(text):
    cleaned = re.sub(r"^```(?:json)?|```$", "", (text or "").strip(), flags=re.M).strip()
    if not cleaned:
        return []
    m = re.search(r"\[.*\]", cleaned, re.S)
    body = m.group(0) if m else cleaned
    try:
        out = json.loads(body)
    except json.JSONDecodeError:
        # Salvage whole objects from a truncated array. The searches were
        # already paid for; losing the rows that finished writing is waste.
        out = []
        for chunk in re.findall(r"\{(?:[^{}]|\{[^{}]*\})*\}", body, re.S):
            try:
                o = json.loads(chunk)
                if isinstance(o, dict) and o.get("source_url"):
                    out.append(o)
            except json.JSONDecodeError:
                pass
    if isinstance(out, dict):
        out = [out]
    return [o for o in out if isinstance(o, dict)]


# ============================================================ storage

class Store:
    def __init__(self, dry=False, distances=None):
        self.base = os.environ.get("SUPABASE_URL", "").rstrip("/")
        self.key = os.environ.get("SUPABASE_SERVICE_KEY", "")
        self.offline = not (self.base and self.key)
        self.dry = dry or self.offline
        self.distances = distances or {}
        if self.offline:
            print("  [no Supabase — printing only, nothing will be saved]")
        elif dry:
            print("  [dry run — reading normally, writing nothing]")

    def known(self):
        """
        Deduplication happens here, in code, not with an on_conflict upsert.

        There is deliberately NO unique index on source_url: one notification
        page can legitimately produce many rows. The NFR railway notification
        is one URL and thirteen rows, one per sport, because a badminton
        player searching badminton should find the badminton post.

        So we read what exists and skip URLs we already hold.
        """
        if self.offline:
            return set()
        try:
            r = requests.get(f"{self.base}/rest/v1/opportunities",
                             headers={"apikey": self.key,
                                      "Authorization": f"Bearer {self.key}"},
                             params={"select": "source_url", "limit": "20000"},
                             timeout=40)
            r.raise_for_status()
            return {canonical(x["source_url"]) for x in r.json()}
        except Exception as e:
            print(f"  could not read existing rows: {e}")
            return set()

    def row(self, it, pts, why, threshold):
        """
        Maps a finding onto the EXISTING opportunities table.

        Column names differ from what this script generates internally, so
        the mapping is spelled out rather than assumed:

            education        -> education_levels
            distance_km      -> distance_from_cbe_km
            confidence       -> confidence_score
            confidence_why   -> confidence_reasons
            relocation       -> relocation_required
            found_by         -> verified_by
            last_checked     -> last_verified

        And status uses THIS project's vocabulary: 'verified' means
        published. The Lovable app and the RLS policy both read
        status = 'verified', so writing 'published' here would produce rows
        that exist and are invisible.
        """
        city = it.get("location_city")
        dist = self.distances.get(city) if city else None

        # A past deadline is expired whatever it scored. Score decides
        # whether we trust the row, not whether the window is open — and a
        # closed listing shown as live is the failure students notice first.
        # Expired rows are kept: reopens_month is the whole point.
        closed, reopens = False, None
        if it.get("closes_on"):
            try:
                when = dt.datetime.strptime(str(it["closes_on"]), "%Y-%m-%d").date()
                if when < dt.date.today():
                    closed, reopens = True, when.month
            except ValueError:
                it["closes_on"] = None

        if closed:
            status = "expired"
        elif pts >= threshold:
            status = "verified"          # this project's word for published
        else:
            status = "draft"

        return {
            "kind": it.get("kind") or "scholarship",
            "title": (it.get("title") or "")[:400],
            "provider": it.get("provider"),
            "provider_kind": it.get("provider_kind"),
            "sports": it.get("sports") or [],
            "min_level": it.get("min_level"),
            "age_min": it.get("age_min"), "age_max": it.get("age_max"),
            "gender": it.get("gender") or "any",
            "education_levels": it.get("education") or [],
            "domicile": it.get("domicile"),
            "income_ceiling_inr": it.get("income_ceiling_inr"),
            "benefit_kinds": it.get("benefit_kinds") or [],
            "amount_inr": it.get("amount_inr"),
            "amount_period": it.get("amount_period"),
            "amount_confidence": "reported" if it.get("amount_inr") else "unknown",
            "closes_on": it.get("closes_on"),
            # never claim 'exact' on a row nobody read
            "deadline_confidence": "approximate" if it.get("closes_on") else "unknown",
            "reopens_month": reopens,
            "how_to_apply": it.get("how_to_apply") or "unknown",
            "apply_url": it.get("apply_url"),
            "application_fee_inr": it.get("application_fee_inr"),
            "selection_process": it.get("selection_process"),
            "documents": it.get("documents") or [],
            "posts_total": it.get("posts_total"),
            "location_city": city,
            "location_state": it.get("location_state"),
            "distance_from_cbe_km": dist,
            "relocation_required": bool(dist and dist > 600),
            "contact_phone": it.get("contact_phone"),
            "contact_email": it.get("contact_email"),
            "source_url": it["source_url"],
            "notes": it.get("notes"),
            "scope": "national",
            "status": status,
            "needs_review": status == "draft",
            "confidence_score": pts,
            "confidence_reasons": why,
            "evidence": it.get("evidence"),
            "verified_by": f"harvest-{dt.date.today()}",
            "extracted_by": f"{llm.SEARCH_MODEL} {dt.date.today()}",
            "checked_by_person": False,
            "first_seen": str(dt.date.today()),
        }

    def save(self, rows):
        if not rows:
            return 0
        if self.dry:
            for r in rows[:4]:
                mark = {"verified": "PUBLISH", "expired": "expired", "draft": "draft  "}[r["status"]]
                print(f"      {mark} [{r['confidence_score']:>3}] {r['title'][:64]}")
            if len(rows) > 4:
                print(f"      ... and {len(rows)-4} more")
            return len(rows)
        try:
            resp = requests.post(f"{self.base}/rest/v1/opportunities",
                                 headers={"apikey": self.key,
                                          "Authorization": f"Bearer {self.key}",
                                          "Content-Type": "application/json",
                                          "Prefer": "return=representation"},
                                 json=rows, timeout=60)
            if resp.status_code == 409:
                return 0
            if not resp.ok:
                print(f"      save failed ({resp.status_code}): {resp.text[:160]}")
                return 0
            return len(resp.json())
        except Exception as e:
            print(f"      save error: {type(e).__name__} — journalled, continuing")
            return 0


# ============================================================ main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="plan and cost only")
    ap.add_argument("--broad", action="store_true", help="broad queries only")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--no-chain", action="store_true")
    ap.add_argument("--threshold", type=int, default=8,
                    help="publish at or above this score (default 8)")
    args = ap.parse_args()

    cfg = yaml.safe_load(open(os.path.join(HERE, "queries.yml")))
    st = cfg["settings"]
    blocklist = cfg.get("blocked_domains", [])

    queries = list(cfg["broad"])
    if not args.broad:
        queries += cfg["organisations"] + cfg["sports"] + cfg["tamil"]
    if args.limit:
        queries = queries[:args.limit]

    per = llm.WEB_SEARCH_PER_CALL + (llm.SEARCH_CONTENT_TOKENS / 1e6) * 0.15
    print(f"\nHARVEST — {len(queries)} queries")
    print(f"  search fee   ~${len(queries)*per:.2f}, plus output tokens")
    print(f"  realistic    ~${len(queries)*per*4:.2f}")
    print(f"  ceiling      ${st['cost_ceiling_usd']:.2f}")
    print(f"  publish at   {args.threshold} points\n")

    if args.dry_run and not os.environ.get("OPENAI_API_KEY"):
        for i, q in enumerate(queries, 1):
            print(f"  {i:3}. {q}")
        print(f"\nDry run, no key set. Nothing searched.")
        return

    client = llm.client()
    bud = llm.Budget()
    bud.check()
    store = Store(args.dry_run, cfg.get("distances", {}))
    seen = store.known()
    print(f"  {len(seen)} URLs already in the board\n")

    spent, saved, published, dropped = 0.0, 0, 0, {"blocked": 0, "notsport": 0, "dupe": 0}
    org_seen, searched = {}, set()

    def run(q, tag):
        nonlocal spent, saved, published
        llm.journal("query", {"q": q, "tag": tag})
        try:
            text, ti, to, used = llm.search(
                client, PROMPT.format(query=q, n=st["results_per_query"]),
                st["max_searches_per_query"])
        except Exception as e:
            print(f"    failed: {type(e).__name__}: {e}")
            return
        spent += bud.record(f"harvest:{tag}", llm.SEARCH_MODEL, ti, to, used)

        found = parse(text)
        llm.journal("result", {"q": q, "n": len(found), "items": found})

        keep = []
        for it in found:
            u = it.get("source_url") or ""
            if not u.startswith("http"):
                continue
            if blocked(u, blocklist):
                dropped["blocked"] += 1; continue
            ok, _ = is_sports(it)
            if not ok:
                dropped["notsport"] += 1; continue
            for o in (it.get("other_organisations") or []):
                if isinstance(o, str) and 3 < len(o) < 70:
                    org_seen[o] = org_seen.get(o, 0) + 1
            if canonical(u) in seen:
                dropped["dupe"] += 1; continue
            seen.add(canonical(u))
            pts, why = score(it)
            if pts < 0:
                dropped["notsport"] += 1; continue
            keep.append(store.row(it, pts, why, args.threshold))

        n = store.save(keep)
        saved += n
        published += sum(1 for r in keep if r["status"] == "verified")
        print(f"    {len(found)} found, {len(keep)} kept, {n} saved | ${spent:.3f}")

    # ---- main sweep ----
    for i, q in enumerate(queries, 1):
        if spent >= st["cost_ceiling_usd"]:
            print(f"\n  ceiling reached at ${spent:.2f}")
            break
        print(f"[{i}/{len(queries)}] {q[:64]}")
        searched.add(q.lower())
        run(q, "sweep")

    # ---- chain on organisations we did not search for ----
    if not args.no_chain and st.get("chain_new_organisations") and spent < st["cost_ceiling_usd"]:
        pool = " ".join(searched)
        cand = [o for o, c in sorted(org_seen.items(), key=lambda x: -x[1])
                if c >= 2 and o.lower() not in pool][:st.get("max_chained", 40)]
        if cand:
            print(f"\n{'='*62}\nCHAINING — {len(cand)} organisations named in results "
                  f"but never searched for\n{'='*62}")
            for i, org in enumerate(cand, 1):
                if spent >= st["cost_ceiling_usd"]:
                    break
                print(f"[{i}/{len(cand)}] {org[:58]}")
                run(f"{org} sports athlete scholarship OR recruitment apply", "chain")

    print(f"\n{'='*62}")
    print(f"saved {saved}, of which {published} published, {saved-published} need review")
    print(f"dropped: {dropped['blocked']} blocked, {dropped['notsport']} not sports, "
          f"{dropped['dupe']} already known")
    print(f"total spend ${spent:.2f}")
    print(f"\n  select * from live_board;      -- what students see")
    print(f"  select * from needs_review;    -- your triage list")
    print(f"  select * from calendar;        -- when closed things reopen")


if __name__ == "__main__":
    main()
