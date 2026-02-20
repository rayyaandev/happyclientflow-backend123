# src/app/apis/invites/__init__.py
# This API handles team member invitations, including creation, validation, and management.

import databutton as db
from fastapi import APIRouter, HTTPException, Body, Depends
from pydantic import BaseModel, EmailStr, Field
import uuid
import datetime
from typing import List, Optional
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail, From
from app.env import mode, Mode # Import current environment mode
from supabase import create_client, Client
from app.libs.auth import get_user_from_request # Assuming this handles user auth
from app.libs.auth_utils import require_admin  # Role-based access control
from app.libs.pricing_config import resolve_plan_from_lookup_key, PLANS

# Path for the registration page, ensure it matches frontend routing
REGISTER_PATH = "register" # e.g., app.com/register?token=xyz

router = APIRouter(prefix="/v1/invites", tags=["invites"])

# --- Pydantic Models ---
class InviteBase(BaseModel):
    email: EmailStr
    role: str = Field(default="ADMIN") # Default role, can be ADMIN or AGENT

class InviteCreateDataApi(InviteBase): # Used by the old direct API creation endpoint
    pass

class InviteRead(InviteBase):
    id: str
    token: str
    status: str
    company_id: str
    invited_by_user_id: str
    created_at: datetime.datetime
    expires_at: datetime.datetime

# Model for the new email sending endpoint
class SendInvitationEmailRequest(BaseModel):
    email: EmailStr
    role: str
    token: str
    companyName: Optional[str] = "Your Company"
    language: Optional[str] = "en"

class CreateAndSendInviteRequest(BaseModel):
    email: EmailStr
    role: Optional[str] = "ADMIN"
    language: Optional[str] = "en"
    frontendUrl: Optional[str] = None

class UserLimitStatus(BaseModel):
    allowed: bool
    max_users: int
    current_users: int
    plan_type: Optional[str] = None
    included_users: int = 0
    extra_seats: int = 0

class ValidateInviteRequest(BaseModel):
    token: str

class ValidateInviteResponse(BaseModel):
    valid: bool
    email: Optional[EmailStr] = None
    role: Optional[str] = None
    company_id: Optional[str] = None
    message: str

class ResponseMessage(BaseModel):
    message: str

# --- Supabase Integration ---
def get_supabase_client() -> Client:
    supabase_url = db.secrets.get("SUPABASE_URL")
    supabase_key = db.secrets.get("SUPABASE_SERVICE_KEY") # Use service key for backend operations
    if not supabase_url or not supabase_key:
        # This should ideally not happen if secrets are set
        print("CRITICAL ERROR: Supabase URL or Service Key not configured in secrets.")
        raise HTTPException(status_code=500, detail="Supabase connection details not configured.")
    return create_client(supabase_url, supabase_key)

# --- Helper Functions ---
def _get_base_url() -> str:
    """Get the frontend base URL for invitation links."""
    return "https://app.happyclientflow.de/"

def _send_actual_invitation_email(email_to: EmailStr, role: str, token: str, company_name: Optional[str], language: str):
    sendgrid_api_key = db.secrets.get("SENDGRID_API_KEY")
    sendgrid_from_email = "noreply@happyclientflow.de"

    if not sendgrid_api_key:
        print("ERROR: SendGrid API key not configured. Cannot send email.")
        return False

    app_base_url = _get_base_url()
    # Ensure no double slashes if REGISTER_PATH might start with one or app_base_url ends with one
    registration_link = f"{app_base_url.rstrip('/')}/{REGISTER_PATH.lstrip('/')}?token={token}"
    
    effective_company_name = company_name or "Happy Client Flow"

    if language == "de":
        print("German body email")
        invite_subject = f"Sie wurden eingeladen, {effective_company_name} beizutreten!"
        message_body = f"""
        <p>Hallo,</p>
        <br/>
        <p>Sie wurden eingeladen, {effective_company_name} auf Happy Client Flow als {role} beizutreten.</p>
        <br/>
        <p>Bitte klicken Sie auf den untenstehenden Link, um Ihre Registrierung abzuschließen:</p>
        <p><a href="{registration_link}">{registration_link}</a></p>
        <br/>
        <p>Dieser Link ist 14 Tage gültig.</p>
        <br/>
        <p>Wenn Sie diese Einladung nicht erwartet haben, können Sie diese E-Mail einfach ignorieren.</p>
        <br/>
        <p>Mit freundlichen Grüßen,<br/>Das {effective_company_name} Team</p>
        """
    else:
        print("English body email")
        invite_subject = f"You're invited to join {effective_company_name}!"
        message_body = f"""
        <p>Hello,</p>
        <br/>
        <p>You have been invited to join {effective_company_name} on Happy Client Flow as a(n) {role}.</p>
        <br/>
        <p>Please click the link below to complete your registration:</p>
        <p><a href="{registration_link}">{registration_link}</a></p>
        <br/>
        <p>This link is valid for 14 days.</p>
        <br/>
        <p>If you did not expect this invitation, you can safely ignore this email.</p>
        <br/>
        <p>Best regards,<br/>The {effective_company_name} Team</p>
        """

    message = Mail(
        from_email=From(sendgrid_from_email, "Happy Client Flow"),
        to_emails=email_to,
        subject=invite_subject,
        html_content=message_body
    )
    try:
        sg = SendGridAPIClient(sendgrid_api_key)
        response = sg.send(message)
        print(f"Invitation email sent to {email_to} for company '{effective_company_name}'. Link: {registration_link}. Status: {response.status_code}")
        return response.status_code in [200, 202] # HTTP 202 Accepted is common for email APIs
    except Exception as e:
        print(f"Error sending invitation email to {email_to} via SendGrid: {e}")
        return False

