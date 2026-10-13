"""Dummy data so the CRM and the QA agent run with no real database.

install() replaces pymongo.MongoClient with ONE shared in-memory (mongomock)
client and fills it with:
  * 1,000 leads: website chats, WhatsApp threads, AI calls (transcripts,
    recordings, schedules) and a CRM stage for each lead
  * the outputs of all five QA agents (chatbot QA, voice QA, analytics,
    feedback, recommendations) for those conversations

Every name, phone number and conversation here is generated. Nothing ever
connects to a real MongoDB, and nothing is saved: data resets on each start.

Self-check:  python demo_data.py
"""
import random
from collections import Counter
from datetime import datetime, timedelta, timezone

import mongomock
import pymongo
from bson import ObjectId

N_LEADS = 1000
# SHORTCUT: website leads are capped at 150 because the CRM's real cron_refresh
# profiles at most 150 never-seen website leads per run. Raise both together.
N_WEBSITE = 150
N_VOICE_QA = 700  # most recent answered calls that the voice QA agent "evaluated"
WINDOW_H = 24     # each 4-hourly agent run looks back this far (4h is too sparse for 1,000 rows over 45 days)
VISIT_TARGET = 0.2  # site-visit conversion goal the analytics agent flags against
AGENT = "Anandita"
PROJECTS = ["LOFT", "SPECTRA", "BROADWAY", "LANDMARK", "LEGACY"]
PRICE_CR = {"LOFT": 1.9, "SPECTRA": 1.4, "BROADWAY": 2.3, "LANDMARK": 1.2, "LEGACY": 2.8}
BAND = {"LANDMARK": "1cr_1.5cr", "SPECTRA": "1cr_1.5cr", "LOFT": "1.5cr_2cr",
        "BROADWAY": "2cr_3cr", "LEGACY": "2cr_3cr"}
FIRST = ["Aarav", "Vivaan", "Aditya", "Sai", "Arjun", "Krishna", "Ishaan", "Rohan", "Kiran",
         "Ananya", "Diya", "Saanvi", "Kavya", "Meera", "Priya", "Sneha", "Lakshmi", "Divya",
         "Rahul", "Vikram", "Suresh", "Venkat", "Harsha", "Nikhil", "Pooja", "Swathi"]
LAST = ["Reddy", "Rao", "Sharma", "Varma", "Naidu", "Gupta", "Iyer", "Kumar", "Patel",
        "Shetty", "Menon", "Agarwal", "Joshi", "Nair", "Kapoor", "Singh", "Mehta", "Pillai"]
SOURCES = [("Inncircles", "inncircles", "CRM_Push"), ("Meta Ads", "facebook", "Meta_Leadgen"),
           ("Google Ads", "google", "Search_Brand"), ("99acres", "99acres", "Portal"),
           ("Housing.com", "housing", "Portal")]
WEB_CAMPAIGN = {"google": "LOFT_Search_Brand", "facebook": "LOFT_Meta_Leadgen",
                "instagram": "LOFT_Insta_Reels", "direct": ""}  # website utm source -> campaign
STAGES = {"New Lead": 25, "Lead Initiated": 38, "Pre Site Visit": 12,
          "Post Site Visit": 6, "Not Interested": 19}
NI_REASONS = ["Not Responding", "Declined", "Budget", "Inventory", "Already Purchased",
              "Location", "Looking for Rent"]
MISSED = ["NO_ANSWER", "NO_ANSWER", "BUSY", "NOT_CONNECTED", "SWITCHED_OFF"]
SCHEDULE_STATUS = {"NO_ANSWER": "no_answer", "BUSY": "busy", "NOT_CONNECTED": "failed",
                   "SWITCHED_OFF": "switched_off"}  # anything else -> "completed"

# Website chatbot questions: (user, bot, CTA artifact, CTA label)
QUESTIONS = [
    ("What is the price of a 3 BHK?", "3 BHK homes start around {p} crore.", "price_sheet", "Price sheet"),
    ("Can I see the floor plans?", "Sure, we have 3 and 4 BHK layouts.", "floor_plan", "Floor plans"),
    ("Is there a payment plan?", "Yes, there is a construction-linked plan.", "payment_plan", "Payment plan"),
    ("I want to visit the site", "Great, pick a slot that suits you.", "site_visit_booking", "Book a site visit"),
    ("When is possession?", "Possession is planned for December 2027.", "", ""),
    ("Is it RERA approved?", "Yes, the project is RERA registered.", "", ""),
    ("What amenities are there?", "Clubhouse, pool, gym and a kids' play area.", "", ""),
]
WA_OUT = ["Hi {f}, thanks for your interest in {p}. Here is the brochure.",
          "Hi {f}, sharing the price sheet for {p} as discussed.",
          "Hi {f}, would you like to book a site visit this weekend?"]
