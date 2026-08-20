#!/usr/bin/env python3
"""
Every OpenAI call goes through here. One place for models, prices and the
budget ceiling.

THREE LAYERS OF PROTECTION, and only the third is real:

  1. LIMITS below      soft. Refuses to make the call.
  2. Per-run caps      soft. A bug in our own code bypasses both.
  3. OpenAI dashboard  the only one enforced outside our own code.

Set the dashboard limit. platform.openai.com → Settings → Limits.
Better still, prepay a balance with auto-recharge off.
"""

import datetime as dt
import json
import os
import sys
import time

import requests

# ============================================================
# BUDGET — edit these
# ============================================================

LIMITS = {
    "daily_usd":   20.00,    # a full harvest fits inside this
    "monthly_usd": 50.00,
}

# ============================================================
# Models
# ============================================================

SEARCH_MODEL  = "gpt-4o-mini"   # search runs on the cheap model on purpose
EXTRACT_MODEL = "gpt-4o-mini"   # switch to gpt-4o if extraction proves sloppy
TRIAGE_MODEL  = "gpt-4o-mini"

# USD per 1M tokens. VERIFY at platform.openai.com/pricing — every cost
# figure this file reports is only as good as this table.
PRICES = {
    "gpt-4o-mini":  {"in": 0.15, "out": 0.60},
    "gpt-4o":       {"in": 2.50, "out": 10.00},
    "gpt-4.1-mini": {"in": 0.40, "out": 1.60},
}

WEB_SEARCH_PER_CALL   = 0.010    # $10 per 1,000 calls
SEARCH_CONTENT_TOKENS = 8000     # billed ON TOP of the per-call fee, and the
                                 # thing that surprises people on the invoice

JOURNAL = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".journal")


def cost_of(model, tok_in, tok_out, searches=0):
    p = PRICES.get(model, PRICES["gpt-4o"])
    c = (tok_in / 1e6) * p["in"] + (tok_out / 1e6) * p["out"]
    if searches:
        c += searches * WEB_SEARCH_PER_CALL
        c += searches * (SEARCH_CONTENT_TOKENS / 1e6) * p["in"]
    return c


def journal(tag, payload):
    """
    Write BEFORE any network call. Three runs once lost paid search results
    to a crash that happened after the money was spent but before the save.
    This is the receipt.
    """
    os.makedirs(JOURNAL, exist_ok=True)
    path = os.path.join(JOURNAL, f"{dt.date.today()}.jsonl")
    try:
        with open(path, "a") as f:
            f.write(json.dumps({"at": dt.datetime.now().isoformat(),
                                "tag": tag, "data": payload}, default=str) + "\n")
    except Exception:
        pass    # a journal failure must never stop the run


# ============================================================ ledger

class Budget:
    def __init__(self):
        self.base = os.environ.get("SUPABASE_URL", "").rstrip("/")
        self.key = os.environ.get("SUPABASE_SERVICE_KEY", "")
        self.off = not (self.base and self.key)
        self.unrecorded = 0.0
        if self.off:
            print("  [no Supabase — spend will not persist between runs]")

    def _sum(self, since):
        if self.off:
            return 0.0
        try:
            r = requests.get(f"{self.base}/rest/v1/api_spend",
                             headers={"apikey": self.key,
                                      "Authorization": f"Bearer {self.key}"},
                             params={"select": "cost_usd",
                                     "spent_on": f"gte.{since}", "limit": "20000"},
                             timeout=20)
            r.raise_for_status()
            return sum(float(x["cost_usd"] or 0) for x in r.json())
        except Exception:
            return 0.0

    def check(self):
        today = dt.date.today()
        day = self._sum(today.isoformat()) + self.unrecorded
        month = self._sum(today.replace(day=1).isoformat()) + self.unrecorded
        print(f"  spend today ${day:.3f}/{LIMITS['daily_usd']:.2f}"
              f" | month ${month:.2f}/{LIMITS['monthly_usd']:.2f}")
        if month >= LIMITS["monthly_usd"]:
            sys.exit(f"MONTHLY CEILING REACHED (${month:.2f}). Raise it in llm.py "
                     f"or wait for next month.")
        if day >= LIMITS["daily_usd"]:
            sys.exit(f"DAILY CEILING REACHED (${day:.3f}).")
        return {"day": day, "month": month, "day_left": LIMITS["daily_usd"] - day}

    def record(self, stage, model, tok_in, tok_out, searches=0):
        """
        Never raises. An unguarded version of this call crashed three runs
        when Supabase timed out mid-loop.
        """
        c = cost_of(model, tok_in, tok_out, searches)
        if self.off:
            self.unrecorded += c
            return c
        row = {"spent_on": dt.date.today().isoformat(), "stage": stage,
               "model": model, "tokens_in": tok_in, "tokens_out": tok_out,
               "searches": searches, "cost_usd": round(c, 6)}
        for attempt, wait in enumerate((2, 8, 20)):
            try:
                r = requests.post(f"{self.base}/rest/v1/api_spend",
                                  headers={"apikey": self.key,
                                           "Authorization": f"Bearer {self.key}",
                                           "Content-Type": "application/json",
                                           "Prefer": "return=minimal"},
                                  json=[row], timeout=20)
                if r.ok:
                    return c
            except Exception:
                pass
            if attempt < 2:
                time.sleep(wait)
        # Could not record. Keep a running total so a ledger outage cannot
        # silently disable the ceiling.
        self.unrecorded += c
        print(f"    WARNING: spend not recorded (${self.unrecorded:.3f} untracked)")
        if self.unrecorded > LIMITS["daily_usd"] * 0.25:
            sys.exit("Too much unrecorded spend. Stopping deliberately — a "
                     "ceiling you cannot measure is not a ceiling.")
        return c


# ============================================================ client

def client():
    if not os.environ.get("OPENAI_API_KEY"):
        sys.exit("Set OPENAI_API_KEY first.")
    from openai import OpenAI
    return OpenAI()


def chat(c, model, prompt, max_tokens=3000, json_mode=False):
    """Plain completion. Returns (text, tokens_in, tokens_out)."""
    kw = {"response_format": {"type": "json_object"}} if json_mode else {}
    r = c.chat.completions.create(
        model=model, max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}], **kw)
    u = r.usage
    return r.choices[0].message.content, u.prompt_tokens, u.completion_tokens


def search(c, prompt, max_searches=3):
    """
    Web search through the Responses API.

    Deliberately not gpt-4o-search-preview: the dedicated search models cost
    $25-30 per thousand queries against $10 for this tool.

    No domain filters here — gpt-4o-mini rejects the `filters` parameter
    outright. Blocking happens in code after results come back, which works
    on any model and does not depend on the API honouring it.

    Returns (text, tokens_in, tokens_out, searches_used).
    """
    r = c.responses.create(
        model=SEARCH_MODEL,
        tools=[{"type": "web_search"}],
        max_output_tokens=6000,
        input=prompt + f"\n\nUse at most {max_searches} web searches.",
    )
    text = getattr(r, "output_text", "") or ""
    used = sum(1 for item in getattr(r, "output", [])
               if getattr(item, "type", "") == "web_search_call")
    u = r.usage
    return (text,
            getattr(u, "input_tokens", 0),
            getattr(u, "output_tokens", 0),
            used or 1)
