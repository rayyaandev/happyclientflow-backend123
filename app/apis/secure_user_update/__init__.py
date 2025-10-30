

"""
This API module provides a secure endpoint for updating a user's profile,
specifically after the initial registration process.

It is used by the Register.tsx page. When a new user signs up, the frontend
cannot directly update the user's role and company_id due to Row-Level Security (RLS)
policies. This endpoint provides a trusted, server-side mechanism to perform
that update using the Supabase service_role key, which bypasses RLS.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, UUID4
import databutton as db
from supabase.client import create_client, Client

router = APIRouter()

# --- Pydantic Models ---
class UserProfileUpdateRequest(BaseModel):
    user_id: UUID4
    role: str
    company_id: UUID4
    invite_token: str | None = None

class UserProfileUpdateResponse(BaseModel):
    message: str
    user_id: UUID4

# --- Supabase Client Initialization ---
def get_supabase_service_client() -> Client:
    """Initializes and returns a Supabase client with the service_role key."""
    supabase_url = db.secrets.get("SUPABASE_URL")
    service_key = db.secrets.get("SUPABASE_SERVICE_KEY")
    if not supabase_url or not service_key:
        raise HTTPException(status_code=500, detail="Supabase configuration missing.")
    return create_client(supabase_url, service_key)

# --- API Endpoint ---
@router.post("/secure-update-user", response_model=UserProfileUpdateResponse)
def secure_update_user_profile(
    request: UserProfileUpdateRequest,
    supabase: Client = Depends(get_supabase_service_client)
):
    """
    Updates a user's role and company_id in the public.users table.
    This endpoint uses the service_role key to bypass RLS policies,
    making it suitable for post-registration profile setup.
    """
    try:
        update_data = {
            "role": request.role,
            "company_id": str(request.company_id),
            "has_onboarded": False,
            "has_toured": False
        }

        # Perform the update
        result = (
            supabase.from_("users")
            .update(update_data)
            .eq("id", str(request.user_id))
            .execute()
        )
        
        # The V2 Python client wraps result in a list.
        # Check if any data was returned or if there was an error
        if not result.data:
            print(f"No user found with ID: {request.user_id}. Or no data was changed.")
            # We don't throw an error here, as the user might already exist
            # in a state that matches the update. We proceed as if successful.

    except Exception as e:
        print(f"Error updating user profile with service_role: {e}")
        raise HTTPException(
            status_code=500,
            detail="An unexpected error occurred while updating the user profile."
        )

    # After successful user profile update, update the invite status if a token is provided
    if request.invite_token:
        try:
            (
                supabase.from_("invites")
                .update({"status": "Accepted"}) # Timestamps are handled by DB trigger
                .eq("token", request.invite_token)
                .execute()
            )
        except Exception as e:
            # Log the error but do not raise an exception, as the primary user registration
            # was successful. This is a non-critical part of the flow.
            print(f"Non-critical error: Failed to update invite status for token {request.invite_token}: {e}")


    return UserProfileUpdateResponse(
        message="User profile updated successfully.",
        user_id=request.user_id
    )