WA_IN = ["Yes please share floor plans", "What is the price for 3 BHK?", "Call me after 6 pm",
         "Is the clubhouse ready?", "Saturday works for a visit"]

# QA issue banks: type -> (severity, detail)
CHAT_ISSUES = {
    "KB_MISMATCH": ("HIGH", "Bot quoted a starting price that doesn't match the knowledge base."),
    "INVENTED_FACT": ("HIGH", "Bot claimed a metro station opens next year; not in the knowledge base."),
    "GUARDRAIL_VIOLATION": ("HIGH", "Bot promised a discount, which it is not allowed to offer."),
    "INCOMPLETE_RESPONSE": ("MEDIUM", "User asked for RERA and possession; bot only answered possession."),
    "TONE_ISSUE": ("MEDIUM", "Reply was abrupt after the user raised a budget concern."),
}
VOICE_ISSUES = {
    "DECIMAL_NUMBER": ("HIGH", "Said 'one ninety crore' instead of 'one point nine crore'."),
    "KB_MISMATCH": ("HIGH", "Agent gave the wrong possession date."),
    "INVENTED_FACT": ("HIGH", "Agent said a school is inside the campus; not in the knowledge base."),
    "LANGUAGE_HANDLING": ("MEDIUM", "Customer switched to Telugu; agent kept replying in English."),
    "INCOMPLETE_RESPONSE": ("MEDIUM", "Customer asked about parking; agent moved on without answering."),
}
LOW_ISSUE = {"chatbot": ("MINOR_PHRASING", "Repeated the same sentence twice in one reply."),
             "voice": ("YEAR_FORMAT", "Read 2027 as 'two zero two seven'.")}
SPAM_TEXT = {  # spam filter category -> a junk website message
    "JOB_QUERY": "Are you hiring? I want a job in sales.",
    "PROMO_SCAM": "Earn 50,000 per day from home!! Reply YES to join.",
    "LINK_URL": "check this offer example.com/free-gift",
    "AUTO_REPLY": "Thank you for your message. I am out of office until Monday.",
    "PERSONAL_IRRELEVANT": "hi what is your name, are you a real person?",
    "KEYSMASH": "asdfgh jkl;;",
}
FIX = {  # issue type -> (change type, where, fix)
    "KB_MISMATCH": ("KB_UPDATE", "knowledge_base/webbot/02_kb_project_loft.md",
                    "Put the current price and possession date in one clearly labelled table."),
    "INVENTED_FACT": ("PROMPT_UPDATE", "knowledge_base/webbot/01_system_prompt.md",
                      "Add a rule: if a fact is not in the knowledge base, say so and offer a callback."),
    "GUARDRAIL_VIOLATION": ("PROMPT_UPDATE", "knowledge_base/webbot/01_system_prompt.md",
                            "Restate that discounts and offers come only from the sales team."),
    "INCOMPLETE_RESPONSE": ("PROMPT_UPDATE", "agents/prompts/chatbot_qa.md",
                            "Answer every part of a multi-part question before moving on."),
    "TONE_ISSUE": ("PROMPT_UPDATE", "knowledge_base/webbot/05_kb_persona_playbook.md",
                   "Acknowledge budget concerns before suggesting alternatives."),
    "DECIMAL_NUMBER": ("PROMPT_UPDATE", "knowledge_base/anandita/system_prompt.md",
                       "Add examples: 1.9 crore -> 'one point nine crore'."),
    "LANGUAGE_HANDLING": ("PROMPT_UPDATE", "knowledge_base/anandita/system_prompt.md",
                          "Switch language when the customer does, within one turn."),
    # voice-agent versions of shared issue types point at the voice agent's own files
    ("KB_MISMATCH", "voice"): ("KB_UPDATE", "knowledge_base/anandita/project_facts.md",
                               "Correct the possession date in the voice agent's project facts."),
    ("INVENTED_FACT", "voice"): ("PROMPT_UPDATE", "knowledge_base/anandita/system_prompt.md",
                                 "Add a rule: if a fact is not in project_facts.md, say you'll confirm and call back."),
    ("INCOMPLETE_RESPONSE", "voice"): ("PROMPT_UPDATE", "knowledge_base/anandita/qa_checklist.md",
                                       "Check every customer question is answered before closing the call."),
}

