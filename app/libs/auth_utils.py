"""
This library contains authentication and authorization utilities for the application.
"""
from fastapi import Depends, HTTPException
import supabase
from app.libs.supabase_client import get_supabase_client
from app.libs.auth import get_user_from_request

# ===============================================================================
# Role Hierarchy
# Higher number = more privileges
# Values must match the database enum exactly (uppercase)
# ===============================================================================
ROLE_LEVELS = {
    "TEAM_MEMBER": 1,
    "ADMIN": 2,
    "SUPERADMIN": 3,
}


async def _get_user_role(
    current_user_id: str,
    supabase_client: supabase.Client,
) -> str:
    """
    Helper function to fetch the user's role from the database.
    Returns the role in uppercase to match database enum.
    """
    if not current_user_id:
        raise HTTPException(status_code=401, detail="Authentication required.")
    
    try:
        user_profile = (
            supabase_client.from_("users")
            .select("role")
            .eq("id", current_user_id)
            .single()
            .execute()
        )
        role = user_profile.data.get("role")
        if not role:
            raise HTTPException(status_code=403, detail="User role not found.")
        return role.upper()
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error fetching user role: {e}")
        raise HTTPException(status_code=403, detail="Could not verify user role.")


async def require_admin(
    current_user_id: str = Depends(get_user_from_request),
    supabase_client: supabase.Client = Depends(get_supabase_client),
):
    """
    Dependency that requires the current user to be an admin or superadmin.
    Team members are NOT allowed.
    """
    user_role = await _get_user_role(current_user_id, supabase_client)
    user_level = ROLE_LEVELS.get(user_role, 0)
    required_level = ROLE_LEVELS.get("ADMIN", 2)
    
    if user_level < required_level:
        raise HTTPException(
            status_code=403, 
            detail="Only admins can perform this action."
        )
    
    return current_user_id


async def require_superadmin(
    current_user_id: str = Depends(get_user_from_request),
    supabase_client: supabase.Client = Depends(get_supabase_client),
):
    """
    Dependency that requires the current user to be a superadmin.
    """
    user_role = await _get_user_role(current_user_id, supabase_client)
    
    if user_role != "SUPERADMIN":
        raise HTTPException(
            status_code=403, 
            detail="Only superadmins can perform this action."
        )
    
    return current_user_id


async def require_team_member(
    current_user_id: str = Depends(get_user_from_request),
    supabase_client: supabase.Client = Depends(get_supabase_client),
):
    """
    Dependency that requires the current user to be at least a team member.
    Any authenticated user with a valid role (TEAM_MEMBER, ADMIN, SUPERADMIN) can access.
    """
    user_role = await _get_user_role(current_user_id, supabase_client)
    user_level = ROLE_LEVELS.get(user_role, 0)
    
    if user_level < 1:
        raise HTTPException(
            status_code=403, 
            detail="You do not have permission to perform this action."
        )
    
    return current_user_id
