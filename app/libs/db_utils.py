

# src/app/libs/db_utils.py
"""
This library provides database utility functions, such as creating a database connection
and fetching user profiles.
"""
import databutton as db
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.sql import text
from contextlib import asynccontextmanager
from fastapi import Depends, HTTPException
from app.libs.auth import require_auth

def get_db_connection_string():
    return f"postgresql+asyncpg://{db.secrets.get('SUPABASE_DB_USER')}:{db.secrets.get('SUPABASE_DB_USER_PASSWORD')}@{db.secrets.get('SUPABASE_URL')}"

@asynccontextmanager
async def get_db_connection():
    engine = create_async_engine(get_db_connection_string())
    async with engine.connect() as connection:
        try:
            yield connection
        finally:
            await connection.close()

async def get_user_profile_from_db(user_id: str):
    async with get_db_connection() as conn:
        result = await conn.execute(
            text("SELECT id, role, company_id, email, first_name, last_name FROM user_profiles WHERE id = :user_id"),
            {"user_id": user_id}
        )
        profile = await result.first()
    if not profile:
        return None
    return profile._asdict()

async def get_current_user_profile(user_id: str = Depends(require_auth)):
    user_profile = await get_user_profile_from_db(user_id)
    if not user_profile:
        raise HTTPException(status_code=404, detail="User profile not found")
    return user_profile
