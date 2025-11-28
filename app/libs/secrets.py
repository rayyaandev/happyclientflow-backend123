"""
Secrets helper that works both locally (via environment variables) 
and in Databutton cloud (via db.secrets).
"""
import os


def get_secret(name: str) -> str | None:
    """
    Get a secret value. First checks environment variables,
    then falls back to Databutton secrets API if available.
    
    Args:
        name: The name of the secret (e.g., "SUPABASE_URL")
        
    Returns:
        The secret value or None if not found
    """
    # First, try environment variable
    value = os.environ.get(name)
    if value:
        return value
    
    # Fall back to Databutton secrets (for cloud deployment)
    try:
        import databutton as db
        return db.secrets.get(name)
    except Exception:
        return None

