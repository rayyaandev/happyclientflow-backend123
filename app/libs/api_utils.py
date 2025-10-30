"""
Utility module for handling request locking to prevent concurrent API calls.
"""
import asyncio
import hashlib
from functools import wraps
from typing import Callable, Any

# Global state for request locking
_ongoing_requests = {}
_request_locks = {}

def single_request_per_url(func: Callable) -> Callable:
    """
    Decorator that ensures only one request per URL is processed at a time.
    If multiple requests come in for the same URL, subsequent requests will
    wait for the first one to complete and return the same result.
    """
    @wraps(func)
    async def wrapper(request, *args, **kwargs):
        # Generate cache key based on URL and page
        cache_key = hashlib.md5(f"{func.__name__}_{request.url}_{getattr(request, 'page', 1)}".encode()).hexdigest()
        
        # Create a lock for this specific request if it doesn't exist
        if cache_key not in _request_locks:
            _request_locks[cache_key] = asyncio.Lock()
        
        # Use the lock to ensure only one request per URL is processed at a time
        async with _request_locks[cache_key]:
            # Check if this request is already being processed
            if cache_key in _ongoing_requests:
                # Wait for the ongoing request to complete and return its result
                return await _ongoing_requests[cache_key]
            
            # Mark this request as ongoing
            request_future = asyncio.create_task(func(request, *args, **kwargs))
            _ongoing_requests[cache_key] = request_future
            
            try:
                result = await request_future
                return result
            finally:
                # Clean up the ongoing request tracking
                _ongoing_requests.pop(cache_key, None)
                # Optionally clean up old locks to prevent memory leaks
                if len(_request_locks) > 1000:  # Arbitrary limit
                    # Keep only the most recent 500 locks
                    keys_to_remove = list(_request_locks.keys())[:-500]
                    for key in keys_to_remove:
                        _request_locks.pop(key, None)
    
    return wrapper
