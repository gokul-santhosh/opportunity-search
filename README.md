# Opportunity Board

Finds sports scholarships, college sports-quota admissions, and jobs open to
sportspersons, for student athletes in Tamil Nadu.

One command does the whole thing: search, filter, score, save.

```
websites  ──▶  harvest.py  ──▶  Supabase  ──▶  your phone app
              search · filter · score
```

---

## Setup, about 15 minutes

### 1. Supabase — your existing project, unchanged

There is no schema file. This writes to the `opportunities` table you already
have, using the column names it already uses. Nothing to migrate, and your
Lovable app keeps working exactly as it does now.

Two things worth knowing about how it fits:

**`status = 'verified'` means published.** That is this project's vocabulary,
and both the Lovable app and the row-level security policy read it. The
harvest writes `verified` for rows that clear the threshold, `draft` for those
that do not, and `expired` for anything whose deadline has passed.

**There is deliberately no unique index on `source_url`.** One notification
page can legitimately produce many rows — the railway notification is one URL
and thirteen rows, one per sport, so a badminton player searching badminton
finds the badminton post. Deduplication happens in code instead: the harvest
reads the URLs already on the board and skips them.

Project Settings → API, and take two keys. **Keep them apart.**

| Key | Goes to | Can do |
|---|---|---|
| **anon** | your phone app only | read published rows |
| **service_role** | your terminal only | everything, bypasses all security |

### 2. Your machine

```bash
python3 -m venv venv
source venv/bin/activate
pip install openai requests PyYAML

export OPENAI_API_KEY="sk-proj-..."
export SUPABASE_URL="https://YOURPROJECT.supabase.co"
export SUPABASE_SERVICE_KEY="eyJhbG..."
```

These last only for that terminal window. That is deliberate — the keys never
end up saved in a file.

### 3. Set a real spending limit

platform.openai.com → Settings → Limits → budget limit 25, alert at 15.

Better still, prepay $25 with auto-recharge **off**. The ceilings in `llm.py`
are advisory; a bug in our own code can bypass them. Only the dashboard limit
cannot be.

---

## Running it

```bash
python harvest.py --dry-run     # see all 148 queries, spend nothing
python harvest.py --broad       # 26 queries, about $2, sanity check
python harvest.py               # everything, about $7
```

Start with `--broad`. If the results look sensible, run the full sweep.

Expect 30 to 50 minutes and 100 to 300 findings.

---

## What comes out

```sql
-- what a student sees, soonest and nearest first
select title, provider, closes_on, distance_from_cbe_km, how_to_apply
from opportunities where status = 'verified'
order by closes_on nulls last, distance_from_cbe_km nulls last;

-- scored below the line, your triage list
select confidence_score, title, provider, confidence_reasons
from opportunities where status = 'draft'
order by confidence_score desc;

-- closed, and which month each reopens
select reopens_month, title, provider from opportunities
where status = 'expired' and reopens_month is not null
order by reopens_month;

-- what it cost
select * from spend_summary;
```

That third query is the most underrated. A scheme that closed in May tells
you to prepare documents in March, and that is worth more than most live
listings — you stop finding out about things three weeks late.

---

## How it decides what to publish

Every finding is scored on checks that pass or fail independently of what the
model claims about itself.

| Check | Points |
|---|---|
| Government domain | +3 |
| Deadline quote both verbatim AND reads like a deadline | +3 |
| Deadline is in the future and sourced | +3 |
| Looks like the originating document | +2 |
| Provider named, route known, documents listed | +1 each |
| **Quote is real but is not a deadline** | **−4** |
| Closed 120 to 500 days ago | −5 |
| Closed over 500 days ago | −12 |
| **Page asks applicants for money** | **−20** |

Eight or more publishes. Below that it waits in `needs_review`. A past deadline
is marked `expired` whatever it scored — score decides whether we trust the
row, not whether the window is open.

### The check worth understanding

A government page once said *"The Quarter 4 amount released for 2023-24 covers
Jan - Feb - March 2024"*. The model returned that as evidence for a closing
date of 31 March 2024. The verbatim check **passed**, because the sentence
really was on the page — it just describes a disbursement period.

Verifying that a quote was copied honestly is not the same as verifying it
means what the model says. `quote_is_a_deadline()` now requires both a date
and deadline language. That case now scores −8 instead of publishing.

---

## The pattern behind most of this code

**A prompt is a request. Code is a guarantee.**

Four times, an instruction written into a prompt was ignored, and only moving
it into code fixed it:

| Asked for | Got | Now enforced by |
|---|---|---|
| Run 9 searches | 1 search | one API call per query |
| Skip closed items | deadlines from 2023 | date check in `score()` |
| Sports only | free-bicycle welfare schemes | `is_sports()` |
| Quote must be a deadline | a disbursement period | `quote_is_a_deadline()` |

If you find yourself fixing a reliability problem by rewording the prompt,
write the check instead.

---

## Tuning it

**`queries.yml`** is where you will spend your time.

