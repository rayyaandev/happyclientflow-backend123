"""
This library contains authentication and authorization utilities for the application.
"""
from fastapi import Depends, HTTPException
import supabase
from app.libs.supabase_client import get_supabase_client
from app.libs.auth import get_user_from_request


async def require_superadmin(
    current_user_id: str = Depends(get_user_from_request),
    supabase_client: supabase.Client = Depends(get_supabase_client),
):
    """
    Dependency that requires the current user to be a superadmin.
    """
    try:
        user_profile = (
            supabase_client.from_("users")
            .select("role")
            .eq("id", current_user_id)
            .single()
            .execute()
        )
        userRole = user_profile.data.get("role").lower()
        if userRole != "superadmin":
            raise HTTPException(status_code=403, detail="Only superadmins can perform this action.")
    except Exception as e:
        print(e)
        raise HTTPException(status_code=403, detail="Could not verify user role.")
    
    return current_user_id
