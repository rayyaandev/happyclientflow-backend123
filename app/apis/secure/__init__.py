# src/app/apis/secure/__init__.py
"""
This API module provides authentication-related endpoints for the Happy Client Flow app.
It includes token verification and secure endpoints that require valid Supabase JWT tokens.

Endpoints:
- /verify-token: Checks the validity of a JWT token from the Authorization header.
- /secure-data: An example endpoint that requires authentication to access.
- /health: A simple health check endpoint that requires authentication.
"""
import databutton as db
import jwt
from fastapi import Request, HTTPException, APIRouter, Depends
from typing import Optional
from pydantic import BaseModel
# Corrected import path for auth modules
from app.libs.auth import get_user_from_request, require_auth

router = APIRouter(prefix="/auth", tags=["Authentication"])

class TokenVerificationResponse(BaseModel):
    authenticated: bool
    user_id: Optional[str] = None
    message: str

@router.get("/verify-token", response_model=TokenVerificationResponse)
async def verify_token_endpoint(request: Request) -> TokenVerificationResponse:
    """
    Verifies the JWT token provided in the Authorization header.
    Returns whether the token is valid and the associated user ID if it is.
    """
    user_id = await get_user_from_request(request)
    if user_id:
        return TokenVerificationResponse(authenticated=True, user_id=user_id, message="Token is valid")
    else:
        return TokenVerificationResponse(authenticated=False, message="Invalid or missing token")

class SecureDataResponse(BaseModel):
    message: str
    user_id: str

@router.get("/secure-data", response_model=SecureDataResponse)
async def get_secure_data_endpoint(request: Request, user_id: str = Depends(require_auth)):
    """
    An example endpoint that is protected and requires a valid JWT token.
    Returns a message indicating the authenticated user's ID.
    """
    return SecureDataResponse(message="This is secure data accessible only by authenticated users.", user_id=user_id)

class HealthResponse(BaseModel):
    status: str
    message: str
    user_id: str

@router.get("/health", response_model=HealthResponse)
async def health_check_secure(user_id: str = Depends(require_auth)):
    """
    A simple health check endpoint that requires authentication.
    Used for testing the auth integration before securing other APIs.
    """
    return HealthResponse(
        status="ok", 
        message="Auth integration working correctly", 
        user_id=user_id
    )

