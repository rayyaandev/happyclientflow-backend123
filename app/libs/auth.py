# src/app/libs/auth.py
"""
This library handles Supabase JWT token verification for FastAPI requests.
It provides functions to extract user information from a token and to require authentication for endpoints.
Used by API endpoints that need to be protected or need user context.
"""
import databutton as db
import jwt
from fastapi import Request, HTTPException
from typing import Optional

async def get_user_from_request(request: Request) -> Optional[str]:
    print("Attempting to get user from request...")
    try:
        auth_header = request.headers.get('Authorization')
        if not auth_header:
            print("No Authorization header found.")
            return None
        if not auth_header.startswith('Bearer '):
            print("Authorization header does not start with Bearer.")
            return None

        token = auth_header.split(' ')[1]
        print(f"Token found: {token[:30]}...")

        jwt_secret = db.secrets.get("SUPABASE_JWT_SIGNING_SECRET")
        if not jwt_secret:
            print("SUPABASE_JWT_SIGNING_SECRET not found.")
            return None

        print("Decoding token...")
        decoded_token = jwt.decode(token, jwt_secret, algorithms=['HS256'], audience='authenticated')
        user_id = decoded_token.get('sub')
        print(f"Token decoded successfully. User ID: {user_id}")
        return user_id
    except jwt.ExpiredSignatureError:
        print("JWT Error: ExpiredSignatureError")
        return None
    except jwt.InvalidAudienceError:
        print("JWT Error: InvalidAudienceError")
        return None
    except jwt.InvalidTokenError as e:
        print(f"JWT Error: InvalidTokenError - {e}")
        return None
    except Exception as e:
        print(f"An unexpected error occurred in get_user_from_request: {e}")
        return None

async def require_auth(request: Request) -> str:
    user_id = await get_user_from_request(request)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or missing authentication token")
    return user_id
