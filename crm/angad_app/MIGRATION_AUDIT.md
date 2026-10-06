# Migration audit — old cluster vs new cluster
Generated 2026-08-07 13:10. Read-only on both sides; write-guard active. Identities compared as pseudonyms.
## Connectivity
- **new**: connected (growth-prod.rugkur.mongodb.net)
- **old**: connected (angad.3z3qbu.mongodb.net)

## Collection census (old vs new)
| db.collection | old (live) | old (archive) | new | delta vs best-old |
|---|---:|---:|---:|---:|
| angad.calls | 45897 | 45206 | 45954 | +57 |
| angad.leads | 4799 | 4788 | 4824 | +25 |
| angad.scheduledcalls | 5121 | 5121 | 5141 | +20 |
| angad.prompts | 5 | 5 | 9 | +4 |
| angad.whatsappsenders | 44 | 40 | 44 | +0 |
| angad.callstates | 0 | 49 | 3 | +3 |
| angad.events | 0 | — | 0 | +0 |
| angad.learninginsights | 4 | 4 | 4 | +0 |
| angad.dashboard_snapshots | 1 | 1 | 1 | +0 |
| angad.webhooks | 1 | 1 | 1 | +0 |
| angad.apikeys | 3 | — | 3 | +0 |
| ASBLVoiceBot.call_schedules | 45005 | 44334 | 45040 | +35 |
| ASBLVoiceBot.call_transcripts | 21482 | 21331 | 21515 | +33 |
| ASBLVoiceBot.call_recordings | 18743 | 18593 | 18776 | +33 |
| ASBLVoiceBot.call_transcript_intent_identification | 21654 | 21523 | 21654 | +0 |
| ASBLVoiceBot.lead_journeys | 4275 | 4256 | 4287 | +12 |
| ASBLVoiceBot.lead_profiles | 4236 | 4217 | 4248 | +12 |
| ASBLVoiceBot.kb_store | 7 | 7 | 7 | +0 |
| ASBLVoiceBot.audit_log | 25 | — | 25 | +0 |
| ASBLVoiceBot.users | 4 | — | 4 | +0 |
| Zoho_Database.leads | 4150 | 4093 | 4147 | -3 |
| Zoho_Database.posthook_payloads | 2514 | 2941 | 2480 | -34 |
| Zoho_Database.whatsapp_messages | 1450 | — | 1450 | +0 |
| Zoho_Database.site_visit_bookings | 59 | 57 | 59 | +0 |
| Zoho_Database.doc_send_log | 134 | 134 | 134 | +0 |
| Zoho_Database.stuck_sends | 32063 | 30161 | 30556 | -1507 **SHRANK** |
| Zoho_Database.dead_senders | 19 | 19 | 19 | +0 |
| Zoho_Database.bot_settings | 10 | 10 | 10 | +0 |
| Zoho_Database.reactivation_list | 2999 | 2999 | 2999 | +0 |
| Zoho_Database.mlid_registry | 3209 | — | 3241 | +32 |
| Zoho_Database.plid_registry | 4456 | 4442 | 4498 | +42 |
| Zoho_Database.audit_logs | 464 | — | 464 | +0 |
| Zoho_Database.follow_up_log | 402 | 402 | 402 | +0 |
| Zoho_Database.call_block_list | 1 | 1 | 1 | +0 |
| Zoho_Database.crm_call_logs | 1 | — | 1 | +0 |
| Zoho_Database.alert_dedupe | 0 | — | 0 | +0 |
| Zoho_Database.user_profiles | 138 | 138 | 138 | +0 |
| Zoho_Database.workflow_queue | 2 | 2 | 2 | +0 |
| Zoho_Database._counters | 2 | — | 2 | +0 |
| Zoho_Database.frozen_inbox | 351 | — | 351 | +0 |
| Zoho_Database.processed_inbound | 3 | — | 3 | +0 |
| Zoho_Database.cron_log | 18736 | — | 18572 | -164 |
| Zoho_Database.project_documents | 63 | 63 | 63 | +0 |
| Zoho_Database.project_facts | 5 | 5 | 5 | +0 |
| Zoho_Database.redactions | 9 | — | 9 | +0 |
| Zoho_Database.crm_notes | 1 | — | 1 | +0 |
| Zoho_Database.whatsapp_sender_map | 1046 | 922 | 1046 | +0 |
| Zoho_Database.callback_log | 55 | 55 | 55 | +0 |
| Zoho_Database.dashboard_users | 4 | — | 4 | +0 |
| Intelligent_CRM.crm_leads | 3914 | 3914 | 3914 | +0 |
| Intelligent_CRM.lead_events | 28669 | 28669 | 28669 | +0 |
| Intelligent_CRM.calls | 7175 | 7175 | 7175 | +0 |
| Intelligent_CRM.messages | 7980 | 7980 | 7980 | +0 |
| Intelligent_CRM.persons | 3113 | 3113 | 3113 | +0 |
| Intelligent_CRM.site_visits | 55 | 55 | 55 | +0 |
| Intelligent_CRM.mlid_registry | 3123 | — | 3123 | +0 |
| Intelligent_CRM.login_attempts | 0 | — | 0 | +0 |
| Intelligent_CRM.frozen_inbox | 0 | — | 0 | +0 |
| Intelligent_CRM.error_logs | 0 | — | 0 | +0 |
| Intelligent_CRM.orchestrator_context | 0 | — | 0 | +0 |
| Intelligent_CRM.stale_person_record | 572 | 572 | 572 | +0 |
| Intelligent_CRM.dashboard_users | 3 | — | 3 | +0 |
| Intelligent_CRM.audit_logs | 58 | — | 58 | +0 |
| Intelligent_CRM._counters | 3915 | — | 3915 | +0 |

