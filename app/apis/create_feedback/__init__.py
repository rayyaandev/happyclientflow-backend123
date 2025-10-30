"""
This API endpoint provides a secure method for creating feedback entries.
It is designed to be called from the frontend when an anonymous or unauthenticated user
submits feedback. This endpoint uses a service-level Supabase client to bypass
Row Level Security (RLS) policies, ensuring that feedback can be created by any user.

The endpoint receives feedback data, validates it, and inserts it into the 'feedback' table.
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any
from supabase import Client, create_client
import databutton as db

router = APIRouter()

# --- Pydantic Models ---
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

# --- Supabase Client Dependency ---
def get_supabase_service_client():
    """Initializes and returns a Supabase client with the service role key."""
    supabase_url = db.secrets.get("SUPABASE_URL")
    service_key = db.secrets.get("SUPABASE_SERVICE_KEY")
    if not supabase_url or not service_key:
        raise HTTPException(status_code=500, detail="Supabase configuration missing.")
    return create_client(supabase_url, service_key)

# --- API Endpoint ---
@router.post("/create-feedback")
def create_feedback(
    request: CreateFeedbackRequest,
    supabase: Client = Depends(get_supabase_service_client)
):
    """
    Creates a new feedback entry in the database.

    This endpoint is used to securely save feedback submitted by users.
    It leverages a service-level Supabase client to bypass RLS, allowing
    anonymous users to submit feedback.
    """
    try:
        feedback_insert_data = {
            "client_id": request.client_id,
            "satisfaction": request.satisfaction,
            "recommendation": request.recommendation,
            "content": request.content.dict(),
        }
        
        # Insert data into the 'feedback' table
        response = supabase.from_("feedback").insert(feedback_insert_data).execute()

        if response.data:
            inserted_record = response.data[0]
            return inserted_record
        else:
            raise HTTPException(status_code=500, detail="Failed to create feedback entry.")

    except Exception as e:
        print(f"Error creating feedback: {e}")
        raise HTTPException(status_code=500, detail=str(e))
