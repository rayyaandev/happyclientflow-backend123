"""
This API is for admin-related actions that require elevated privileges. Mainly used in admindashboard
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
import supabase
from app.libs.auth_utils import require_superadmin
from app.libs.supabase_client import get_supabase_client

router = APIRouter()

class UpdateRoleRequest(BaseModel):
    user_id: str
    role: str

@router.post("/update-user-role-by-admin2", tags=["admin"])
async def update_user_role_by_admin2(
    request: UpdateRoleRequest,
    supabase_client: supabase.Client = Depends(get_supabase_client),
    current_user_id: str = Depends(require_superadmin),
):
    """
    Update a user's role. Only accessible by superadmins.
    The user_id of the user to be updated is in the request body.
    The current_user_id is the superadmin performing the action.
    """
    try:
        # The require_superadmin dependency already ensures the current user is a superadmin.
        # We can directly proceed with updating the target user's role.
        (
            supabase_client.from_("users")
            .update({"role": request.role})
            .eq("id", request.user_id)
            .execute()
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update user role: {str(e)}")

    return {"message": "User role updated successfully"}


class DeleteUserRequest(BaseModel):
    user_id: str


@router.post("/delete-user", tags=["admin"])
async def delete_user(
    request: DeleteUserRequest,
    supabase_client: supabase.Client = Depends(get_supabase_client),
    current_user_id: str = Depends(require_superadmin),
):
    """
    Delete a user. Only accessible by superadmins.
    """
    try:
        # Check if the user is a company owner
        companies = (
            supabase_client.from_("companies")
            .select("id")
            .eq("owner_id", request.user_id)
            .execute()
        )
        if companies.data:
            for company in companies.data:
                (
                    supabase_client.from_("companies")
                    .delete()
                    .eq("id", company["id"])
                    .execute()
                )

        # First, delete from the public users table
        (
            supabase_client.from_("users")
            .delete()
            .eq("id", request.user_id)
            .execute()
        )

        # Then, delete from auth.users
        supabase_client.auth.admin.delete_user(request.user_id)

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete user: {str(e)}")

    return {"message": "User deleted successfully"}


class DeleteCompanyRequest(BaseModel):
    company_id: str


@router.post("/delete-company", tags=["admin"])
async def delete_company(
    request: DeleteCompanyRequest,
    supabase_client: supabase.Client = Depends(get_supabase_client),
    current_user_id: str = Depends(require_superadmin),
):
    """
    Delete a company. Only accessible by superadmins.
    """
    try:
        (
            supabase_client.from_("companies")
            .delete()
            .eq("id", request.company_id)
            .execute()
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete company: {str(e)}")

    return {"message": "Company deleted successfully"}
