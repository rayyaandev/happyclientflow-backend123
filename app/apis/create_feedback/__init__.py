"""
This API endpoint provides a secure method for creating feedback entries.
It is designed to be called from the frontend when an anonymous or unauthenticated user
submits feedback. This endpoint uses a service-level Supabase client to bypass
Row Level Security (RLS) policies, ensuring that feedback can be created by any user.

Reminder lifecycle is handled by /v1/reminders/process (low-star skip, external click,
non-follow-up templates) and by /mark-external-review-clicked (and legacy
/mark-google-review-clicked) — not by create-feedback.
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import List, Optional
from supabase import Client, create_client
import databutton as db

from app.libs.reminder_scheduling import cancel_pending_reminders_for_client

router = APIRouter()


class FeedbackContent(BaseModel):
    products: Optional[List[str]] = None
    employee: Optional[str] = None
    other_product: Optional[str] = None
    other_employee: Optional[str] = None
    collaboration_feeling: Optional[str] = None
    highlight: Optional[str] = None
    improvements: Optional[str] = None


class CreateFeedbackRequest(BaseModel):
    client_id: str
    satisfaction: int
    recommendation: str
    content: FeedbackContent


class MarkGoogleReviewClickedRequest(BaseModel):
    client_id: str


class MarkExternalReviewClickedRequest(BaseModel):
    client_id: str
    profile_type: Optional[str] = None


def get_supabase_service_client():
    """Initializes and returns a Supabase client with the service role key."""
    supabase_url = db.secrets.get("SUPABASE_URL")
    service_key = db.secrets.get("SUPABASE_SERVICE_KEY")
    if not supabase_url or not service_key:
        raise HTTPException(status_code=500, detail="Supabase configuration missing.")
    return create_client(supabase_url, service_key)


def _record_external_review_platform_click(supabase: Client, client_id: str) -> dict:
    """
    Cancels pending reminders, sets clicked_google_link (any external review CTA),
    and sets review_status to ReviewComplete.
    """
    n = cancel_pending_reminders_for_client(supabase, client_id)
    supabase.from_("clients").update(
        {
            "clicked_google_link": True,
            "review_status": "ReviewComplete",
        }
    ).eq("id", client_id).execute()

    verify = (
        supabase.from_("clients")
        .select("id, clicked_google_link, review_status")
        .eq("id", client_id)
        .limit(1)
        .execute()
    )
    vdata = getattr(verify, "data", None) or []
    row = vdata[0] if vdata else None
    if not row:
        raise HTTPException(
            status_code=404,
            detail="Client not found after update.",
        )
    if not row.get("clicked_google_link") or row.get("review_status") != "ReviewComplete":
        print(
            f"mark_external_review_clicked: update did not persist for client_id={client_id!r} "
            f"row={row!r}"
        )
        raise HTTPException(
            status_code=404,
            detail="Could not save external review click (column missing or RLS?).",
        )

    return {
        "message": "OK",
        "reminders_cancelled": n,
        "clicked_google_link": row.get("clicked_google_link"),
        "review_status": row.get("review_status"),
    }


@router.post("/create-feedback")
def create_feedback(
    request: CreateFeedbackRequest,
    supabase: Client = Depends(get_supabase_service_client),
):
    """
    Creates a new feedback entry in the database.
    """
    try:
        feedback_insert_data = {
            "client_id": request.client_id,
            "satisfaction": request.satisfaction,
            "recommendation": request.recommendation,
            "content": request.content.model_dump(),
        }

        response = supabase.from_("feedback").insert(feedback_insert_data).execute()

        if not response.data:
            raise HTTPException(status_code=500, detail="Failed to create feedback entry.")

        # Anonymous feedback page cannot update clients via browser Supabase (RLS).
        # Service client sets survey-complete status here.
        cid = (request.client_id or "").strip()
        if cid:
            try:
                supabase.from_("clients").update(
                    {"review_status": "FeedbackSubmitted"}
                ).eq("id", cid).execute()
            except Exception as upd_err:
                # Do not fail the request: feedback row is already stored; client may retry and duplicate.
                print(
                    f"create_feedback: WARNING could not set FeedbackSubmitted for client_id={cid!r}: {upd_err}"
                )

        return response.data[0]

    except HTTPException:
        raise
    except Exception as e:
        print(f"Error creating feedback: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/mark-external-review-clicked")
def mark_external_review_clicked(
    body: MarkExternalReviewClickedRequest,
    supabase: Client = Depends(get_supabase_service_client),
):
    """
    Cancels pending reminders and sets clicked_google_link when the user opens any
    external review CTA (Google, Anwalt.de, Trustpilot, etc.).
    """
    try:
        client_id = (body.client_id or "").strip()
        if not client_id:
            raise HTTPException(status_code=400, detail="client_id is required.")

        if body.profile_type:
            print(
                f"mark_external_review_clicked: client_id={client_id!r} "
                f"profile_type={body.profile_type!r}"
            )

        return _record_external_review_platform_click(supabase, client_id)
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error in mark_external_review_clicked for {body.client_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/mark-google-review-clicked")
def mark_google_review_clicked(
    body: MarkGoogleReviewClickedRequest,
    supabase: Client = Depends(get_supabase_service_client),
):
    """
    Legacy alias for mark-external-review-clicked (Google-only clients).
    """
    try:
        client_id = (body.client_id or "").strip()
        if not client_id:
            raise HTTPException(status_code=400, detail="client_id is required.")

        return _record_external_review_platform_click(supabase, client_id)
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error in mark_google_review_clicked for {body.client_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))