# --- API Endpoints ---

# NEW Endpoint specifically for sending email after frontend creates invite in Supabase
# This is what `inviteStore.ts` will call via the brain client.
@router.post("/send-email", response_model=ResponseMessage, name="send_invitation_email") # Explicit name for brain client
async def send_invitation_email_via_api(
    payload: SendInvitationEmailRequest = Body(...),
    current_user_id: str = Depends(require_admin)  # Only admins can send invites
):
    """
    Receives invite details (invite already created in Supabase by frontend) 
    and triggers sending the invitation email.
    This endpoint DOES NOT create or modify invite records in Supabase itself.
    Only admins can access this endpoint.
    """
    email_sent = _send_actual_invitation_email(
        email_to=payload.email,
        role=payload.role,
        token=payload.token,
        company_name=payload.companyName,
        language=payload.language
    )
    if not email_sent:
        # The invite exists in DB, but email failed. Frontend should be aware.
        raise HTTPException(status_code=502, detail="Invitation created, but failed to send invitation email.")
    
    return ResponseMessage(message="Invitation email dispatched successfully.")


LEGACY_UNLIMITED_PRICE_ID = "price_1Ro10tFS4l6OGNWUaMYBCOmn"


def _compute_user_limit(supabase: Client, company_id: str) -> dict:
    """Compute user limit status for a company based on its subscription."""
    # Get subscription
    sub_res = (
        supabase.table("subscriptions")
        .select("plan_type, extra_seats, included_users, status, stripe_price_id")
        .eq("company_id", company_id)
        .eq("status", "active")
        .maybe_single()
        .execute()
    )

    if not sub_res or not sub_res.data:
        # No active subscription — allow a generous default so free/trial users aren't blocked
        return {
            "allowed": True,
            "max_users": 1,
            "current_users": 0,
            "plan_type": None,
            "included_users": 1,
            "extra_seats": 0,
        }

    sub = sub_res.data
    plan_type = sub.get("plan_type")
    stripe_price_id = sub.get("stripe_price_id")
    included_users = sub.get("included_users") or 0
    extra_seats = sub.get("extra_seats") or 0

    # Legacy price: no seat limit enforcement
    if stripe_price_id == LEGACY_UNLIMITED_PRICE_ID:
        return {
            "allowed": True,
            "max_users": 999,
            "current_users": 0,
            "plan_type": plan_type,
            "included_users": 999,
            "extra_seats": 0,
        }

    # If included_users not stored on the row, resolve from plan config
    if not included_users and plan_type and plan_type in PLANS:
        included_users = PLANS[plan_type]["included_users"]

    max_users = included_users + extra_seats

    # Count current users + pending invites for this company
    users_res = supabase.table("users").select("id", count="exact").eq("company_id", company_id).execute()
    current_users = users_res.count or 0

    pending_res = supabase.table("invites").select("id", count="exact").eq("company_id", company_id).eq("status", "Pending").execute()
    pending_count = pending_res.count or 0

    total_current = current_users + pending_count

    return {
        "allowed": total_current < max_users,
        "max_users": max_users,
        "current_users": total_current,
        "plan_type": plan_type,
        "included_users": included_users,
        "extra_seats": extra_seats,
    }


