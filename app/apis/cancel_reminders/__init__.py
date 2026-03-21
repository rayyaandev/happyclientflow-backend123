"""
This API module provides an endpoint to cancel pending reminders for a client.
It is used after a client submits feedback to stop any further automated reminders from being sent.
The endpoint uses the Supabase service key to bypass RLS policies.
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
import databutton as db
from supabase import create_client, Client

from app.libs.reminder_scheduling import cancel_pending_reminders_for_client

# Supabase connection
def get_supabase_client() -> Client:
    """Initializes and returns a Supabase client using the service role key."""
    try:
        url = db.secrets.get("SUPABASE_URL")
        key = db.secrets.get("SUPABASE_SERVICE_KEY")
        if not url or not key:
            raise ValueError("Supabase URL or service key is missing.")
        return create_client(url, key)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to initialize Supabase client: {str(e)}")

router = APIRouter()

class CancelRemindersRequest(BaseModel):
    client_id: str

@router.post("/reminders/cancel")
def cancel_reminders(body: CancelRemindersRequest):
    """
    Cancels all pending reminders for a specific client by updating their
    `sent_status` to 'cancelled'.
    """
    supabase = get_supabase_client()
    client_id = body.client_id
    
    try:
        updated = cancel_pending_reminders_for_client(supabase, client_id)
        return {
            "message": "Reminders cancelled successfully.",
            "updated_count": updated,
        }

    except Exception as e:
        print(f"Error cancelling reminders for client_id {client_id}: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"An unexpected error occurred while cancelling reminders: {str(e)}"
        )
