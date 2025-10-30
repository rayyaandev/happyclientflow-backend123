"""
This API endpoint provides a secure method for creating or updating a client
after they have given consent. It is designed to be called from the frontend
consent page. This endpoint uses a service-level Supabase client to bypass
Row Level Security (RLS) policies.
"""

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel
from typing import Optional
from supabase import Client, create_client
import databutton as db

router = APIRouter()

# --- Pydantic Models ---
class ClientData(BaseModel):
    company_id: str
    inviter_user_id: str
    title: Optional[str] = None
    email: str
    first_name: str
    last_name: str
    phone: Optional[str] = None
    preferred_contact_channel: str
    start_date: str
    product_id: Optional[str] = None
    review_status: Optional[str] = None

# --- Supabase Client Dependency ---
def get_supabase_service_client():
    """Initializes and returns a Supabase client with the service role key."""
    supabase_url = db.secrets.get("SUPABASE_URL")
    service_key = db.secrets.get("SUPABASE_SERVICE_KEY")
    if not supabase_url or not service_key:
        raise HTTPException(status_code=500, detail="Supabase configuration missing.")
    return create_client(supabase_url, service_key)

# --- API Endpoint ---
@router.post("/add-client-from-consent")
def add_client_from_consent(
    request: ClientData,
    response: Response,
    supabase: Client = Depends(get_supabase_service_client)
):
    """
    Creates a new client or updates an existing one from consent.
    """
    # Add CORS headers
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    
    try:
        # Check if a client with this email already exists for this company
        existing_client_response = supabase.from_("clients").select("id").eq("email", request.email).eq("company_id", request.company_id).execute()

        if existing_client_response.data:
            # UPDATE existing client
            existing_client_id = existing_client_response.data[0]['id']
            update_data = request.dict()
            update_data['has_given_consent'] = True
            update_response = supabase.from_("clients").update(update_data).eq("id", existing_client_id).execute()

            if update_response.data:
                return update_response.data[0]
            else:
                raise HTTPException(status_code=500, detail="Failed to update client from consent.")
        else:
            # INSERT new client
            insert_data = request.dict()
            insert_data['has_given_consent'] = True
            insert_data['is_auto_created'] = True
            insert_data['import_method'] = "manual"
            insert_response = supabase.from_("clients").insert(insert_data).execute()

            if insert_response.data:
                return insert_response.data[0]
            else:
                raise HTTPException(status_code=500, detail="Failed to add client from consent.")

    except Exception as e:
        print(f"Error in add_client_from_consent: {e}")
        raise HTTPException(status_code=500, detail=str(e))