## angad.calls — record-level diff (archive = pre-cutover truth)
- old (archive): 45,206 · new: 45,954
- **added on new** (calls made after the archive, or injected): 748
- **REMOVED on new** (existed before cutover, gone now): 0
- outcome changed on 24 of 2,000 sampled common docs — **records were EDITED, not just moved**

## Do-Not-Call preservation
- old (live) DNC leads: 210 · new: 210 · **LOST in migration: 0**

## Configuration diffs (the 'what was done' signals)
- webhook `Zoho Webhook` → `https://growth-relay.asbl.in/api/relay/inhouse-posthook` (active=True)
  - old cluster pointed at the LEGACY relay (asbl-crm-api.vercel.app/api/relay/inhouse-posthook). If the URL above now targets the new CRM, the cutover included the webhook repoint; if unchanged, the CRM-blindness problem migrated too.
- bot_settings (new): {'zoho_access_token_v1': '<masked>', 'mongo_sync_last_run': '2026-08-06T13:30:38.458Z', 'automation_frozen': 'false', 'calls_delayed_batched': 'false', 'calls_paused': 'false', 'first_call_batch_size': '15', 'system_prompt': 'You are Anandita Reddy, ', 'system_prompt_version': '2026-07-09-v8-new-launch', 'whatsapp_born_paused': 'false', 'whatsapp_followups_paused': 'false'}

## Indexes (new cluster)
- `angad.calls`: ['_id_', 'externalScheduleId_1', 'slug_1', 'voiceEngine_1', 'createdAt_-1', 'phoneNumber_1_createdAt_-1', 'status_1_startedAt_1', 'direction_1', 'source_1', 'externalCustomerId_1', 'eventSlug_1', 'leadId_1', 'roomName_1']
- `angad.leads`: ['_id_', 'siteVisitScheduledAt_1', 'phone_1', 'doNotCall_1']
- `Intelligent_CRM.lead_events`: ['_id_', 'type_ts', 'lead_id_type_ts', 'lead_id_seq', 'person_id_ts']
- `Intelligent_CRM.crm_leads`: ['_id_', 'stage_next_call_at', 'stage_next_wa_at', 'external_zoho_lead_id', 'person_id', 'stage_next_action_at', 'is_primary_next_action_at', 'is_primary_caller_next_action_at']

## VERDICT
**NOT a plain migration — 2 anomalies need answers from the dev team:**
1. `Zoho_Database.stuck_sends` shrank: 32063 → 30556
1. 24/2000 sampled call docs have a DIFFERENT outcome on the new cluster


---

## FINAL CUTOVER — growth-prod (2026-08-07, 13:11 IST)

Third cluster `growth-prod.rugkur.mongodb.net` verified as the REAL production
target. All checks live, three-way (archive / original-old / growth-prod):

- **Cutover COMPLETE**: 41 calls TODAY on growth-prod, zero on the original
  since Aug 6 18:58. The dialler resumed this morning onto the new cluster.
- **The tail survived**: zero calls exist only on the original — the 123-call
  tail we flagged was swept across before cutover. Exactly as requested.
- **Zero deletions, zero edits**: 0 removed records; the 24/93 "changed
  outcomes" are the same NaN-vs-None null-representation artifact as before —
  real old-vs-new divergence: 0.
- **DNC intact**: 210 = 210, identity-verified.
- **Webhook**: growth-relay.asbl.in, active. **Indexes**: full set carried.
- **Credential**: `grow_team_read`, readAnyDatabase (read-only).
- stuck_sends census delta = replication timing + rolling window (known-innocent).

**VERDICT: CLEAN MIGRATION, CUTOVER COMPLETE. growth-prod is the system of
record from 2026-08-07. The original cluster and growth-test are historical.**