Add an organisation whenever you learn one exists. Named-organisation queries
outperform category queries by a wide margin — "GoSports Foundation athlete
support" beats "sports foundation India", because organisations are stable and
well indexed while categories are answered with aggregator spam.

Add a domain to `blocked_domains` whenever something useless keeps appearing.
Grow that list from what you actually see, never from guesses.

`--threshold 6` publishes more and reviews less. `--threshold 12` is the
reverse. Eight is a starting point, not a finding.

---

## Two things to keep straight

**No student data in this repo, ever.** Most of these students are minors. The
`opportunities` table is public by design. Student records belong somewhere
separate and access-controlled, with written parental consent.

**Rows publish without a person reading them.** They land as
`status = 'verified'`, which is what your app shows. That is the right trade
while nobody is using the app — a review queue nobody works means an empty board.
The day real students start opening it, raise `--threshold` and work
`needs_review` properly. `checked_by_person` stays false until a human opens
the page, and your app should show that rather than a green tick.

---

## Files

| | |
|---|---|
| `harvest.py` | Search, filter, score, save. The whole thing. |
| `queries.yml` | 148 queries, blocked domains, distances |
| `llm.py` | All OpenAI calls, prices, budget ceiling |
| `.journal/` | Every result written before any save. Your receipt. |

---

## Raw mode — everything, unfiltered

Before deciding what to filter, look at what actually comes back.

```bash
python raw.py --dry-run        # the plan, free
python raw.py --broad          # 26 queries, ~$2
python raw.py                  # all 148, ~$7
python raw.py --slack          # post digests to Slack as it runs
```

**What raw mode does not do:** no deduplication, no domain blocking, no sports
filter, no scoring gate. Blogs, aggregators, news, repeats, near-misses — all
of it comes through.

**One thing it does keep, and it is not a filter:** raw results are never
written to `opportunities`. An unfiltered blog post landing as `verified`
would be in your app tonight. Everything goes to local files instead.

Each result carries `_flags` recording what the filters *would* have done:

```json
"_flags": {
  "domain": "sarkariresult.com",
  "would_block": true,
  "would_drop_notsport": false,
  "duplicate_in_run": false,
  "already_on_board": false,
  "would_score": 2,
  "would_publish": false
}
```

That is the point of the exercise. Decide the blocklist from the domains that
actually turn up, and the threshold from the scores real results earn, rather
than from a guess about what they will be.

### Output

```
raw/harvest-2026-08-20-1530.jsonl    every result, one per line
raw/harvest-2026-08-20-1530.csv      the same, openable in a spreadsheet
raw/summary-2026-08-20-1530.md       domains, empty queries, what got flagged
```

The JSONL is written after every query, so a crash costs you nothing that has
already been paid for.

---

## Slack

Get an incoming webhook:

1. api.slack.com/apps → **Create New App** → From scratch
2. Name it, pick your workspace
3. **Incoming Webhooks** → toggle on → **Add New Webhook to Workspace**
4. Choose the channel, copy the URL

Then:

```bash
export SLACK_WEBHOOK_URL="https://hooks.slack.com/services/T00/B00/xxxx"
python raw.py --broad --slack
```

You get one digest per query, up to eight links each, plus a start and finish
message. Not one message per result — 148 queries at ten results each would be
1,500 messages, which is not a report.

The webhook is a secret. It is in `.gitignore` via `.env`, and anyone holding
it can post to your channel.

---

## Running it from GitHub instead of your terminal

No Python install, no virtual environment, no folder versions to confuse.

### Setup, once

1. New repository on github.com. Public is fine — no keys live in the code.
2. Upload these files. **Press Cmd+Shift+. in Finder first** so the hidden
   `.github` folder is visible and gets included. If the Actions tab is empty
   afterwards, that is why.
3. Settings → Secrets and variables → Actions → New repository secret:

   | Name | Value |
   |---|---|
   | `OPENAI_API_KEY` | your key |
   | `SLACK_WEBHOOK_URL` | optional |

### Running it

Actions tab → **Search** → **Run workflow**. You get three boxes:

- **query** — type one and it runs just that. Leave blank to use the list in
  `search.py`.
- **how many** — run the first N built-in queries. Blank runs all of them.
- **model** — `gpt-4o-mini` or `gpt-4o`.

Press Run. Come back in a few minutes.

### What you get

**On the run page**, without downloading anything: what is open now sorted by
deadline, what closed recently and when it reopens, what had no date, and
which domains turned up.

**As a downloadable artifact**: the full JSON and text, kept 30 days.

**In Slack**, if you set the webhook: the ten soonest deadlines.

### Trying one query

The fastest way to test a query before adding it to the file. Type it into the
query box, run, read the summary. About four cents.

### Editing the query list

Open `search.py` on GitHub, click the pencil, edit `QUERIES`, commit. The next
run uses it. No local checkout needed.

### Note on scheduling

The `schedule:` block in the workflow is commented out. Uncomment it for a
weekly run. GitHub disables scheduled workflows on public repositories after
60 days without repository activity, and any commit resets that clock — so a
repo you never touch will quietly stop running.