N_BULK = 60  # cold-call numbers for the Bulk Calls tab (on top of the 1,000 leads)
BULK_OUTCOMES = ["CONNECTED", "NOT_INTERESTED", "AUTO_CALLBACK", "PRE_SITE"]
BULK_SHEET = []  # filled by seed(): the campaign sheet rows demo.py uploads
IST = timezone(timedelta(hours=5, minutes=30))

_client = None


def _calling_hours(t, rng):
    """Move a call into 9am-9pm IST, the window the real dialler calls in."""
    local = t.astimezone(IST)
    if 9 <= local.hour < 21:
        return t
    day = local.date() + timedelta(days=1 if local.hour >= 21 else 0)
    return (datetime(day.year, day.month, day.day, 9, tzinfo=IST) + timedelta(minutes=rng.uniform(0, 600))
            ).astimezone(timezone.utc)


def install():
    """Point every pymongo.MongoClient(...) at one seeded in-memory database."""
    global _client
    if _client is None:
        _client = mongomock.MongoClient()
        seed(_client)
        pymongo.MongoClient = lambda *args, **kwargs: _client
    return _client


def _transcript(rng, outcome, first, project, speaker, start):
    lines = [(AGENT, f"Hi {first}, this is {AGENT} calling about {project.title()}. Is this a good time?")]
    if outcome == "DROPPED":
        lines.append((speaker, "Who is this?"))
    elif outcome == "AUTO_CALLBACK":
        lines += [(speaker, "I'm driving, call me in the evening."),
                  (AGENT, "Sure, I'll call you back after 6 PM.")]
    else:
        lines += [(speaker, "Yes, tell me."),
                  (AGENT, f"Are you looking at 3 or 4 BHK? Prices start around {PRICE_CR[project]} crore."),
                  (speaker, rng.choice(["3 BHK, budget around two crore.", "4 BHK. When is possession?",
                                        "Mostly 3 BHK. Is there a clubhouse?"]))]
        if outcome == "PRE_SITE":
            lines += [(AGENT, "Would you like to visit the site this weekend?"),
                      (speaker, rng.choice(["Saturday 11 AM works.", "Sunday evening is fine."])),
                      (AGENT, "Done, I've booked your visit and will send the location on WhatsApp.")]
        elif outcome == "NOT_INTERESTED":
            lines += [(AGENT, "Shall I share the floor plans on WhatsApp?"),
                      (speaker, rng.choice(["No, I already bought a flat.", "It's above my budget.",
                                            "I need something closer to my office."]))]
        elif outcome == "VIRTUAL_TOUR":
            lines += [(speaker, "I'm abroad right now. Can I see it online?"),
                      (AGENT, "Of course, I'll share a virtual tour link on WhatsApp.")]
        else:
            lines += [(AGENT, "I'll send the brochure and price sheet on WhatsApp."), (speaker, "Okay, thanks.")]
    out, t = [], start
    for who, text in lines:
        t += timedelta(seconds=rng.randint(4, 25))
        out.append({"speaker": who, "text": text, "ts": t})
    return out


def _call_plan(rng, stage, reason):
    """Call outcomes in order, consistent with the lead's CRM stage."""
    miss = lambda k: [rng.choice(MISSED) for _ in range(k)]
    if stage == "New Lead":
        return miss(rng.choice([0, 0, 1, 2]))
    if stage == "Lead Initiated":
        return miss(rng.randint(0, 4)) + [rng.choice(["CONNECTED", "AUTO_CALLBACK", "DROPPED", "VIRTUAL_TOUR"])]
    if stage == "Pre Site Visit":
        return miss(rng.randint(0, 3)) + ["CONNECTED", "PRE_SITE"]
    if stage == "Post Site Visit":
        return miss(rng.randint(0, 2)) + ["PRE_SITE", "CONNECTED"]
    if reason == "Not Responding":
        return miss(rng.randint(3, 6))
    return miss(rng.randint(0, 3)) + ["NOT_INTERESTED"]


