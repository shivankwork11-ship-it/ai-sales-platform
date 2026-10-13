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
import json
import os
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

# One script per answered call type. make_recordings.py turns each into a real audio
# clip (agent + customer voices), so every call's transcript matches what you hear.
_GREETING = ("agent", "Hello, this is Anandita calling about the homes you enquired about. "
                      "Is this a good time to talk?")
CALL_SCRIPTS = {
    "CONNECTED": [_GREETING, ("customer", "Yes, tell me."),
                  ("agent", "Are you looking for a 3 BHK or a 4 BHK?"),
                  ("customer", "3 BHK. When is possession?"),
                  ("agent", "Possession is planned for December 2027. I will send you the brochure "
                            "and price sheet on WhatsApp."),
                  ("customer", "Okay, thank you."), ("agent", "Thank you, have a great day.")],
    "PRE_SITE": [_GREETING, ("customer", "Yes, go ahead."),
                 ("agent", "Would you like to visit the site and see the show flat this weekend?"),
                 ("customer", "Yes, Saturday at 11 works for me."),
                 ("agent", "Done. I have booked your visit for Saturday at 11 AM. "
                           "I will send the location on WhatsApp."),
                 ("customer", "Great, see you then.")],
    "NOT_INTERESTED": [_GREETING, ("customer", "Who is this?"),
                       ("agent", "This is Anandita. You had enquired about our homes. "
                                 "Shall I share the floor plans?"),
                       ("customer", "No, I have already bought a flat. I am not interested."),
                       ("agent", "No problem. Thank you for your time.")],
    "AUTO_CALLBACK": [_GREETING, ("customer", "I am driving right now. Can you call me in the evening?"),
                      ("agent", "Sure, I will call you back after 6 PM. Drive safe.")],
    "VIRTUAL_TOUR": [_GREETING, ("customer", "I am abroad right now. Can I see the property online?"),
                     ("agent", "Of course. I will set up a virtual tour and share the link on WhatsApp."),
                     ("customer", "Perfect, thanks.")],
    "DROPPED": [_GREETING, ("customer", "Hello? Who is this?")],
    # outcome NOT_INTERESTED + do-not-call request (the real dialler sets dncTriggered on these)
    "DNC": [_GREETING, ("customer", "Please don't call me again. Remove my number from your list."),
            ("agent", "Understood. I have removed your number. Sorry for the trouble.")],
}
RECORDING_URL = "/static/demo_recordings"  # served by the CRM from crm/static/demo_recordings
RECORDING_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "crm", "static", "demo_recordings")
try:  # where each script line starts in its clip; written by make_recordings.py
    with open(os.path.join(RECORDING_DIR, "timings.json")) as _f:
        RECORDING_TIMINGS = json.load(_f)
except OSError:
    RECORDING_TIMINGS = {}
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
WA_INTENT = {"Yes please share floor plans": "floor_plan_request", "What is the price for 3 BHK?": "price_query",
             "Call me after 6 pm": "callback_request", "Is the clubhouse ready?": "amenity_query",
             "Saturday works for a visit": "site_visit_request"}

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
ZOHO_EXPORT = []  # filled by seed(): rows of the daily Zoho lead export CSV (ANGAD imports it)
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