@router.get("/user-limit-status", response_model=UserLimitStatus, name="get_user_limit_status")
async def get_user_limit_status(current_user: dict = Depends(get_user_from_request)):
    """Get the current user limit status for the authenticated user's company."""
    supabase = get_supabase_client()

    # get_user_from_request returns user_id string
    user_id = current_user if isinstance(current_user, str) else current_user.get("id")
    user_res = (
        supabase.table("users")
        .select("company_id")
        .eq("id", user_id)
        .maybe_single()
        .execute()
    )
    if not user_res or not user_res.data or not user_res.data.get("company_id"):
        raise HTTPException(status_code=403, detail="Company not found for user.")

    company_id = user_res.data["company_id"]
    return _compute_user_limit(supabase, company_id)


@router.post("/create-and-send", response_model=InviteRead, name="create_and_send_invite")
async def create_and_send_invite(
    payload: CreateAndSendInviteRequest = Body(...),
    current_user_id: str = Depends(require_admin),
):
    """
    Create an invitation and send the email in a single atomic operation
    with user limit enforcement.
    """
    supabase = get_supabase_client()

    # Resolve admin's company
    admin_res = (
        supabase.table("users")
        .select("company_id")
        .eq("id", current_user_id)
        .maybe_single()
        .execute()
    )
    if not admin_res or not admin_res.data or not admin_res.data.get("company_id"):
        raise HTTPException(status_code=403, detail="Company not found for user.")

    company_id = admin_res.data["company_id"]

    # Enforce user limit
    limit = _compute_user_limit(supabase, company_id)
    if not limit["allowed"]:
        raise HTTPException(status_code=403, detail={
            "error": "user_limit_reached",
            "max_users": limit["max_users"],
            "current_users": limit["current_users"],
        })

    # Check for duplicate pending invite
    dup_res = (
        supabase.table("invites")
        .select("id")
        .eq("company_id", company_id)
        .eq("email", payload.email)
        .eq("status", "Pending")
        .maybe_single()
        .execute()
    )
    if dup_res and dup_res.data:
        raise HTTPException(status_code=409, detail="A pending invitation already exists for this email.")

    # Normalise role
    role = payload.role.upper() if payload.role else "ADMIN"
    if role not in ("ADMIN", "TEAM_MEMBER"):
        role = "ADMIN"

    # Get company name for the email
    company_res = (
        supabase.table("companies")
        .select("name")
        .eq("id", company_id)
        .maybe_single()
        .execute()
    )
    company_name = company_res.data.get("name") if (company_res and company_res.data) else "Happy Client Flow"

    # Create invite record
    token = str(uuid.uuid4())
    now = datetime.datetime.now(datetime.timezone.utc)
    expires_at = now + datetime.timedelta(days=14)

    invite_data = {
        "email": payload.email,
        "role": role,
        "token": token,
        "status": "Pending",
        "company_id": company_id,
        "invited_by_user_id": current_user_id,
        "created_at": now.isoformat(),
        "expires_at": expires_at.isoformat(),
    }

    insert_res = supabase.table("invites").insert(invite_data).execute()
    if not insert_res.data:
        raise HTTPException(status_code=500, detail="Failed to create invitation record.")

    created_invite = insert_res.data[0]

    # Send email (non-blocking — invite is already persisted)
    email_sent = _send_actual_invitation_email(
        email_to=payload.email,
        role=role,
        token=token,
        company_name=company_name,
        language=payload.language or "en",
    )
    if not email_sent:
        print(f"WARNING: Invite {created_invite['id']} created but email failed to send.")

    return InviteRead(**created_invite)


@router.get("", response_model=List[InviteRead], name="get_all_invites_for_company")
async def get_all_invites(current_user: dict = Depends(get_user_from_request)):
    """
    Get all invites for the authenticated user's company.
    """
    supabase = get_supabase_client()
    company_id = current_user.get("company_id")
    if not company_id:
        raise HTTPException(status_code=403, detail="Company ID not found for user.")
    
    result = supabase.table("invites").select("id, email, role, token, status, company_id, invited_by_user_id, created_at, expires_at").eq("company_id", company_id).order("created_at", desc=True).execute()
    
    # Convert role and status to uppercase for consistency if they aren't already from DB
    # However, schema enforces uppercase for role, and frontend expects uppercase for status.
    return [InviteRead(**inv) for inv in result.data]