def _verdict(rng, bank, kind):
    """A QA agent verdict: ~25% FAIL with real-looking issues."""
    if rng.random() < 0.25:
        types = rng.sample(sorted(bank), k=rng.choice([1, 1, 2]))
        issues = [{"type": t, "severity": bank[t][0], "detail": bank[t][1]} for t in types]
        summary = f"Failed: {issues[0]['detail']}"
        return "FAIL", rng.randint(2, 6), issues, summary
    issues = []
    if rng.random() < 0.15:
        t, d = LOW_ISSUE[kind]
        issues = [{"type": t, "severity": "LOW", "detail": d}]
    return "PASS", rng.randint(7, 10), issues, "Answered accurately and offered a clear next step."


def seed(client, n_leads=N_LEADS):
    rng = random.Random(42)  # same data on every start, in every app
    now = datetime.now(timezone.utc)
    iso = lambda d: d.isoformat()
    zleads, wa, transcripts, recordings, schedules = [], [], [], [], []
    loft_leads, vlogs, convs, events, chat_qa, answered = [], [], [], [], [], []

    def add_call(sid, phone, project, outcome, t, first, esid):
        """One call: transcript, schedule (trigger record) and, if answered, a recording."""
        inbound = outcome == "CONNECTED" and rng.random() < 0.05
        direction = "inbound" if inbound else "outbound"
        turns = [] if outcome in MISSED else _transcript(rng, outcome, first, project, "91" + phone, t)
        lang = rng.choice(["English", "English", "Hinglish", "Telugu", "Hindi"])
        transcripts.append({"call_sid": sid, "phone_number": "+91" + phone, "project": project,
                            "call_direction": direction, "call_outcome": outcome, "started_at": t,
                            "transcript": turns, "language": lang})
        schedules.append({"to": "+91" + phone, "call_id": sid, "call_outcome": outcome,
                          "status": SCHEDULE_STATUS.get(outcome, "completed"), "metadata": {"project": project},
                          "external_schedule_id": esid, "created_at": t, "scheduled_at": t, "called_at": iso(t)})
        if not turns:
            return None
        dur = (turns[-1]["ts"] - turns[0]["ts"]).total_seconds() + rng.randint(5, 40)
        recordings.append({"call_sid": sid, "phone_number": "+91" + phone, "call_status": "completed",
                           "call_direction": direction, "recording_url": f"https://example.com/recordings/{sid}.mp3",
                           "recording_duration_secs": round(dur), "created_at": t})
        return t, sid, "+91" + phone, project, lang, inbound

    for i in range(n_leads):
        first, last = rng.choice(FIRST), rng.choice(LAST)
        phone = f"74{i:08d}"  # obviously fake, unique 10-digit numbers
        website = i < N_WEBSITE
        project = "LOFT" if website else rng.choice(PROJECTS)
        stage = rng.choices(list(STAGES), weights=list(STAGES.values()))[0]
        reason = rng.choice(NI_REASONS) if stage == "Not Interested" else None
        created = now - timedelta(days=rng.uniform(0.3, 45))
        lead_source, utm, camp = rng.choice(SOURCES)
        plid = f"{1000 + i}-{project}"
        web_src = rng.choice(list(WEB_CAMPAIGN)) if website else None
        campaign = WEB_CAMPAIGN[web_src] if website else f"{project}_{camp}"

        # CRM record (Zoho). Website leads also get one, so they carry a stage.
        zleads.append({
            "_id": plid, "phone": "91" + phone, "first_name": first, "last_name": last,
            "project": project, "prd_stage": stage, "not_interested_reason": reason,
            "lead_source": "Website" if website else lead_source,
            "utm_source": "website" if website else utm, "utm_campaign": campaign,
            "campaign_name": campaign, "created_at": created, "lead_received_at": created,
            "source_lead_id": f"SRC-{500000 + i}", "mlid": str(700000 + i), "bot_enabled": True,
            "chatbot_status": rng.choice(["NA", "CF", "SF"]), "call_status": "SS",
        })

        if website:  # website lead + chatbot conversations + browsing sessions
            # Real Mongo ids start with their creation time; plain ObjectId() would give
            # all 150 leads the same prefix (they're made in the same second).
            oid = ObjectId(f"{int(created.timestamp()):08x}{rng.getrandbits(64):016x}")
            vid = f"v-{i:05d}"
            loft_leads.append({"_id": oid, "name": f"{first} {last}", "phone": "+91" + phone,
                               "createdAt": created, "visitorId": vid,
                               "source": {"utmSource": web_src, "utmCampaign": WEB_CAMPAIGN[web_src]}})
            vlogs.append({"visitorId": vid, "leadId": oid})
            for s in range(rng.randint(1, 5)):
                st = created + timedelta(hours=rng.uniform(0, 72))
                events.append({"leadId": oid, "visitorId": vid, "startedAt": st,
                               "endedAt": st + timedelta(minutes=rng.uniform(1, 15))})
            for c in range(rng.randint(1, 3)):
                t = created - timedelta(minutes=rng.uniform(1, 30)) + timedelta(days=c * rng.uniform(0, 3))
                if t > now:
                    break
                cid = f"c-{int(t.timestamp() * 1000)}-{i:04d}{c}"
                depth, start = [], t
                spam = rng.choice(sorted(SPAM_TEXT)) if rng.random() < 0.06 else None
                if spam:  # one junk message; the spam filter skips these before QA
                    t += timedelta(seconds=rng.randint(10, 90))
                    depth.append({"userText": SPAM_TEXT[spam], "userAt": t, "botAt": t + timedelta(seconds=3),
                                  "botText": "I can help with questions about our homes. What would you like to know?"})
                for q, a, art, label in ([] if spam else rng.sample(QUESTIONS, k=rng.randint(2, 5))):
                    t += timedelta(seconds=rng.randint(10, 90))
                    depth.append({"userText": q, "botText": a.format(p=PRICE_CR[project]),
                                  "userAt": t, "botAt": t + timedelta(seconds=3)})
                    if art and rng.random() < 0.5:
                        t += timedelta(seconds=20)
                        depth.append({"isCtaTrigger": True, "artifact": art, "artifactLabel": label,
                                      "userText": "Yes please", "botText": f"Here you go: {label}.",
                                      "userAt": t, "botAt": t + timedelta(seconds=2)})
                convs.append({"conversationId": cid, "leadId": oid, "visitorId": vid,
                              "createdAt": start, "conversationDepth": depth})
                evaluated = min(start + timedelta(hours=rng.uniform(0.5, 4)), now)
                base = {"conversation_id": cid, "turn_count": len(depth), "visitor_id": vid,
                        "lead_id": str(oid), "conversation_date": iso(start), "evaluated_at": iso(evaluated),
                        "confidence": round(rng.uniform(0.7, 0.98), 2),
                        "token_usage": {"input": rng.randint(9000, 12000), "output": rng.randint(150, 600)}}
                if spam:  # same flag shape as spam_filter.flag_conversation
                    chat_qa.append({**base, "status": "SKIPPED", "skip_reason": "SPAM_FILTERED", "is_spam": True,
                                    "spam_flags": [{"turn": 1, "type": spam, "layer": 1, "text": SPAM_TEXT[spam]}],
                                    "issues": [],
                                    "summary": "Skipped by spam filter.", "score": None})
                    continue
                status, score, issues, summary = _verdict(rng, CHAT_ISSUES, "chatbot")
                chat_qa.append({**base, "status": status, "score": score, "issues": issues,
                                "summary": summary, "is_spam": False, "spam_flags": [], "user_flags": []})

        # AI calls, only inside calling hours
        t = created
        for n, outcome in enumerate(_call_plan(rng, stage, reason)):
            t = _calling_hours(t + timedelta(hours=rng.uniform(2, 30)), rng)
            if t > now:
                break
            hit = add_call(f"CA-{i:04d}-{n}", phone, project, outcome, t, first,
                           f"prd-{plid}-{int(t.timestamp() * 1000)}")
            if hit:
                answered.append(hit)

        # WhatsApp thread for ~65% of leads; ~35% of those reply
        if rng.random() < 0.65:
            by_date, last_at, d = {}, None, created + timedelta(hours=rng.uniform(1, 12))
            for _ in range(rng.randint(1, 4)):
                if d > now:
                    break
                day = by_date.setdefault(d.strftime("%Y-%m-%d"), {"inbound": [], "outbound": []})
                day["outbound"].append({"msg": rng.choice(WA_OUT).format(f=first, p=project.title()), "time": iso(d)})
                last_at = d
                if rng.random() < 0.35:
                    r = d + timedelta(minutes=rng.uniform(2, 240))
                    if r <= now:
                        day["inbound"].append({"msg": rng.choice(WA_IN), "time": iso(r)})
                        last_at = r
                d += timedelta(days=rng.uniform(0.5, 3))
            if by_date:
                wa.append({"_id": "91" + phone, "phone": "91" + phone, "last_message_at": iso(last_at),
                           "by_date": by_date})

    # Bulk cold-call campaign: numbers that exist only in the call logs. demo.py uploads
    # BULK_SHEET through the CRM's real upload route, which builds these leads.
    BULK_SHEET.clear()
    for j in range(N_BULK):
        first, last = rng.choice(FIRST), rng.choice(LAST)
        phone, project = f"75{j:08d}", rng.choice(PROJECTS)
        BULK_SHEET.append({"Phone": phone, "Project": project, "Name": f"{first} {last}", "Lead ID": f"BLK-{3000 + j}"})
        t = now - timedelta(days=rng.uniform(1, 20))
        for n in range(rng.randint(1, 4)):
            t = _calling_hours(t + timedelta(hours=rng.uniform(3, 48)), rng)
            if t > now:
                break
            add_call(f"BK-{j:04d}-{n}", phone, project, rng.choice(MISSED + BULK_OUTCOMES), t, first,
                     f"dash_bulk-{j}-{n}")

    # Voice QA: the most recent answered calls
    voice_qa = []
    for t, sid, ph, project, lang, inbound in sorted(answered, reverse=True)[:N_VOICE_QA]:
        status, score, issues, summary = _verdict(rng, VOICE_ISSUES, "voice")
        voice_qa.append({
            "call_sid": sid, "phone_number": ph, "call_direction": "inbound" if inbound else "outbound",
            "language_used": lang, "project": project, "status": status, "score": score,
            "confidence": round(rng.uniform(0.7, 0.98), 2), "issues": issues, "summary": summary,
            "language_compliance": "FAIL" if any(x["type"] == "LANGUAGE_HANDLING" for x in issues) else "PASS",
            "is_spam": False, "conversation_date": iso(t),
            "evaluated_at": iso(min(t + timedelta(hours=rng.uniform(0.5, 4)), now)),
            "token_usage": {"input": rng.randint(4000, 7000), "output": rng.randint(150, 500)},
        })

    runs = _agent_runs(rng, now, chat_qa, voice_qa, zleads)

    zoho, loft, vb, qa = client["Zoho_Database"], client["asbl_loft"], client["ASBLVoiceBot"], client["qa_results"]
    for col, docs in [(zoho["leads"], zleads), (zoho["whatsapp_messages"], wa),
                      (loft["leads"], loft_leads), (loft["visitorLogs"], vlogs),
                      (loft["conversations"], convs), (loft["events"], events),
                      (vb["call_transcripts"], transcripts), (vb["call_recordings"], recordings),
                      (vb["call_schedules"], schedules), (qa["chatbot_qa"], chat_qa), (qa["voice_qa"], voice_qa),
                      *[(qa[name], docs) for name, docs in runs.items()]]:
        if docs:
            col.insert_many(docs)
    return client


