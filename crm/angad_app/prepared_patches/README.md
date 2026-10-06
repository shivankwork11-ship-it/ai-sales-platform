# Prepared planner patches — NOT applied, NOT deployed
*Prepared by CI Team — citeam.claudemax@gmail.com · Built with Claude (Anthropic), 2026-08-07.
These are proposals against the local Bitbucket copy at
`USage/Angad/BItbucket/0708/.../agent/src/`. Nothing here has been applied to any
repo or deployed anywhere — per standing instruction, patches are prepared only.*

## The evidence (from the local analytics cache, recomputed 2026-08-07)
- **157 of 176 bookings (89%) never got a reminder scheduled.** The planner
  (`sitevisit/planner.ts`) only schedules the 12h/1h reminders when it has a
  parsed `visitAt`; `schedule-on-presite.ts` silently schedules **nothing**
  when `parseVisitDateTime` returns null.
- Booked→attended is 24% (AI) vs 32% (human). Bottom-of-funnel is the leak.
- The **heuristic fallback classifier** (`crm/classifier.ts`,
  `heuristicFallback`) can label a call `PRE_SITE` while returning
  `site_visit_requested: null` — those bookings can NEVER get a reminder.
- `preferred_callback_time` is extracted and put on the webhook, but nothing in
  the agent schedules from it — customers who say "call me tomorrow morning"
  are only called again if the upstream CRM decides to.
- Human-corpus mining (RADHIKA_VNEXT_PERSONA.md §3): winning human agents pin
  day AND time ("Saturday or Sunday?" → "first half or second half?"), place a
  day-before confirmation call, and honour customer-named callback slots
  verbatim. The patches make the machine keep the same promises.

## Patch order (apply 01 first; each stands alone)

| # | File touched | What it does |
|---|---|---|
| 01 | `sitevisit/planner.ts` | `parseVisitDateTime` accepts the date/slot shapes the classifier actually emits (DD-MM-YYYY, DD/MM/YYYY, 24h times, "morning/afternoon/evening", bare hours, ranges). Fewer nulls → more reminders, no behaviour change when already parseable. |
| 02 | `sitevisit/schedule-on-presite.ts` | **The 89% fix.** When `visitAt` is still null on a PRE_SITE call, schedule ONE next-morning (10:00 IST) reminder labelled `visit time unknown — confirm details` instead of silently nothing, and stamp `metadata.visit_time_unparsed = true` so ops can see the gap shrinking. |
| 03 | `sitevisit/planner.ts` (+ new call site) | Planner extensions, each mirroring a human follow-up promise: honour `preferred_callback_time` on AUTO_CALLBACK; day-before confirmation call when the visit is >24h out; no-show recovery next morning at 11:00; booked-then-cold rescue at +48h if the visit date passed with no completed visit. |

## How to apply (you, not the assistant)
```bash
cd "C:\Users\Tiwari\Documents\USage\Angad\BItbucket\0708\asbl-dev-angad-40686e886088\asbl-dev-angad-40686e886088"
git apply --check "path\to\prepared_patches\01-parse-visit-time.patch"   # dry run
git apply "path\to\prepared_patches\01-parse-visit-time.patch"
# repeat for 02, 03 — then run the existing scripts/test_sitevisit.ts
```

## What to measure after rollout
The Integrity rule **"Visit BOOKED, no reminder ever scheduled"** (currently 89%)
is the success metric — it should drop toward 0 within days of patch 02 going
live. The daily digest already carries the coverage line every morning.