class RemoveTeamUserRequest(BaseModel):
    user_id: str

@router.post("/remove-team-user", response_model=ResponseMessage, name="remove_team_user_from_company")
async def remove_team_user(
    request: RemoveTeamUserRequest = Body(...), 
    current_user_id: str = Depends(require_admin)  # Only admins can remove team members
):
    """
    Remove a user from the company by setting their company_id to null.
    This preserves the user record but disassociates them from the company.
    Only admins from the same company can remove users.
    """
    supabase = get_supabase_client()
    if not current_user_id:
        raise HTTPException(status_code=401, detail="Unauthorized")

    # Load current user's company from DB
    current_user_res = (
        supabase
        .table("users")
        .select("id, company_id")
        .eq("id", current_user_id)
        .maybe_single()
        .execute()
    )

    if not current_user_res.data:
        raise HTTPException(status_code=403, detail="Authenticated user not found.")

    company_id = current_user_res.data.get("company_id")
    user_id = current_user_res.data.get("id")

    if not company_id:
        raise HTTPException(status_code=403, detail="Company ID not found for user.")

    # Verify the target user belongs to the same company and get their email
    target_user_res = supabase.table("users").select("id, company_id, email").eq("id", request.user_id).maybe_single().execute()
    
    if not target_user_res.data:
        raise HTTPException(status_code=404, detail="User not found.")
    
    if target_user_res.data.get("company_id") != company_id:
        raise HTTPException(status_code=403, detail="User does not belong to your company.")
    
    # Prevent removing yourself
    if request.user_id == user_id:
        raise HTTPException(status_code=400, detail="You cannot remove yourself from the company.")
    
    target_user_email = target_user_res.data.get("email")
    
    # Check for pending or accepted invites for this user's email and company
    # Order by created_at desc to get the most recent invite first
    invite_res = supabase.table("invites").select("id, status, email").eq("email", target_user_email).eq("company_id", company_id).order("created_at", desc=True).execute()
    
    if invite_res.data and len(invite_res.data) > 0:
        # Get the most recent invite (or first one if multiple)
        invite = invite_res.data[0]
        invite_status = invite.get("status", "").upper()
        
        if invite_status == "PENDING":
            # Delete the pending invite
            delete_result = supabase.table("invites").delete().eq("id", invite["id"]).execute()
            # Also remove company_id if user is already associated (edge case)
            if target_user_res.data.get("company_id"):
                supabase.table("users").update({"company_id": None}).eq("id", request.user_id).eq("company_id", company_id).execute()
            
            if delete_result.data:
                return ResponseMessage(message="Pending invitation removed successfully.")
            else:
                raise HTTPException(status_code=500, detail="Failed to delete pending invitation.")
        elif invite_status == "ACCEPTED":
            # Remove company association by setting company_id to null
            update_result = supabase.table("users").update({"company_id": None}).eq("id", request.user_id).eq("company_id", company_id).execute()
            
            if not update_result.data:
                raise HTTPException(status_code=500, detail="Failed to remove user from company.")
            
            return ResponseMessage(message="User removed from company successfully.")
        else:
            # For EXPIRED or other statuses, just remove company_id
            update_result = supabase.table("users").update({"company_id": None}).eq("id", request.user_id).eq("company_id", company_id).execute()
            
            if not update_result.data:
                raise HTTPException(status_code=500, detail="Failed to remove user from company.")
            
            return ResponseMessage(message="User removed from company successfully.")
    else:
        # No invite found, just remove company association (user might have been added directly)
        update_result = supabase.table("users").update({"company_id": None}).eq("id", request.user_id).eq("company_id", company_id).execute()
        
        if not update_result.data:
            raise HTTPException(status_code=500, detail="Failed to remove user from company.")
        
        return ResponseMessage(message="User removed from company successfully.")