SEVERITY_PENALTY = {"HIGH": 2.0, "MEDIUM": 0.8, "LOW": 0.2}  # same as qa-agent/agents/feedback.py


def _health(results):
    """Health 0-10, same formula as feedback.compute_health_score: score minus severity x confidence."""
    if not results:
        return 0.0
    adjusted = [max(0.0, min(10.0, (r["score"] or 0) - sum(
        SEVERITY_PENALTY.get(i["severity"], 0.2) * r.get("confidence", 0.5) for i in r["issues"])))
        for r in results]
    return round(sum(adjusted) / len(results), 2)


def _agent_runs(rng, now, chat_qa, voice_qa, zleads):
    """Analytics, feedback, recommendation and health outputs: one run every 4 hours for 7 days, each looking back WINDOW_H hours."""
    out = {k: [] for k in ("analytics_runs", "feedback", "recommendations", "health_scores", "token_usage")}
    # Site-visit conversion by price band, from the leads themselves
    bands = {}
    for z in zleads:
        b = bands.setdefault(BAND[z["project"]], {"leads": 0, "site_visits": 0})
        b["leads"] += 1
        b["site_visits"] += z["prd_stage"] in ("Pre Site Visit", "Post Site Visit")
    for b in bands.values():
        b["conversion_rate"] = round(b["site_visits"] / b["leads"], 3)

    for k in range(42):
        run_at = now - timedelta(hours=4 * k)
        lo, hi = (run_at - timedelta(hours=WINDOW_H)).isoformat(), run_at.isoformat()
        win = lambda rows: [r for r in rows if lo < r["evaluated_at"] <= hi and r["status"] != "SKIPPED"]
        cb, vc = win(chat_qa), win(voice_qa)
        fail = lambda rows: sum(r["status"] == "FAIL" for r in rows) / len(rows) if rows else 0.0
        cb_fail, vc_fail = fail(cb), fail(vc)

        flags = []
        for name, rows, rate in (("CHATBOT", cb, cb_fail), ("VOICE", vc, vc_fail)):
            if rows and rate > 0.25:
                flags.append({"type": f"{name}_HIGH_FAIL_RATE", "severity": "HIGH", "source": "qa_health_checker",
                              "detail": f"{name.title()} fail rate {rate:.0%} in the last {WINDOW_H} hours (target under 25%)."})
        for band, b in bands.items():
            if b["conversion_rate"] < VISIT_TARGET:
                flags.append({"type": "BAND_CONVERSION_BELOW_TARGET", "severity": "MEDIUM", "source": "orchestrator",
                              "detail": f"{band} band converts {b['conversion_rate']:.1%} of leads to site visits (target {VISIT_TARGET:.0%})."})
        accuracy = round(rng.uniform(0.84, 0.98), 2)
        if accuracy < 0.9:
            flags.append({"type": "META_SIGNAL_MISMATCH", "severity": "HIGH", "source": "orchestrator",
                          "detail": f"Meta conversion signal matches the CRM on only {accuracy:.0%} of leads."})
        out["analytics_runs"].append({
            "run_at": hi, "outcomes_checked": ["site_visit_booked", "lead_captured"],
            "outcome_goals": {"site_visit_booked": VISIT_TARGET}, "band_conversion_rates": bands,
            "multiplier_effectiveness": {a: {"shown": rng.randint(20, 90), "effectiveness": round(rng.uniform(0.5, 0.75), 3)}
                                         for a in ("price_sheet", "floor_plan", "site_visit_booking")},
            "meta_signal_accuracy": {"lead_captured_matches_db": accuracy, "discrepancy_count": rng.randint(0, 9)},
            "funnel_gaps": [f["detail"] for f in flags if f["severity"] != "HIGH"][:2], "flags": flags,
            "agent_messages": [],
        })

        cb_health, vc_health = _health(cb), _health(vc)
        high_f = sum(f["severity"] == "HIGH" for f in flags)
        med_f = sum(f["severity"] == "MEDIUM" for f in flags)
        for comp, score, rate, n in (("chatbot", cb_health, cb_fail, len(cb)),
                                     ("voice", vc_health, vc_fail, len(vc)),
                                     ("analytics", max(0.0, 10.0 - high_f * 2.5 - med_f * 1.0),
                                      round(high_f / max(len(flags), 1), 3), len(flags))):
            if n == 0:
                continue  # nothing evaluated in this window -> no score
            out["health_scores"].append({"recorded_at": hi, "component": comp, "score": score,
                                         "fail_rate": round(rate, 3), "details": {"evaluated": n}})

        # Feedback: the top recurring issue types in this window become "problems"
        issues = [(i, comp, r) for comp, rows in (("chatbot", cb), ("voice", vc))
                  for r in rows for i in r["issues"] if i["severity"] != "LOW"]
        top = Counter((i["type"], comp) for i, comp, _ in issues).most_common(3)
        problems = []
        for n, ((itype, comp), count) in enumerate(top):
            sev, detail = (CHAT_ISSUES if comp == "chatbot" else VOICE_ISSUES)[itype]
            evidence = [r.get("conversation_id") or r.get("call_sid") for i, c, r in issues if i["type"] == itype and c == comp]
            problems.append({"id": f"P{n + 1}", "issue_type": itype, "urgency": sev, "components": [comp],
                             "title": f"{itype.replace('_', ' ').title().replace('Kb ', 'KB ')} ({comp}, {count}x)",
                             "what_is_wrong": detail, "what_is_not_wrong": "Other answers in the same conversations were accurate.",
                             "evidence": evidence[:3]})
        out["feedback"].append({
            "submitted_at": hi, "source": "automated", "window_hours": WINDOW_H,
            "chatbot_health": cb_health, "voice_health": vc_health,
            "chatbot_fail_rate": round(cb_fail, 3), "voice_fail_rate": round(vc_fail, 3),
            "chatbot_evaluated": len(cb), "voice_evaluated": len(vc), "problems_found": len(problems),
            "high_problems": sum(p["urgency"] == "HIGH" for p in problems),
            "medium_problems": sum(p["urgency"] == "MEDIUM" for p in problems),
            "top_issue_types": [t for (t, _), _ in top], "problems": problems,
            "recommendations_generated": bool(problems),
        })
        if problems:
            fixes = []
            for n, p in enumerate(problems):
                change, where, fix = FIX.get((p["issue_type"], p["components"][0])) or FIX[p["issue_type"]]
                fixes.append({"rank": n + 1, "problem_id": p["id"], "component": p["components"][0],
                              "problem": p["title"], "fix": fix, "where": where, "change_type": change,
                              "kb_reference": where, "expected_outcome": "Fewer repeats of this issue in the next runs."})
            out["recommendations"].append({
                "generated_at": hi, "negative_signals": [p["title"] for p in problems],
                "root_cause": problems[0]["what_is_wrong"], "fixes": fixes,
                "priority": "HIGH" if any(p["urgency"] == "HIGH" for p in problems) else "MEDIUM"})
        for agent in ("chatbot_qa", "voice_qa", "analytics", "feedback", "recommendation"):
            out["token_usage"].append({"cron_slot": hi, "agent": agent, "input_tokens": rng.randint(5000, 90000),
                                       "output_tokens": rng.randint(500, 9000), "calls": rng.randint(1, 40),
                                       "recorded_at": hi})
    return out


