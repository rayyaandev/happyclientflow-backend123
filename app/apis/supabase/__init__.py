"""
This API module handles the connection to the Supabase PostgreSQL database
using SQLAlchemy and provides endpoints for executing raw SQL queries and
checking the connection status.
"""

from fastapi import APIRouter, HTTPException, Body, Depends
from pydantic import BaseModel, Field
from typing import List, Dict, Any, Optional
from sqlalchemy import create_engine, text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from contextlib import contextmanager
import databutton as db
from app.libs.auth import require_auth # Fixed import path to match current auth module location
import os
from urllib.parse import quote_plus

# Create API router
router = APIRouter(prefix="/database", tags=["Database"])

# =====================
# Database Connection
# =====================

# Get Supabase PostgreSQL connection details from secrets
def get_connection_url():
    """Constructs the database connection URL from secrets."""
    try:
        db_host = db.secrets.get("SUPABASE_DB_HOST")
        # Use a default port if not explicitly set or invalid
        try:
            db_port = int(db.secrets.get("SUPABASE_DB_PORT"))
        except (KeyError, ValueError, TypeError) as e:
            print(f"Warning: Using default PostgreSQL port 5432. Could not read or parse SUPABASE_DB_PORT secret: {e}")
            db_port = 5432  # Default PostgreSQL port
        db_name = db.secrets.get("SUPABASE_DB_NAME")
        db_user = db.secrets.get("SUPABASE_DB_USER")
        db_password = db.secrets.get("SUPABASE_DB_PASSWORD")
        
        if not all([db_host, db_port, db_name, db_user, db_password]):
             raise ValueError("One or more Supabase database connection secrets are missing.")

        # URL-encode the password to handle special characters
        encoded_password = quote_plus(db_password)

        return f"postgresql://{db_user}:{encoded_password}@{db_host}:{db_port}/{db_name}"
    except Exception as e:
        print(f"Error getting database connection details: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to retrieve database connection secrets: {e}")

# Create SQLAlchemy engine with connection pooling
def create_db_engine():
    """Creates a SQLAlchemy engine with connection pooling."""
    try:
        connection_url = get_connection_url()
        # Configure engine with connection pooling
        # pool_pre_ping checks connection validity before use
        return create_engine(
            connection_url,
            pool_size=5,          # Number of permanent connections
            max_overflow=10,      # Max extra connections allowed
            pool_timeout=30,      # Seconds to wait for a connection
            pool_recycle=1800,    # Recycle connections every 30 mins
            pool_pre_ping=True,   # Check connection health before use
            echo=False            # Set True for SQL query debugging
        )
    except Exception as e:
        print(f"Error creating database engine: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to create database engine: {e}")

# Initialize engine globally but carefully
try:
    db_engine = create_db_engine()
except Exception as e:
    db_engine = None # Allow app to start, endpoints will fail gracefully
    print(f"Initial database engine creation failed: {e}. Endpoints requiring DB will likely fail.")


# Create declarative base for models (if needed in the future)
Base = declarative_base()

# Create session factory
def get_session_factory():
    """Creates a session factory bound to the engine."""
    global db_engine
    if db_engine is None:
        # Attempt to recreate engine if initial creation failed
        try:
            print("Attempting to re-initialize database engine...")
            db_engine = create_db_engine()
            print("Database engine re-initialized successfully.")
        except Exception as e:
            print(f"Re-initialization of database engine failed: {e}")
            raise HTTPException(status_code=500, detail="Database engine is not available.")

    try:
        return sessionmaker(autocommit=False, autoflush=False, bind=db_engine)
    except Exception as e:
        print(f"Error creating session factory: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to create database session factory: {e}")

# Context manager for database sessions
@contextmanager
def get_db_session():
    """Provides a transactional scope around a series of operations."""
    session_factory = get_session_factory() # Raises HTTPException if factory creation fails
    db_session = session_factory()
    try:
        yield db_session
        db_session.commit()
    except Exception as e:
        db_session.rollback()
        print(f"Database session error: {e}")
        # Re-raise as HTTPException for FastAPI to handle
        raise HTTPException(status_code=500, detail=f"Database operation failed: {e}")
    finally:
        db_session.close()

