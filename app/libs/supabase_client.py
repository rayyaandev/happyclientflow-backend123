"""
This library provides a Supabase client for interacting with the database.
"""
from fastapi import HTTPException
import databutton as db
from supabase import create_client, Client


def get_supabase_client() -> Client:
    """
    Returns a Supabase client configured with the service key.
    """
    supabase_url = db.secrets.get("SUPABASE_URL")
    supabase_key = db.secrets.get("SUPABASE_SERVICE_KEY")
    if not supabase_url or not supabase_key:
        raise HTTPException(status_code=500, detail="Supabase connection details not configured.")
    return create_client(supabase_url, supabase_key)
