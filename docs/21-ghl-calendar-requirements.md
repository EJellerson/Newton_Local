# 21 — Future GoHighLevel calendar integration: requirements

**Status: requirements only (commission NEWTON_LOCAL_FISH_DRAMA3_001, §15).**
Nothing was implemented or tested. GoHighLevel (LeadConnector) was not
accessed, no credential was requested, and no credential store was
inspected. A later commission implements this inside Newton_Local.

## 1. Target

| Field | Value (from the operator) |
|---|---|
| Provider | GoHighLevel / LeadConnector |
| Business | the operator's business (named privately in the commission) |
| Location ID | given privately by the operator (commission §15); not in this repository |
| Calendar ID | given privately by the operator (commission §15); not in this repository |
| Credential | a private integration token the operator has already created. **It is not in this repository, was not requested, and must never be pasted into a conversation with an agent.** |

The location and calendar IDs grant no access, but this repository is public,
so they are kept out of it. The implementing commission reads them from a
machine-local file that `.gitignore` excludes (for example
`config/ghl.local.json`, beside `config/model-sources.local.json`; the ignore
rule is added with it).

## 2. Credential procedure (when commissioned)

1. **Loading.** Newton_Local reads the token from one local secret location
   only, chosen by the implementing commission. Recommended: the macOS
   Keychain (generic password, service `newton.ghl`, account = location ID),
   read with `security find-generic-password -w` by the calling process, so
   the token never sits in a file. Acceptable alternative: a file such as
   `~/.config/newton/ghl.token`, mode `0600`, in a `0700` directory, outside
   every repository.
2. **Explaining it.** The runbook tells the operator exactly which Keychain
   item (or file) to create and the one command to create it; nothing else.
3. **Entering it.** The operator enters the token privately, in their own
   terminal (for example `security add-generic-password -s newton.ghl -a
   <location ID> -w`, which prompts without echo). An agent never asks for
   it and never sees it.
4. **No display.** No command prints the token; status output shows only
   "token present" / "token missing" / "token rejected".
5. **No prompts.** The token is never placed in an LLM prompt, tool result,
   conversation or transcript; tools receive results, never credentials.
6. **No Git.** `.gitignore` covers the file location if a file is used; a
   pre-commit check refuses anything that looks like a bearer token.
7. **No logs.** HTTP logging redacts the `Authorization` header and never
   records request bodies containing personal data beyond what the audit
   trail needs (§5).
8. **Least privilege, minimal validation.** The integration token should
   carry only the calendar and contact scopes the features need. Access is
   validated with the smallest read-only call (for example reading this
   calendar's settings), never by creating anything.

## 3. Capabilities

| Capability | Requirement |
|---|---|
| Availability lookup | Free slots for this calendar in a date range, in an explicit IANA time zone; results reflect the calendar's own rules (business hours, slot length, buffers, minimum notice, maximum bookings per slot or day). |
| Appointment selection | The assistant offers at most a few concrete slots, in the caller's time zone, and confirms the chosen one back in words ("Thursday the 22nd at 2:30 in the afternoon, Eastern"). |
| Contact resolution | Find an existing contact by phone or e-mail before creating one; never create duplicates for the same person; never overwrite contact fields without confirmation. |
| Appointment creation | Create only after the person has confirmed the slot; include a title, the contact, start/end with time zone, and a note that the booking was made by Newton's AI assistant. |
| Booking confirmation | Read back the booked slot; optionally the provider's confirmation message, as configured in GHL. |
| Error handling | Slot no longer free → offer the next ones; token missing or rejected → say so plainly, book nothing, report to the operator; rate-limited or provider down → do not retry endlessly; tell the person they will be contacted. |
| Duplicate prevention | Idempotency key per call and slot (for example CallSid + start time); before creating, re-check free slots and look for an existing appointment for the same contact in that window; record the provider's appointment id. |
| Explicit authorization for writes | Every external write (create, reschedule, cancel, contact create/update) requires an authorization rule the operator configured in advance (for example "may book into calendar X during business hours for callers who asked to book"); anything outside the rule needs the operator's approval first. |

## 4. Scheduling correctness

* Store and compare times in UTC; present them in the person's zone; the
  calendar's own zone governs business hours.
* Handle daylight-saving transitions explicitly (a slot list spanning a DST
  change must not shift by an hour).
* Respect the calendar's slot interval, duration, buffers, notice period and
  booking limits as the provider reports them, rather than re-implementing
  them from assumptions.
* Re-validate a slot immediately before booking; the provider's answer wins.

## 5. Audit trail

For every external operation: time, operation, target ids, outcome, the call
or session it belonged to, and the authorization rule that allowed it. No
token, no full request bodies; personal data limited to what the operator
needs to follow up. Stored locally, privately, outside Git.

## 6. API details to confirm at implementation time

The LeadConnector (GoHighLevel v2) API exposes calendar endpoints for free
slots and appointments and contact search/upsert endpoints, authenticated
with a bearer token and an API version header. Exact paths, parameters,
version strings and scopes **must be taken from the official documentation
at implementation time**; this document deliberately does not fix them, and
nothing here was verified against the live API.

## 7. Dependencies on other work

* The voice gateway (docs/20) supplies the conversation; the calendar
  integration is one of its tools.
* Outbound calls that book appointments are subject to the compliance
  requirements in docs/20 §7 (disclosure, consent, calling rules).