if __name__ == "__main__":
    c = install()
    z, l, vb, qa = c["Zoho_Database"], c["asbl_loft"], c["ASBLVoiceBot"], c["qa_results"]
    assert z["leads"].count_documents({}) == N_LEADS, "one CRM record per lead"
    assert l["leads"].count_documents({}) == N_WEBSITE
    conv_ids = {d["conversationId"] for d in l["conversations"].find({}, {"conversationId": 1})}
    sids = {d["call_sid"] for d in vb["call_transcripts"].find({}, {"call_sid": 1})}
    assert all(d["conversation_id"] in conv_ids for d in qa["chatbot_qa"].find()), "chatbot QA -> real conversation"
    assert all(d["call_sid"] in sids for d in qa["voice_qa"].find()), "voice QA -> real call"
    assert qa["voice_qa"].count_documents({}) == N_VOICE_QA
    for name in ("analytics_runs", "feedback", "recommendations", "health_scores", "token_usage"):
        assert qa[name].count_documents({}) > 0, name
    assert all(0 <= d["score"] <= 10 for d in qa["health_scores"].find()), "health is out of 10"
    hours = {datetime.fromisoformat(s["called_at"]).astimezone(IST).hour for s in vb["call_schedules"].find()}
    assert hours and min(hours) >= 9 and max(hours) < 21, f"calls outside 9am-9pm IST: {sorted(hours)}"
    assert len(BULK_SHEET) == N_BULK and vb["call_schedules"].count_documents({"call_id": {"$regex": "^BK-"}}) > 0
    print({f"{db}.{name}": c[db][name].count_documents({}) for db, name in [
        ("Zoho_Database", "leads"), ("Zoho_Database", "whatsapp_messages"), ("asbl_loft", "leads"),
        ("asbl_loft", "conversations"), ("ASBLVoiceBot", "call_transcripts"), ("ASBLVoiceBot", "call_recordings"),
        ("qa_results", "chatbot_qa"), ("qa_results", "voice_qa"), ("qa_results", "analytics_runs"),
        ("qa_results", "feedback"), ("qa_results", "recommendations"), ("qa_results", "health_scores")]})
    print("demo_data self-check passed")
