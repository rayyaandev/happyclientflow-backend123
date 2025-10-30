"""
This API module provides an endpoint to cancel pending reminders for a client.
It is used after a client submits feedback to stop any further automated reminders from being sent.
The endpoint uses the Supabase service key to bypass RLS policies.
"""
from fastapi import APIRouter, HTTPException, Body
from pydantic import BaseModel
import databutton as db
from supabase import create_client, Client

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
        # Update reminders where client_id matches and status is 'pending'
        data, count = supabase.from_("reminders") \
            .update({"sent_status": "cancelled"}) \
            .eq("client_id", client_id) \
            .in_("sent_status", ["pending"]) \
            .execute()

        # The 'execute' method returns a tuple (data, count)
        # We can check the count to see how many rows were updated
        
        return {
            "message": "Reminders cancelled successfully.",
            "updated_count": len(data[1]) if data and len(data) > 1 else 0
        }

    except Exception as e:
        print(f"Error cancelling reminders for client_id {client_id}: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"An unexpected error occurred while cancelling reminders: {str(e)}"
        )