def _transcript(outcome, speaker, start):
    """The call type's fixed script, timestamped where each line is heard in its recording."""
    lines = CALL_SCRIPTS[outcome]
    offsets = (RECORDING_TIMINGS.get(outcome) or {}).get("offsets") or [6.0 * n for n in range(len(lines))]
    return [{"speaker": AGENT if role == "agent" else speaker, "text": text, "ts": start + timedelta(seconds=off)}
            for (role, text), off in zip(lines, offsets)]


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

    def add_call(sid, phone, project, outcome, t, first, esid, provider="plivo", script=None):
        """One call: transcript, schedule (trigger record) and, if answered, a recording."""
        key = script or outcome  # which script/recording; differs from outcome only for DNC
        inbound = outcome == "CONNECTED" and rng.random() < 0.05
        direction = "inbound" if inbound else "outbound"
        turns = [] if outcome in MISSED else _transcript(key, "91" + phone, t)
        lang = rng.choice(["English", "English", "Hinglish", "Telugu", "Hindi"])
        transcripts.append({"call_sid": sid, "phone_number": "+91" + phone, "project": project,
                            "call_direction": direction, "call_outcome": outcome, "started_at": t,
                            "transcript": turns, "language": lang, "dnc": key == "DNC"})
        schedules.append({"to": "+91" + phone, "call_id": sid, "call_outcome": outcome,
                          "status": SCHEDULE_STATUS.get(outcome, "completed"), "metadata": {"project": project},
                          "provider": provider,  # telnyx = international (NRI) line, plivo = India (RI)
                          "external_schedule_id": esid, "created_at": t, "scheduled_at": t, "called_at": iso(t)})
        if not turns:
            return None
        dur = (RECORDING_TIMINGS.get(key) or {}).get("duration") or 6.0 * len(turns)
        recordings.append({"call_sid": sid, "phone_number": "+91" + phone, "call_status": "completed",
                           "call_direction": direction, "recording_url": f"{RECORDING_URL}/{key}.m4a",
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
        zid = f"5977462{i:012d}"  # 19-digit Zoho lead id; call schedule ids embed it
        provider = "telnyx" if rng.random() < 0.06 else "plivo"
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
            "zoho_lead_id": zid, "plid": plid, "stage": stage, "born_date": created.strftime("%Y-%m-%d"),
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
        t, plan, made = created, _call_plan(rng, stage, reason), []
        dnc = stage == "Not Interested" and reason != "Not Responding" and rng.random() < 0.25
        for n, outcome in enumerate(plan):
            t = _calling_hours(t + timedelta(hours=rng.uniform(2, 30)), rng)
            if t > now:
                break
            made.append(outcome)
            hit = add_call(f"CA-{i:04d}-{n}", phone, project, outcome, t, first,
                           f"prd-{zid}-{int(t.timestamp() * 1000)}", provider,
                           "DNC" if dnc and outcome == "NOT_INTERESTED" else None)
            if hit:
                answered.append(hit)
        if len(made) < len(plan):  # newer lead: the calls that set its stage haven't all happened yet
            fallback = "Pre Site Visit" if "PRE_SITE" in made else "Lead Initiated" if made else "New Lead"
            zleads[-1].update(prd_stage=fallback, stage=fallback, not_interested_reason=None)

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
    angad = _angad_sources(rng, now, zleads, transcripts, schedules, recordings, wa, loft_leads, events)

    zoho, loft, vb, qa = client["Zoho_Database"], client["asbl_loft"], client["ASBLVoiceBot"], client["qa_results"]
    for col, docs in [(zoho["leads"], zleads), (zoho["whatsapp_messages"], wa),
                      (loft["leads"], loft_leads), (loft["visitorLogs"], vlogs),
                      (loft["conversations"], convs), (loft["events"], events),
                      (vb["call_transcripts"], transcripts), (vb["call_recordings"], recordings),
                      (vb["call_schedules"], schedules), (qa["chatbot_qa"], chat_qa), (qa["voice_qa"], voice_qa),
                      *[(qa[name], docs) for name, docs in runs.items()],
                      *[(client[db][name], docs) for (db, name), docs in angad.items()]]:
        if docs:
            col.insert_many(docs)
    return client


# ANGAD (the "Main Dashboard") reads the dialler's own call log plus a few more
# collections. Everything below is DERIVED from the leads and calls generated
# above, so ANGAD, the CRM and the QA agent all show the same people and calls.
CALL_SUMMARY = {
    "CONNECTED": "Customer is looking for a 3 BHK and asked about possession (December 2027). "
                 "Brochure and price sheet sent on WhatsApp.",
    "PRE_SITE": "Customer agreed to a site visit on Saturday at 11 AM. Location shared on WhatsApp.",
    "NOT_INTERESTED": "Customer has already bought a flat and is not interested.",
    "AUTO_CALLBACK": "Customer was driving and asked for a call back after 6 PM.",
    "VIRTUAL_TOUR": "Customer is abroad and asked for a virtual tour; link to be shared on WhatsApp.",
    "DROPPED": "Call dropped right after the greeting.",
    "DNC": "Customer asked not to be called again; number removed from the calling list.",
}
CALL_INTENT = {  # the post-call intent model's read of each script
    "CONNECTED": ("buy_home", "moderate", "positive", "qualification", "medium", "not_discussed",
                  "Send brochure and price sheet on WhatsApp", "engaged", "agent_led"),
    "PRE_SITE": ("buy_home", "strong", "positive", "booking", "high", "booked",
                 "Site visit on Saturday at 11 AM; send location", "highly_engaged", "balanced"),
    "NOT_INTERESTED": ("not_interested", "none", "negative", "pitch", "low", "declined",
                       "Close the lead: already purchased", "disengaged", "agent_led"),
    "AUTO_CALLBACK": ("callback_requested", "weak", "neutral", "greeting", "medium", "not_discussed",
                      "Call back after 6 PM", "busy", "balanced"),
    "VIRTUAL_TOUR": ("buy_home", "moderate", "positive", "closing", "medium", "virtual_tour",
                     "Share the virtual tour link", "engaged", "balanced"),
    "DROPPED": ("unknown", "none", "neutral", "greeting", "low", "not_discussed",
                "Retry the call", "dropped", "agent_only"),
    "DNC": ("not_interested", "none", "negative", "greeting", "low", "declined",
            "Do not call again (customer request)", "disengaged", "balanced"),
}
WA_DOCS = {"CONNECTED": ["brochure", "price_sheet"], "PRE_SITE": ["location"], "VIRTUAL_TOUR": ["virtual_tour_link"]}
CALL_STATUS = {"NO_ANSWER": "no-answer", "BUSY": "busy", "NOT_CONNECTED": "failed", "SWITCHED_OFF": "failed"}
CRM_STAGE = {"New Lead": "new", "Lead Initiated": "contacted", "Pre Site Visit": "visit_scheduled",
             "Post Site Visit": "visit_done", "Not Interested": "not_interested"}
BUDGETS = ["1-1.5 Cr", "1.5-2 Cr", "2-3 Cr", "3 Cr+"]


def _next_saturday_11am(t):
    local = t.astimezone(IST)
    day = local.date() + timedelta(days=(5 - local.weekday()) % 7 or 7)
    return datetime(day.year, day.month, day.day, 11, tzinfo=IST).astimezone(timezone.utc)


def _angad_sources(rng, now, zleads, transcripts, schedules, recordings, wa, loft_leads, events):
    out = {k: [] for k in [
        ("angad", "calls"), ("angad", "leads"), ("angad", "scheduledcalls"),
        ("ASBLVoiceBot", "call_transcript_intent_identification"),
        ("Zoho_Database", "mlid_registry"), ("Zoho_Database", "plid_registry"),
        ("Zoho_Database", "site_visit_bookings"), ("Zoho_Database", "callback_log"),
        ("Zoho_Database", "doc_send_log"), ("Zoho_Database", "call_transcripts"),
        ("Intelligent_CRM", "persons"), ("Intelligent_CRM", "crm_leads"),
        ("Intelligent_CRM", "site_visits"), ("Intelligent_CRM", "lead_events"), ("Intelligent_CRM", "messages")]}
    lead_by_phone = {z["phone"][-10:]: z for z in zleads}
    dnc_phones = {tx["phone_number"][-10:] for tx in transcripts if tx.get("dnc")}
    esid = {s["call_id"]: s["external_schedule_id"] for s in schedules}
    rec = {r["call_sid"]: r for r in recordings}
    booking_call = {tx["call_sid"]: _next_saturday_11am(tx["started_at"]) for tx in transcripts
                    if tx["call_outcome"] == "PRE_SITE"}
    for sch in schedules:
        lead, n = lead_by_phone.get(sch["to"][-10:]), int(sch["call_id"].rsplit("-", 1)[1])
        sch["masterleadid"] = lead["mlid"] if lead else None
        sch["metadata"]["reason"] = ("manual_bulk" if not lead else "new_lead" if n == 0
                                     else f"fast_retry_{n}" if n <= 2 else f"daily_retry_{n - 2}")
        if sch["call_id"] in booking_call:
            sch["metadata"]["visit_date"] = booking_call[sch["call_id"]].astimezone(IST).strftime("%Y-%m-%d")
    for sid, visit in booking_call.items():  # the lead record carries its latest booked visit
        lead = lead_by_phone.get(next(tx["phone_number"][-10:] for tx in transcripts if tx["call_sid"] == sid))
        if lead:
            lead["site_visit_date"] = visit.astimezone(IST).strftime("%Y-%m-%d")
    for z in zleads:
        z["masterleadid"] = z["mlid"]
    wa_by_phone = {w["_id"][-10:]: w for w in wa}

    # website behaviour onto the CRM record (ANGAD's "web history")
    oid_phone = {l["_id"]: l["phone"][-10:] for l in loft_leads}
    web_secs, web_views = Counter(), Counter()
    for e in events:
        p = oid_phone.get(e["leadId"])
        web_secs[p] += (e["endedAt"] - e["startedAt"]).total_seconds()
        web_views[p] += rng.randint(2, 6)
    for p, secs in web_secs.items():
        lead_by_phone[p].update(first_page_visited="/", last_page_visited=rng.choice(["/floor-plans", "/pricing", "/amenities"]),
                                time_spent_minutes=round(secs / 60, 1), total_page_views=web_views[p])

    out[("Zoho_Database", "project_documents")] = [
        {"project": pr, "doc_type": d, "filename": f"{pr.title()}_{d}.pdf", "size_label": size,
         "applies_to_all": size == ""}
        for pr in PROJECTS for d, size in (("brochure", ""), ("price_sheet", ""), ("floor_plan", "3 BHK"),
                                           ("floor_plan", "4 BHK"), ("location", ""), ("virtual_tour_link", ""))]
    # WhatsApp: per-thread totals on the Zoho doc + one CRM message record per message
    for w in wa:
        lead = lead_by_phone.get(w["_id"][-10:])
        msgs = [(d, m) for day in w["by_date"].values() for d in ("inbound", "outbound") for m in day[d]]
        w.update(inbound_count=sum(d == "inbound" for d, _ in msgs), outbound_count=sum(d == "outbound" for d, _ in msgs),
                 total_count=len(msgs), first_message_at=min(m["time"] for _, m in msgs))
        for d, m in msgs:
            out[("Intelligent_CRM", "messages")].append({
                "person_id": f"P-{lead['mlid']}" if lead else None, "phone": "+91" + w["_id"][-10:],
                "channel": "whatsapp", "direction": d, "text": m["msg"], "at": m["time"],
                "intent": WA_INTENT.get(m["msg"]) if d == "inbound" else None})
    for z in zleads:
        p, mlid = z["phone"][-10:], z["mlid"]
        person = f"P-{mlid}"
        out[("Zoho_Database", "mlid_registry")].append({"_id": z["phone"], "mlid": mlid, "master_lead_id": mlid})
        out[("Zoho_Database", "plid_registry")].append({"_id": z["plid"], "phone": z["phone"], "project": z["project"], "mlid": mlid})
        out[("angad", "leads")].append({"phone": "+91" + p, "status": z["stage"], "source": z["lead_source"],
                                        "propertyInterested": z["project"], "createdAt": z["created_at"],
                                        "updatedAt": z["created_at"], "doNotCall": p in dnc_phones,
                                        "doNotCallReason": "asked on call" if p in dnc_phones else None})
        w = wa_by_phone.get(p, {}).get("by_date", {})
        out[("Intelligent_CRM", "persons")].append({"_id": person, "phone": "+91" + p, "mlid": mlid})
        out[("Intelligent_CRM", "crm_leads")].append({
            "_id": f"CL-{z['plid']}", "person_id": person, "project": z["project"], "stage": CRM_STAGE[z["stage"]],
            "born_at": z["created_at"], "counters": {"wa_in": sum(len(d["inbound"]) for d in w.values()),
                                                     "wa_out": sum(len(d["outbound"]) for d in w.values())}})

    for tx in transcripts:
        sid, p, oc, t = tx["call_sid"], tx["phone_number"][-10:], tx["call_outcome"], tx["started_at"]
        lead = lead_by_phone.get(p)  # None for bulk cold-call numbers
        r = rec.get(sid)
        dur = r["recording_duration_secs"] if r else None
        answered = bool(tx["transcript"])
        key = "DNC" if tx.get("dnc") else oc
        doc = {
            "roomName": sid, "phoneNumber": tx["phone_number"], "direction": tx["call_direction"],
            "status": CALL_STATUS.get(oc, "completed"), "outcome": oc, "startedAt": t,
            "endedAt": t + timedelta(seconds=dur or 0), "durationSeconds": dur, "createdAt": t,
            "updatedAt": t + timedelta(seconds=dur or 0), "slug": "follow-up" if sid[-2:] != "-0" else "new-lead",
            "source": "relay" if lead else "dashboard", "voiceEngine": "gemini-live", "agentPersonaId": "anandita",
            "externalScheduleId": esid.get(sid), "externalCustomerId": lead["mlid"] if lead else None,
            "transcript": [{"speaker": "agent" if x["speaker"] == AGENT else "lead", "text": x["text"],
                            "timestamp": x["ts"]} for x in tx["transcript"]],
            "summary": CALL_SUMMARY.get(key), "recordingUrl": r["recording_url"] if r else None,
            "metadata": {"project": tx["project"]}, "dncTriggered": key == "DNC",
            "sentiment": CALL_INTENT[key][2] if answered else None,
            "closingScore": ({"PRE_SITE": rng.randint(80, 95), "CONNECTED": rng.randint(50, 75),
                              "VIRTUAL_TOUR": rng.randint(55, 75)}.get(key, rng.randint(5, 30))
                             if answered else None),
            "dncVerbatim": CALL_SCRIPTS["DNC"][1][1] if key == "DNC" else None,
        }
        if answered:
            bhk = "3 BHK" if oc == "CONNECTED" else rng.choice(["3 BHK", "4 BHK"])
            budget = rng.choice(BUDGETS)
            prompt = int(rng.gauss(32750, 1500))
            doc.update(
                extractedSlots={"intent": CALL_INTENT[oc][0], "budget_bucket": budget if oc in WA_DOCS else None,
                                "configuration_interest": bhk if oc in WA_DOCS else None,
                                "timeline_bucket": {"PRE_SITE": "immediate", "CONNECTED": "3-6 months",
                                                    "VIRTUAL_TOUR": "6-12 months"}.get(oc),
                                "preferred_callback_time": "after 6 PM" if oc == "AUTO_CALLBACK" else None,
                                "objection_raised": ("do_not_call" if key == "DNC" else "already_purchased")
                                if oc == "NOT_INTERESTED" else None,
                                "site_visit_requested": {"date": _next_saturday_11am(t).astimezone(IST).strftime(
                                    "%Y-%m-%d"), "slot": "11:00"} if oc == "PRE_SITE" else None},
                userSignals={"is_qualified": oc in WA_DOCS, "is_likely_broker": False,
                             "language_detected": tx["language"].lower()},
                buyingSignals=[{"signal": CALL_INTENT[oc][5], "strength": CALL_INTENT[oc][1]}] if oc in WA_DOCS else [],
                leadTemperature={"PRE_SITE": "hot", "CONNECTED": "warm", "VIRTUAL_TOUR": "warm"}.get(oc, "cold"),
                objectionsHandled=["already_purchased"] if oc == "NOT_INTERESTED" else [],
                tokenUsage={"prompt": prompt, "response": rng.randint(150, 400)},
                whatsappSent=oc in WA_DOCS, whatsappDocs=WA_DOCS.get(oc, []),
                whatsappSentAt=t + timedelta(seconds=dur + 30) if oc in WA_DOCS else None,
                whatsappProject=tx["project"] if oc in WA_DOCS else None)
            doc["tokenUsage"]["total"] = doc["tokenUsage"]["prompt"] + doc["tokenUsage"]["response"]
            # the clean post-call transcription (Gemini in Zoho) that the CIF call-scoring reads:
            # the same script, stamped with where each line is heard in the recording
            offs = (RECORDING_TIMINGS.get(key) or {}).get("offsets") or [6.0 * n for n in range(len(CALL_SCRIPTS[key]))]
            out[("Zoho_Database", "call_transcripts")].append({
                "phone": tx["phone_number"], "call_sid": sid, "at": t, "outcome": oc, "model": "gemini-2.5-pro",
                "transcript": "\n".join(f"[{int(o) // 60:02d}:{int(o) % 60:02d}] "
                                        f"{'Agent' if role == 'agent' else 'Customer'}: {text}"
                                        for (role, text), o in zip(CALL_SCRIPTS[key], offs))})
            ci = CALL_INTENT[key]
            out[("ASBLVoiceBot", "call_transcript_intent_identification")].append({
                "call_sid": sid, "master_lead_id": lead["mlid"] if lead else None, "phone_number": tx["phone_number"],
                "created_at": t + timedelta(seconds=dur + 60),
                "intent": {"caller_intent": ci[0], "intent_strength": ci[1], "customer_sentiment": ci[2],
                           "conversation_stage": ci[3], "urgency": ci[4], "site_visit_outcome": ci[5],
                           "next_step": ci[6], "customer_engagement": ci[7], "talk_balance": ci[8],
                           "requirement_bhk": bhk if oc in WA_DOCS else None,
                           "budget_inr": budget if oc in WA_DOCS else None,
                           "objections": ("do_not_call" if key == "DNC" else "already_purchased")
                           if oc == "NOT_INTERESTED" else None,
                           "language": tx["language"].lower(), "call_summary": CALL_SUMMARY[key]}})
            for d in WA_DOCS.get(oc, []):
                blocked = rng.random() < 0.1
                out[("Zoho_Database", "doc_send_log")].append({
                    "phone": tx["phone_number"], "project": tx["project"], "doc_type": d, "call_sid": sid,
                    "created_at": t + timedelta(seconds=dur + 30), "outcome": "blocked" if blocked else "sent",
                    "block_reason": "outside the 24-hour WhatsApp window" if blocked else None,
                    "matched_file": None if blocked else f"{tx['project'].title()}_{d}.pdf"})
        if oc == "VIRTUAL_TOUR" and lead:
            tour = (t + timedelta(days=1)).astimezone(IST).replace(hour=19, minute=0, second=0, microsecond=0)
            out[("Zoho_Database", "site_visit_bookings")].append({
                "phone": lead["phone"], "zoho_lead_id": lead["zoho_lead_id"], "project": lead["project"],
                "call_sid": sid, "booked_at": t, "created_at": t, "scheduled_for": tour.astimezone(timezone.utc),
                "type": "virtual", "visit_date": tour.strftime("%Y-%m-%d"), "visit_time": "19:00",
                "visit_raw": {"date": "tomorrow", "time": "7 PM"},
                "status": "completed" if tour.astimezone(timezone.utc) < now else "booked", "source": "ai_call"})
            out[("Intelligent_CRM", "site_visits")].append({
                "person_id": f"P-{lead['mlid']}", "project": lead["project"], "booked_at": t, "created_at": t,
                "visit_at": tour.astimezone(timezone.utc), "scheduled_for": tour.astimezone(timezone.utc),
                "type": "virtual", "status": "visit_scheduled"})
        if oc == "AUTO_CALLBACK":
            out[("Zoho_Database", "callback_log")].append({
                "phone": tx["phone_number"], "call_sid": sid, "requested_at": t, "preferred_time": "after 6 PM",
                "status": "done" if t + timedelta(hours=8) < now else "scheduled"})
        if oc == "PRE_SITE" and lead:
            visit = _next_saturday_11am(t)
            done = lead["stage"] == "Post Site Visit"
            out[("Zoho_Database", "site_visit_bookings")].append({
                "phone": lead["phone"], "zoho_lead_id": lead["zoho_lead_id"], "project": lead["project"],
                "call_sid": sid, "booked_at": t, "created_at": t, "scheduled_for": visit, "type": "site",
                "visit_date": visit.astimezone(IST).strftime("%Y-%m-%d"), "visit_time": "11:00",
                "visit_raw": {"date": "Saturday", "time": "11 AM"},
                "status": "completed" if done else "booked", "source": "ai_call"})
            out[("Intelligent_CRM", "site_visits")].append({
                "person_id": f"P-{lead['mlid']}", "project": lead["project"], "booked_at": t, "visit_at": visit,
                "created_at": t, "scheduled_for": visit, "type": "site",
                "status": "visit_done" if done else "visit_scheduled"})
            for hours, label in ((12, "12h before visit"), (1, "1h before visit")):
                due = visit - timedelta(hours=hours)
                out[("angad", "scheduledcalls")].append({
                    "phone": tx["phone_number"], "slug": "site-visit-reminder", "kind": "reminder",
                    "status": "done" if due < now else "pending", "dueAt": due, "visitAt": visit, "attempts": int(due < now),
                    "label": label, "sourceCallId": sid, "createdAt": t})
        doc["callbackUrl"] = None
        out[("angad", "calls")].append(doc)
        if lead:
            out[("Intelligent_CRM", "lead_events")].append({
                "person_id": f"P-{lead['mlid']}", "type": "ai_call", "outcome": oc, "at": t, "call_sid": sid})
    calls_by_phone = {}
    for tx in transcripts:
        calls_by_phone.setdefault(tx["phone_number"][-10:], []).append(tx)
    ZOHO_EXPORT.clear()
    for z in zleads:
        p = z["phone"][-10:]
        cs = sorted(calls_by_phone.get(p, []), key=lambda x: x["started_at"])
        w = wa_by_phone.get(p, {}).get("by_date", {})
        replies = sum(len(d["inbound"]) for d in w.values())
        ZOHO_EXPORT.append({
            "Record Id": z["zoho_lead_id"], "Master Lead ID": z["mlid"], "Project Lead ID": z["plid"],
            "Phone Number": "+91" + p, "Lead Name": f"{z['first_name']} {z['last_name']}",
            "Stage": z["stage"], "Lead Status": z["stage"], "PRD Stage": z["prd_stage"],
            "ASBL Project": z["project"], "Lead Source": z["lead_source"], "UTM Source": z["utm_source"],
            "Campaign Name": z["campaign_name"], "Call Attempt Count": len(cs),
            "Call Connected Flag": str(any(x["transcript"] for x in cs)).lower(),
            "Last Call Disposition": cs[-1]["call_outcome"] if cs else "",
            "Do Not Call": str(p in dnc_phones).lower(),
            "Visit Attended": str(z["stage"] == "Post Site Visit").lower(),
            "Site Visit Date": z.get("site_visit_date", ""), "Not Interested Reason": z["not_interested_reason"] or "",
            "WhatsApp Reply Count": replies, "WA Replied Flag": str(replies > 0).lower(),
            # Zoho exports plain local (IST) times, no timezone
            "Born Date": z["born_date"], "Created Time": z["created_at"].astimezone(IST).strftime("%Y-%m-%d %H:%M:%S"),
            "Lead Received At": z["created_at"].astimezone(IST).strftime("%Y-%m-%d %H:%M:%S")})
    return out


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
    outs = {}
    for d in vb["call_transcripts"].find():
        outs.setdefault(d["phone_number"][-10:], set()).add(d["call_outcome"])
    assert all("PRE_SITE" in outs.get(lead["phone"][-10:], ()) for lead in z["leads"].find(
        {"prd_stage": {"$in": ["Pre Site Visit", "Post Site Visit"]}})), "booked visit without a booking call"
    assert ({d["roomName"] for d in c["angad"]["calls"].find()}
            == {d["call_sid"] for d in vb["call_transcripts"].find()}), "ANGAD and CRM must share the same calls"
    assert ({d["phone"][-10:] for d in z["site_visit_bookings"].find({"type": "site"})} == {d["phone"][-10:] for d in z["leads"].find(
        {"prd_stage": {"$in": ["Pre Site Visit", "Post Site Visit"]}})}), "bookings must match booked stages"
    urls = {r["recording_url"] for r in vb["call_recordings"].find()}
    assert all(os.path.exists(os.path.join(RECORDING_DIR, u.rsplit("/", 1)[1])) for u in urls), "missing recording file"
    assert set(RECORDING_TIMINGS) == set(CALL_SCRIPTS), "run make_recordings.py"
    assert len(BULK_SHEET) == N_BULK and vb["call_schedules"].count_documents({"call_id": {"$regex": "^BK-"}}) > 0
    print({f"{db}.{name}": c[db][name].count_documents({}) for db, name in [
        ("Zoho_Database", "leads"), ("Zoho_Database", "whatsapp_messages"), ("asbl_loft", "leads"),
        ("asbl_loft", "conversations"), ("ASBLVoiceBot", "call_transcripts"), ("ASBLVoiceBot", "call_recordings"),
        ("qa_results", "chatbot_qa"), ("qa_results", "voice_qa"), ("qa_results", "analytics_runs"),
        ("qa_results", "feedback"), ("qa_results", "recommendations"), ("qa_results", "health_scores")]})
    print("demo_data self-check passed")
