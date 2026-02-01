"""
This API endpoint provides a secure method for creating reminder entries.
It is designed to be called from the frontend or external systems to create
reminders for clients. This endpoint uses a service-level Supabase client to bypass
Row Level Security (RLS) policies, ensuring that reminders can be created by any authorized caller.

The endpoint checks if a client already has existing reminders and creates them if they don't exist.
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, EmailStr
from typing import List, Optional, Dict, Any
from supabase import Client, create_client
import databutton as db
from datetime import datetime, timezone, timedelta

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
        templates = templates_res.data

        # --- Prepare reminder data ---
        reminders_to_insert = []
        import os
        base_url = os.environ.get("FRONTEND_URL", "https://app.happyclientflow.de")
        review_link = f"{base_url}/feedback?client_id={client['id']}&company_name={company['name'].replace(' ', '%20')}"

        for template in templates:
            # Calculate scheduled_at based on template's rules
            send_value = template.get("scheduled_send_value")
            send_unit = template.get("scheduled_send_unit")
            delta = timedelta(days=0)  # Default to no delay

            if send_value is not None and send_unit:
                try:
                    value = int(send_value)
                    if send_unit == 'days':
                        delta = timedelta(days=value)
                    elif send_unit == 'hours':
                        delta = timedelta(hours=value)
                    elif send_unit == 'minutes':
                        delta = timedelta(minutes=value)
                except (ValueError, TypeError):
                    # If value is not a valid integer, skip this template or log it
                    print(f"Warning: Invalid scheduled_send_value '{send_value}' for template {template.get('id')}. Skipping.")
                    continue

            scheduled_at = datetime.now(timezone.utc) + delta
            
            reminder_insert_data = {
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
            reminders_to_insert.append(reminder_insert_data)
        
        # --- Insert Reminders ---
        if not reminders_to_insert:
            return {
                "message": "No reminders to create.",
                "created_reminders": []
            }

        insert_res = supabase.from_("reminders").insert(reminders_to_insert).execute()
        
        if insert_res.data:
            created_reminders = insert_res.data
            return {
                "message": f"Successfully created {len(created_reminders)} new reminders for client {request.client_id}.",
                "existing_reminders_count": 0,
                "created_reminders_count": len(created_reminders),
                "created_reminders": created_reminders
            }
        else:
            # Handle potential insertion error from Supabase
            error_detail = "Failed to create reminder entries in database."
            if hasattr(insert_res, 'error') and insert_res.error:
                error_detail = insert_res.error.message
            raise HTTPException(status_code=500, detail=error_detail)
            
    except Exception as e:
        print(f"Error in create_reminders_if_not_exists for client {request.client_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))
