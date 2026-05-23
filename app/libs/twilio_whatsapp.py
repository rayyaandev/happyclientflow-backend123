"""
Twilio WhatsApp Content SID resolution for reminder processing.

Survey reminders use fixed SIDs in reminders/__init__.py.
Google review follow-ups prefer dedicated secrets (see docs/twilio-google-review-whatsapp.md)
and fall back to survey reminder shells when unset.
"""

from __future__ import annotations

import os
from typing import Optional

# Survey reminder shells (fallback for Google follow-up when dedicated SIDs unset)
SURVEY_REMINDER_1_FORMAL = "HX363218948b597c323bc628e54be1f9af"
SURVEY_REMINDER_2_FORMAL = "HX3dfb020601addbcbed02fe683439cd9c"
SURVEY_REMINDER_1_INFORMAL = "HXd8ffd916c5eddf9506e4f70a86d06fbe"
SURVEY_REMINDER_2_INFORMAL = "HX695b1182dfcb84dea5ece052e7e35614"


def _read_secret(name: str) -> Optional[str]:
    raw = os.environ.get(name)
    if raw and str(raw).strip():
        return str(raw).strip()
    try:
        import databutton as db

        val = db.secrets.get(name)
        if val and str(val).strip():
            return str(val).strip()
    except Exception:
        pass
    return None


def google_review_followup_whatsapp_content_sid(
    rule_type: str, *, slot_index: int
) -> str:
    """
    Content SID for google_review_followup WhatsApp sends.

    slot_index: 0 = first nudge, >= 1 = second (and any further) nudge.
  """
    use_second = slot_index >= 1
    rt = (rule_type or "formal").strip().lower()

    if rt == "informal":
        keys = (
            "TWILIO_WHATSAPP_GOOGLE_FOLLOWUP_1_INFORMAL_SID",
            "TWILIO_WHATSAPP_GOOGLE_FOLLOWUP_2_INFORMAL_SID",
        )
        fallbacks = (SURVEY_REMINDER_1_INFORMAL, SURVEY_REMINDER_2_INFORMAL)
    else:
        keys = (
            "TWILIO_WHATSAPP_GOOGLE_FOLLOWUP_1_FORMAL_SID",
            "TWILIO_WHATSAPP_GOOGLE_FOLLOWUP_2_FORMAL_SID",
        )
        fallbacks = (SURVEY_REMINDER_1_FORMAL, SURVEY_REMINDER_2_FORMAL)

    key = keys[1] if use_second else keys[0]
    fallback = fallbacks[1] if use_second else fallbacks[0]
    dedicated = _read_secret(key)
    if not dedicated:
        print(
            f"twilio_whatsapp: {key} not set; using survey reminder shell fallback"
        )
    return dedicated or fallback
