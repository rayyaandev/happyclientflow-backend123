"""
Schedule/cancel reminders for template_kind=google_review_followup (after internal feedback,
nudge to publish on Google). Not part of the survey / outreach reminder batch.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from supabase import Client

from app.libs.reminder_scheduling import (
    GOOGLE_REVIEW_FOLLOWUP_KIND,
    feedback_high_satisfaction_min,
)

# Match SendReviewRequestDialog / public feedback links
def _feedback_public_url(client_id: str) -> str:
    base = (
        os.environ.get("FRONTEND_PUBLIC_URL")
        or os.environ.get("VITE_FRONTEND_URL")
        or "https://app.happyclientflow.de"
    ).rstrip("/")
    return f"{base}/feedback?c={client_id}"


def has_pending_google_review_followup_reminders(
    supabase: Client, client_id: str
) -> bool:
    pending = (
        supabase.from_("reminders")
        .select("id, template_id")
        .eq("client_id", client_id)
        .eq("sent_status", "pending")
        .execute()
    )
    rows = getattr(pending, "data", None) or []
    for row in rows:
        tid = row.get("template_id")
        if not tid:
            continue
        tres = (
            supabase.from_("message_templates")
            .select("template_kind")
            .eq("id", tid)
            .maybe_single()
            .execute()
        )
        kind = ((tres.data or {}).get("template_kind") or "").strip().lower()
        if kind == GOOGLE_REVIEW_FOLLOWUP_KIND:
            return True
    return False


def cancel_pending_google_review_followup_reminders(
    supabase: Client, client_id: str
) -> int:
    """Cancel pending reminders whose template has template_kind=google_review_followup."""
    pending = (
        supabase.from_("reminders")
        .select("id, template_id")
        .eq("client_id", client_id)
        .eq("sent_status", "pending")
        .execute()
    )
    rows = getattr(pending, "data", None) or []
    cancelled = 0
    for row in rows:
        tid = row.get("template_id")
        if not tid:
            continue
        tres = (
            supabase.from_("message_templates")
            .select("template_kind")
            .eq("id", tid)
            .maybe_single()
            .execute()
        )
        kind = ((tres.data or {}).get("template_kind") or "").strip().lower()
        if kind != GOOGLE_REVIEW_FOLLOWUP_KIND:
            continue
        supabase.from_("reminders").update({"sent_status": "cancelled"}).eq(
            "id", row["id"]
        ).execute()
        cancelled += 1
    return cancelled


def schedule_google_review_followup_reminders_after_feedback(
    supabase: Client,
    *,
    client_id: str,
    satisfaction: int,
) -> None:
    """
    After feedback is stored: if high satisfaction, company has google_review_url,
    client has not published on Google, and no pending google-review follow-ups,
    insert two reminder rows (same timing as survey reminders: +1d, +2d from now).
    """
    try:
        min_stars = feedback_high_satisfaction_min()
        if int(satisfaction) < min_stars:
            return

        cres = (
            supabase.from_("clients")
            .select(
                "id, company_id, email, title, first_name, last_name, "
                "google_review_published, product_id"
            )
            .eq("id", client_id)
            .maybe_single()
            .execute()
        )
        client = cres.data
        if not client:
            return
        if client.get("google_review_published"):
            return

        company_id = client.get("company_id")
        if not company_id:
            return

        co_res = (
            supabase.from_("companies")
            .select("id, name, owner_id, google_review_url")
            .eq("id", company_id)
            .maybe_single()
            .execute()
        )
        company = co_res.data
        if not company:
            return

        google_url = (company.get("google_review_url") or "").strip()
        if not google_url:
            return

        owner_id = company.get("owner_id")
        if not owner_id:
            return

        if has_pending_google_review_followup_reminders(supabase, client_id):
            return

        tres = (
            supabase.from_("message_templates")
            .select("*")
            .eq("company_id", company_id)
            .eq("template_kind", GOOGLE_REVIEW_FOLLOWUP_KIND)
            .eq("rule_type", "formal")
            .execute()
        )
        templates: List[Dict[str, Any]] = list(tres.data or [])
        if len(templates) < 1:
            print(
                f"schedule_google_review_followup: no google_review_followup templates "
                f"for company {company_id}"
            )
            return

        templates.sort(key=lambda t: (t.get("name") or ""))

        review_link = _feedback_public_url(client_id)
        company_name = company.get("name") or ""
        product_name = "Product Name"
        now = datetime.now(timezone.utc)

        rows_to_insert: List[Dict[str, Any]] = []
        for index, tmpl in enumerate(templates):
            delay = int(tmpl.get("scheduled_send_value") or 1)
            if index == 1:
                delay *= 2
            unit = (tmpl.get("scheduled_send_unit") or "days").lower()
            if unit in ("day", "days"):
                delta = timedelta(days=delay)
            elif unit in ("hour", "hours"):
                delta = timedelta(hours=delay)
            elif unit in ("minute", "minutes"):
                delta = timedelta(minutes=delay)
            elif unit in ("second", "seconds"):
                delta = timedelta(seconds=delay)
            else:
                delta = timedelta(days=delay)

            scheduled_at = now + delta
            rows_to_insert.append(
                {
                    "author_id": owner_id,
                    "template_id": tmpl["id"],
                    "client_id": client_id,
                    "client_email": client.get("email") or "",
                    "title": client.get("title"),
                    "first_name": client.get("first_name"),
                    "last_name": client.get("last_name"),
                    "company_name": company_name,
                    "product_name": product_name,
                    "review_link": review_link,
                    "google_review_link": google_url,
                    "scheduled_at": scheduled_at.isoformat(),
                    "sent_status": "pending",
                }
            )

        if rows_to_insert:
            supabase.from_("reminders").insert(rows_to_insert).execute()
            print(
                f"schedule_google_review_followup: scheduled {len(rows_to_insert)} "
                f"reminder(s) for client {client_id}"
            )
    except Exception as exc:
        print(f"schedule_google_review_followup_after_feedback: non-fatal: {exc}")
