"""
This API endpoint provides a secure method for creating reminder entries.
It is designed to be called from the frontend or external systems to create
reminders for clients. This endpoint uses a service-level Supabase client to bypass
Row Level Security (RLS) policies, ensuring that reminders can be created by any authorized caller.

The endpoint checks if a client already has existing reminders and creates them if they don't exist.
"""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, EmailStr
from typing import List, Optional, Dict, Any
from supabase import Client, create_client
import databutton as db

from app.libs.reminder_scheduling import (
    filter_followup_templates,
    build_reminder_rows,
    insert_reminder_rows,
    cancel_pending_reminders_for_client,
    client_should_not_receive_followups,
)

router = APIRouter()

# --- Pydantic Models ---
class ReminderData(BaseModel):
    template_id: str
    scheduled_at: datetime
    client_email: str
    first_name: str
    last_name: Optional[str] = None
    company_name: str
    product_name: Optional[str] = None
    review_link: str
    title: Optional[str] = None

class CreateRemindersRequest(BaseModel):
    client_id: str
    company_id: str
    reminders: List[ReminderData] = [] # Made optional

class CheckRemindersRequest(BaseModel):
    client_id: str

# --- Supabase Client Dependency ---
def get_supabase_service_client():
    """Initializes and returns a Supabase client with the service role key."""
    supabase_url = db.secrets.get("SUPABASE_URL")
    service_key = db.secrets.get("SUPABASE_SERVICE_KEY")
    if not supabase_url or not service_key:
        raise HTTPException(status_code=500, detail="Supabase configuration missing.")
    return create_client(supabase_url, service_key)

# --- API Endpoints ---
@router.post("/create-reminders-if-not-exists")
def create_reminders_if_not_exists(
    request: CreateRemindersRequest,
    supabase: Client = Depends(get_supabase_service_client)
):
    """
    Creates reminder entries only if the client doesn't already have existing pending reminders.
    
    This is a convenience endpoint that combines the check and create operations.
    It will only create reminders if no pending reminders exist for the client.
    """
    try:
        # Low-satisfaction feedback: never schedule (and clear stray pending rows)
        if client_should_not_receive_followups(supabase, request.client_id):
            n = cancel_pending_reminders_for_client(supabase, request.client_id)
            return {
                "message": (
                    "Client has submitted below-threshold feedback; "
                    f"cancelled {n} pending reminder(s). No new reminders scheduled."
                ),
                "existing_reminders_count": 0,
                "created_reminders": [],
                "reminders_cancelled": n,
            }

        # Check for existing pending reminders
        existing_check_res = supabase.from_("reminders").select("id, scheduled_at, template_id").eq("client_id", request.client_id).eq("sent_status", "pending").execute()
        
        if existing_check_res.data and len(existing_check_res.data) > 0:
            return {
                "message": f"Client {request.client_id} already has existing pending reminders. No action taken.",
                "existing_reminders_count": len(existing_check_res.data),
                "existing_reminders": existing_check_res.data,
                "created_reminders": []
            }
        
        # --- Fetch required data ---
        # Fetch client data
        client_res = supabase.from_("clients").select("*").eq("id", request.client_id).single().execute()
        if not client_res.data:
            raise HTTPException(status_code=404, detail=f"Client with id {request.client_id} not found.")
        client = client_res.data

        # Fetch company data
        company_res = supabase.from_("companies").select("id, name, owner_id").eq("id", request.company_id).single().execute()
        if not company_res.data:
            raise HTTPException(status_code=404, detail=f"Company with id {request.company_id} not found.")
        company = company_res.data
        
        # Fetch formal reminder templates for the company
        templates_res = supabase.from_("message_templates").select("*").eq("company_id", request.company_id).eq("rule_type", "formal").execute()
        if not templates_res.data:
            return {
                "message": f"No formal reminder templates found for company {request.company_id}. No reminders created.",
                "created_reminders": []
            }
        templates = filter_followup_templates(templates_res.data)
        if not templates:
            return {
                "message": f"No follow-up reminder templates found for company {request.company_id} (outreach templates are excluded). No reminders created.",
                "created_reminders": [],
            }

        reminders_to_insert = build_reminder_rows(
            client=client, company=company, templates=templates
        )

        if not reminders_to_insert:
            return {
                "message": "No reminders to create.",
                "created_reminders": [],
            }

        created_reminders, error_detail = insert_reminder_rows(
            supabase, reminders_to_insert
        )
        if error_detail:
            raise HTTPException(status_code=500, detail=error_detail)

        return {
            "message": f"Successfully created {len(created_reminders or [])} new reminders for client {request.client_id}.",
            "existing_reminders_count": 0,
            "created_reminders_count": len(created_reminders or []),
            "created_reminders": created_reminders or [],
        }
            
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error in create_reminders_if_not_exists for client {request.client_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))