@router.delete("/by-id/{invite_id}", response_model=ResponseMessage, name="delete_invite_from_company")
async def delete_invite(
    invite_id: str, 
    current_user_id: str = Depends(require_admin)  # Only admins can delete invites
):
    """
    Delete an invite, ensuring it belongs to the authenticated user's company.
    Only admins can delete invitations.
    """
    supabase = get_supabase_client()
    
    # First get the admin's company_id
    current_user_res = (
        supabase
        .table("users")
        .select("id, company_id")
        .eq("id", current_user_id)
        .maybe_single()
        .execute()
    )
    
    if not current_user_res.data:
        raise HTTPException(status_code=403, detail="Authenticated user not found.")
    
    company_id = current_user_res.data.get("company_id")
    user_id = current_user_res.data.get("id")

    if not company_id or not user_id:
        raise HTTPException(status_code=403, detail="User or Company ID not found.")
    
    # Check if invite exists and belongs to this company before deleting
    # Can also check if user is admin or the one who invited if stricter rules needed.
    invite_check_res = supabase.table("invites").select("id").eq("id", invite_id).eq("company_id", company_id).maybe_single().execute()

    if not invite_check_res.data:
        raise HTTPException(status_code=404, detail="Invitation not found or not associated with your company.")
    
    # Perform delete
    delete_result = supabase.table("invites").delete().eq("id", invite_id).eq("company_id", company_id).execute()
    
    # supabase-py delete doesn't raise error on 0 rows affected by default, check data if needed
    if not delete_result.data: # This usually means 0 rows affected, or error occurred
        # Check if error attribute exists for more details
        if hasattr(delete_result, 'error') and delete_result.error:
            print(f"Supabase delete error: {delete_result.error}")
            raise HTTPException(status_code=500, detail=f"Failed to delete invitation due to database error: {delete_result.error.message}")
        # If no error but no data, it might mean the condition didn't match (already deleted / concurrency)
        # For simplicity, we trust the prior check, but in robust systems, you might re-verify
        # For now, if it gets here and delete_result.data is empty but no error, assume it was fine or already gone.
        # However, a more robust check would be to see if the delete operation itself confirmed deletion (e.g. row count)
        print(f"Delete operation for invite {invite_id} completed. Result data: {delete_result.data}")
        
    return ResponseMessage(message="Invitation deleted successfully")

@router.post("/validate", response_model=ValidateInviteResponse, name="validate_invite_token_api")
async def validate_invite_token_endpoint(request: ValidateInviteRequest = Body(...)):
    """
    Validates an invitation token.
    Checks if the token exists, is PENDING, and not expired.
    Updates status to EXPIRED if applicable.
    """
    token_to_validate = request.token
    supabase = get_supabase_client()
    
    # Fetch the invite by token
    # Ensure select matches fields needed for InviteRead and logic here
    result = supabase.table("invites").select("id, email, role, token, status, company_id, invited_by_user_id, created_at, expires_at").eq("token", token_to_validate).maybe_single().execute()
    
    if not result.data:
        return ValidateInviteResponse(valid=False, message="Invalid invitation token.")
    
    invite = result.data

    # Status check (expecting PENDING)
    if invite["status"] != "Pending":
        return ValidateInviteResponse(valid=False, message=f"Invitation has already been {invite['status'].lower()}.")

    # Expiry check
    expires_at_str = invite["expires_at"]
    try:
        # Pydantic models expect datetime objects, but Supabase returns ISO strings.
        # Comparison needs consistent timezone awareness.
        expires_at_dt = datetime.datetime.fromisoformat(expires_at_str.replace('Z', '+00:00'))
        if expires_at_dt.tzinfo is None: # Should be set by replace, but double check
            expires_at_dt = expires_at_dt.replace(tzinfo=datetime.timezone.utc)
    except (ValueError, TypeError) as e:
        print(f"Error parsing expires_at '{expires_at_str}': {e}")
        raise HTTPException(status_code=500, detail="Invalid date format for 'expires_at' in database record.")

    if datetime.datetime.now(datetime.timezone.utc) > expires_at_dt:
        # Token expired, update status in DB
        update_payload = {"status": "Expired", "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}
        supabase.table("invites").update(update_payload).eq("token", token_to_validate).execute()
        return ValidateInviteResponse(valid=False, message="Invitation token has expired.")

    return ValidateInviteResponse(
        valid=True, 
        email=invite["email"],
        role=invite["role"],
        company_id=invite["company_id"],
        message="Invitation token is valid."
    )

@router.get("/health", name="health_check_invites")
async def health_check():
    return {"status": "ok", "message": "Invites API is healthy"}

# Ensure the file ends with a newline for POSIX compatibility and some linters
