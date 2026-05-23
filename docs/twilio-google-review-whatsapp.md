# Twilio WhatsApp: Google review follow-up templates

Google review reminders (`template_kind = google_review_followup`) must **not** reuse the same approved WhatsApp copy as “please complete our survey” messages, or clients will see duplicate-looking nudges after they already submitted feedback.

The backend reads **dedicated Content SIDs** from secrets/environment. If they are missing, it falls back to the existing survey reminder shells (legacy behavior).

## 1. Create two Content Templates in Twilio

In [Twilio Console](https://console.twilio.com/) → **Messaging** → **Content Template Builder** (or WhatsApp Templates):

Create **two** templates per language/formality you use (formal is required today; informal only if you send informal outreach).

### Suggested copy (formal, German)

**Template 1 — first Google nudge**

- Purpose: thank for feedback, ask to publish prepared review on Google.
- Example body (adjust to your brand; must match what you submit for WhatsApp approval):

  > Hallo {{2}} {{3}},
  >
  > vielen Dank für Ihr Feedback. Als Nächstes fehlt nur noch Ihre kurze Bewertung bei Google.
  >
  > {{1}}

**Template 2 — second Google nudge**

- Shorter reminder, same variable layout.

  > Hallo {{2}} {{3}},
  >
  > eine kurze Google-Bewertung würde uns sehr helfen. Vielen Dank!
  >
  > {{1}}

### Variable mapping (must match code)

The reminder processor sends `content_variables` as JSON with:

| Twilio placeholder | Value |
|--------------------|--------|
| `{{1}}` | `client_id` (UUID) — your approved template should build the feedback/review deep link from this, same as survey reminders today |
| `{{2}}` | Translated title (`Herr` / `Frau`) for formal, or `first_name` for informal |
| `{{3}}` | `last_name` for formal, or `company_name` for informal |
| `{{4}}` | `company_name` for formal only (if your template uses a 4th variable) |

Copy the **existing** survey reminder templates (`whatsapp_reminder1_formal_v2` / `whatsapp_reminder2_formal_v2`) as a starting point, then change the **wording** so it clearly refers to **Google / public review**, not “feedback survey”.

Submit both for **WhatsApp approval**.

## 2. Copy Content SIDs

After approval, open each template and copy its **Content SID** (starts with `HX…`).

## 3. Configure backend secrets

Add to Databutton secrets and/or deployment environment:

| Secret | Use |
|--------|-----|
| `TWILIO_WHATSAPP_GOOGLE_FOLLOWUP_1_FORMAL_SID` | First Google follow-up, formal |
| `TWILIO_WHATSAPP_GOOGLE_FOLLOWUP_2_FORMAL_SID` | Second Google follow-up, formal |
| `TWILIO_WHATSAPP_GOOGLE_FOLLOWUP_1_INFORMAL_SID` | Optional, informal |
| `TWILIO_WHATSAPP_GOOGLE_FOLLOWUP_2_INFORMAL_SID` | Optional, informal |

Existing Twilio credentials stay unchanged:

- `TWILIO_ACCOUNT_SID`
- `TWILIO_AUTH_TOKEN`
- `TWILIO_FROM_NUMBER` (WhatsApp-enabled sender)

## 4. Verify

1. Submit 5★ feedback for a test client with `companies.google_review_url` set.
2. Confirm in DB: survey reminders (`1./2. Erinnerung`) → `cancelled`; Google rows (`Bewertungs-Erinnerung`) → `pending`.
3. When due, process reminders (`POST /v1/reminders/process`) and confirm WhatsApp uses the **new** Content SIDs (Twilio message log → Content SID).
4. If secrets are unset, messages still send using survey reminder SIDs (fallback).

## 5. Email/SMS

Email and SMS already use `message_templates.body` with `{{google_review_link}}`. Only WhatsApp required separate Twilio templates.