# Function to execute raw SQL queries
def execute_sql_query(query_str: str, params: Optional[Dict[str, Any]] = None, fetch_all: bool = True):
    """Executes a raw SQL query safely using SQLAlchemy session.

    Args:
        query_str (str): SQL query string.
        params (Optional[Dict[str, Any]]): Parameters for the query.
        fetch_all (bool): True to fetch all results, False to fetch one.

    Returns:
        Union[List[Dict[str, Any]], Dict[str, Any], None]: Query results.

    Raises:
        HTTPException: If the query execution fails.
    """
    with get_db_session() as session: # Handles session lifecycle and exceptions
        try:
            query = text(query_str)
            result_proxy = session.execute(query, params or {})

            if result_proxy.returns_rows:
                column_names = list(result_proxy.keys())
                if fetch_all:
                    rows = result_proxy.fetchall()
                    return [dict(zip(column_names, row)) for row in rows]
                else:
                    row = result_proxy.fetchone()
                    return dict(zip(column_names, row)) if row else None
            else:
                # For INSERT, UPDATE, DELETE that don't return rows explicitly
                # result_proxy.rowcount might be useful depending on dialect
                return [] # Return empty list for consistency if no rows are returned

        except Exception as e:
            print(f"Error executing SQL query: {query_str} | Params: {params} | Error: {e}")
            # Let the context manager handle rollback and raise HTTPException
            raise # Re-raise the exception caught by the context manager


# Function to test database connection
def test_connection():
    """Tests the database connection by executing a simple query."""
    try:
        # Use a non-modifying, simple query
        result = execute_sql_query("SELECT 1 as connection_test", fetch_all=False)
        if result and result.get("connection_test") == 1:
            return {"status": "connected", "details": "Successfully executed test query."}
        else:
             return {"status": "error", "details": f"Test query did not return expected result: {result}"}
    except HTTPException as http_exc: # Catch specific HTTP exceptions from execute_sql_query
        return {"status": "error", "details": f"HTTP Error during connection test: {http_exc.detail}"}
    except Exception as e: # Catch any other unexpected errors
        return {"status": "error", "details": f"Unexpected error during connection test: {str(e)}"}


# =====================
# API Models
# =====================

class SQLQueryRequest(BaseModel):
    query: str = Field(..., description="SQL query to execute", example="SELECT * FROM users WHERE id = :user_id")
    params: Optional[Dict[str, Any]] = Field(default=None, description="Parameters for the SQL query", example={"user_id": 1})
    fetch_all: bool = Field(default=True, description="Whether to fetch all results or just one row")

class SQLQueryResponse(BaseModel):
    results: Optional[List[Dict[str, Any]]] = Field(default=None, description="Query results")
    rowCount: Optional[int] = Field(default=None, description="Number of rows returned (if applicable)")
    error: Optional[str] = Field(default=None, description="Error message if query failed")

class ConnectionStatusResponse(BaseModel):
    status: str = Field(..., description="Connection status (connected/error)")
    details: Optional[Any] = Field(default=None, description="Additional details about the connection or error")



# =====================
# API Endpoints
# =====================

@router.post("/execute-query", response_model=SQLQueryResponse)
async def execute_query_endpoint(request: SQLQueryRequest = Body(...)):
    """
    Execute an arbitrary SQL query against the configured Supabase PostgreSQL database.
    Use parameters (:param_name) in the query string for safety.
    """
    try:
        results = execute_sql_query(
            query_str=request.query,
            params=request.params,
            fetch_all=request.fetch_all
        )

        response_data = {"error": None}
        if isinstance(results, list):
            response_data["results"] = results
            response_data["rowCount"] = len(results)
        elif isinstance(results, dict): # fetch_all=False returned one row
             response_data["results"] = [results]
             response_data["rowCount"] = 1
        else: # fetch_all=False returned None or DML statement executed
             response_data["results"] = []
             response_data["rowCount"] = 0

        return SQLQueryResponse(**response_data)

    except HTTPException as http_exc:
         # Forward HTTP exceptions from lower layers
         raise http_exc
    except Exception as e:
        # Catch unexpected errors during endpoint logic
        print(f"Error in execute_query_endpoint: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to execute query: {str(e)}")


@router.get("/connection-status", response_model=ConnectionStatusResponse)
async def connection_status_endpoint(user_id: str = Depends(require_auth)):
    """
    Check the status of the connection to the Supabase PostgreSQL database.
    """
    # The test_connection function now handles exceptions and returns a dict
    status_dict = test_connection()
    return ConnectionStatusResponse(**status_dict)
