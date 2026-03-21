"""
This API endpoint provides a secure method for creating feedback entries.
It is designed to be called from the frontend when an anonymous or unauthenticated user
submits feedback. This endpoint uses a service-level Supabase client to bypass
Row Level Security (RLS) policies, ensuring that feedback can be created by any user.

Reminder lifecycle is handled by /v1/reminders/process (low-star skip, Google click,
non-follow-up templates) and by /mark-google-review-clicked — not by create-feedback.
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


def get_supabase_service_client():
    """Initializes and returns a Supabase client with the service role key."""
    supabase_url = db.secrets.get("SUPABASE_URL")
    service_key = db.secrets.get("SUPABASE_SERVICE_KEY")
    if not supabase_url or not service_key:
        raise HTTPException(status_code=500, detail="Supabase configuration missing.")
    return create_client(supabase_url, service_key)


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

        return response.data[0]

    except HTTPException:
        raise
    except Exception as e:
        print(f"Error creating feedback: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/mark-google-review-clicked")
def mark_google_review_clicked(
    body: MarkGoogleReviewClickedRequest,
    supabase: Client = Depends(get_supabase_service_client),
):
    """
    Cancels pending reminders and sets clicked_google_link on the client when the
    user follows the Google review CTA.
    """
    try:
        client_id = (body.client_id or "").strip()
        if not client_id:
            raise HTTPException(status_code=400, detail="client_id is required.")

        n = cancel_pending_reminders_for_client(supabase, client_id)
        # Do not chain .select() onto .update() — postgrest-py builds differ and often
        # raise: SyncFilterRequestBuilder has no attribute 'select'.
        supabase.from_("clients").update({"clicked_google_link": True}).eq(
            "id", client_id
        ).execute()

        verify = (
            supabase.from_("clients")
            .select("id, clicked_google_link")
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
        if not row.get("clicked_google_link"):
            print(
                f"mark_google_review_clicked: update did not persist for client_id={client_id!r}"
            )
            raise HTTPException(
                status_code=404,
                detail="Could not save clicked_google_link (column missing or RLS?).",
            )

        return {
            "message": "OK",
            "reminders_cancelled": n,
            "clicked_google_link": row.get("clicked_google_link"),
        }
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error in mark_google_review_clicked for {body.client_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))
