"""
Shared logic for which message_templates become scheduled reminders and how rows are inserted.

Goals:
- Do not schedule "outreach / invitation" templates as follow-up reminders (email chaos fix).
- Reuse the same insertion + timing rules from public_create_reminders and create_feedback.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

from supabase import Client


def feedback_high_satisfaction_min() -> int:
    """Stars at or above this value may receive post-survey follow-up reminders (default 5)."""
    try:
        return int(os.environ.get("FEEDBACK_HIGH_SATISFACTION_MIN", "5"))
    except ValueError:
        return 5


def get_latest_feedback_satisfaction(supabase: Client, client_id: str) -> Optional[int]:
    """Most recent feedback row for this client, or None."""
    res = (
        supabase.from_("feedback")
        .select("satisfaction")
        .eq("client_id", client_id)
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    if not res.data:
        return None
    return res.data[0].get("satisfaction")


def client_should_not_receive_followups(supabase: Client, client_id: str) -> bool:
    """True if the client already submitted feedback below the high-satisfaction threshold."""
    sat = get_latest_feedback_satisfaction(supabase, client_id)
    if sat is None:
        return False
    try:
        value = int(sat)
    except (TypeError, ValueError):
        return False
    return value < feedback_high_satisfaction_min()


def prefetch_latest_feedback_satisfaction(
    supabase: Client, client_ids: List[str]
) -> Dict[str, Optional[int]]:
    """Latest satisfaction per client_id for batch reminder processing."""
    ids = list({cid for cid in client_ids if cid})
    if not ids:
        return {}
    res = (
        supabase.from_("feedback")
        .select("client_id,satisfaction,created_at")
        .in_("client_id", ids)
        .execute()
    )
    rows = list(res.data or [])
    rows.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
    out: Dict[str, Optional[int]] = {}
    for r in rows:
        cid = r.get("client_id")
        if cid and cid not in out:
            out[cid] = r.get("satisfaction")
    return out


# If set on message_templates.template_kind (optional DB column), takes precedence.
_REMINDER_KIND = "reminder"
_OUTREACH_KINDS = frozenset(
    {"outreach", "invitation", "invite", "first_touch", "survey_invite", "initial"}
)

# When template_kind is absent, exclude templates whose name clearly looks like first-touch outreach.
_NAME_OUTREACH_MARKERS = (
    "einladung",
    "invitation",
    "outreach",
    "erste nachricht",
    "first message",
    "survey invite",
)

# When template_kind is absent, include templates whose name clearly looks like a follow-up reminder.
_NAME_REMINDER_MARKERS = (
    "erinnerung",
    "reminder",
    "follow-up",
    "follow up",
    "nudge",
)


def is_scheduled_followup_template(template: Dict[str, Any]) -> bool:
    """
    True if this formal template should produce scheduled reminder rows (not initial invite copy).

    Precedence: template_kind == reminder → include; known outreach kinds → exclude;
    then name markers; legacy ambiguous templates → include (backward compatible).
    """
    kind = (template.get("template_kind") or "").strip().lower()
    name = (template.get("name") or "").lower()
    if kind == _REMINDER_KIND:
        return True
    if kind in _OUTREACH_KINDS:
        return False
    for m in _NAME_OUTREACH_MARKERS:
        if m in name:
            return False
    for m in _NAME_REMINDER_MARKERS:
        if m in name:
            return True
    return True


def filter_followup_templates(templates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [t for t in templates if is_scheduled_followup_template(t)]


def cancel_pending_reminders_for_client(supabase: Client, client_id: str) -> int:
    """
    Set sent_status to 'cancelled' for all pending reminders for this client.
    Uses .select() so PostgREST returns updated rows and we get a reliable count.
    """
    # Avoid update().select().eq() — many postgrest-py versions break that chain.
    pending = (
        supabase.from_("reminders")
        .select("id")
        .eq("client_id", client_id)
        .eq("sent_status", "pending")
        .execute()
    )
    rows = getattr(pending, "data", None) or []
    if not rows:
        return 0
    supabase.from_("reminders").update({"sent_status": "cancelled"}).eq(
        "client_id", client_id
    ).eq("sent_status", "pending").execute()
    return len(rows)


def build_reminder_rows(
    *,
    client: Dict[str, Any],
    company: Dict[str, Any],
    templates: List[Dict[str, Any]],
    base_url: str = "https://app.happyclientflow.de",
) -> List[Dict[str, Any]]:
    """Build reminder insert dicts (same rules as historical public_create_reminders)."""
    review_link = (
        f"{base_url}/feedback?client_id={client['id']}"
        f"&company_name={company['name'].replace(' ', '%20')}"
    )

    templates_by_id = {t["id"]: t for t in templates}
    scheduled_times: Dict[str, Optional[datetime]] = {}

    def get_scheduled_at(template_id: str, visited: Optional[set] = None) -> Optional[datetime]:
        if visited is None:
            visited = set()

        if template_id in scheduled_times:
            return scheduled_times[template_id]

        if template_id in visited:
            return datetime.now(timezone.utc)

        visited.add(template_id)

        template = templates_by_id.get(template_id)
        if not template:
            return datetime.now(timezone.utc)

        base_time = datetime.now(timezone.utc)
        prev_id = template.get("previous_message_template_id")
        if prev_id and prev_id in templates_by_id:
            prev_time = get_scheduled_at(prev_id, visited)
            if prev_time:
                base_time = prev_time

        send_value = template.get("scheduled_send_value")
        send_unit = template.get("scheduled_send_unit")
        delta = timedelta(days=0)

        if send_value is not None and send_unit:
            try:
                value = int(send_value)
                if send_unit == "days":
                    delta = timedelta(days=value)
                elif send_unit == "hours":
                    delta = timedelta(hours=value)
                elif send_unit == "minutes":
                    delta = timedelta(minutes=value)
                elif send_unit == "seconds":
                    delta = timedelta(seconds=value)
            except (ValueError, TypeError):
                print(
                    f"Warning: Invalid scheduled_send_value '{send_value}' for template {template_id}. Skipping."
                )
                scheduled_times[template_id] = None
                return None

        scheduled_time = base_time + delta
        scheduled_times[template_id] = scheduled_time
        return scheduled_time

    rows: List[Dict[str, Any]] = []
    for template in templates:
        scheduled_at = get_scheduled_at(template.get("id"))
        if not scheduled_at:
            continue
        rows.append(
            {
                "author_id": company.get("owner_id"),
                "template_id": template.get("id"),
                "client_id": client.get("id"),
                "client_email": client.get("email"),
                "title": client.get("title"),
                "first_name": client.get("first_name"),
                "last_name": client.get("last_name"),
                "company_name": company.get("name"),
                "product_name": client.get("product_used"),
                "review_link": review_link,
                "scheduled_at": scheduled_at.isoformat(),
                "sent_status": "pending",
            }
        )
    return rows


def insert_reminder_rows(
    supabase: Client, rows: List[Dict[str, Any]]
) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
    """Insert reminder rows. Returns (data, error_message)."""
    if not rows:
        return [], None
    insert_res = supabase.from_("reminders").insert(rows).execute()
    if insert_res.data:
        return insert_res.data, None
    err = getattr(insert_res, "error", None)
    msg = err.message if err else "Failed to create reminder entries in database."
    return None, msg
