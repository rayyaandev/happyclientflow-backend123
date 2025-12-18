"""
Google Places API Integration

This API provides functionality to fetch place details from Google Places API,
including business information, reviews, ratings, and other relevant data.

Reviews are fetched via Apify scraper to bypass Google's 5-review limit.

Used by: Dashboard analytics, review management features
Endpoints:
- GET /google-places/details/{place_id} - Fetch detailed place information
"""

import requests
import hashlib
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import List, Optional
import databutton as db
from apify_client import ApifyClientAsync

router = APIRouter(prefix="/google-places")

# =====================
# Pydantic Models
# =====================
class ReviewData(BaseModel):
    author_name: str
    rating: int
    text: str
    time: int
    profile_photo_url: Optional[str] = None
    relative_time_description: str

class PlaceDetailsResponse(BaseModel):
    place_id: str
    name: str
    rating: Optional[float] = None
    user_ratings_total: Optional[int] = None
    reviews: List[ReviewData] = []
    formatted_address: Optional[str] = None
    formatted_phone_number: Optional[str] = None
    website: Optional[str] = None
    business_status: Optional[str] = None
    price_level: Optional[int] = None
    types: List[str] = []

class ErrorResponse(BaseModel):
    error: str
    message: str

# =====================
# Helper Functions
# =====================
def get_reviews_cache_key(place_id: str) -> str:
    """Generates a unique cache key for reviews based on place_id."""
    return f"google_reviews_{hashlib.md5(place_id.encode()).hexdigest()}"

def parse_iso_to_unix(iso_date: str) -> int:
    """Convert ISO date string to Unix timestamp."""
    try:
        dt = datetime.fromisoformat(iso_date.replace('Z', '+00:00'))
        return int(dt.timestamp())
    except (ValueError, TypeError):
        return 0

def transform_apify_review_to_review_data(apify_review: dict) -> ReviewData:
    """Transform Apify review format to our ReviewData format."""
    return ReviewData(
        author_name=apify_review.get("name", "Anonymous"),
        rating=apify_review.get("stars", 0),
        text=apify_review.get("text") or "",  # Handle null text
        time=parse_iso_to_unix(apify_review.get("publishedAtDate", "")),
        profile_photo_url=apify_review.get("reviewerPhotoUrl"),
        relative_time_description=apify_review.get("publishAt", "")
    )

async def fetch_reviews_from_apify(place_id: str) -> List[ReviewData]:
    """
    Fetch reviews from Apify Google Maps Reviews scraper.
    Uses 24-hour caching to minimize API calls.
    """
    cache_key = get_reviews_cache_key(place_id)
    ttl = timedelta(hours=24)
    
    # 1. Check for cached data
    try:
        cached_entry = db.storage.json.get(cache_key)
        cached_timestamp = datetime.fromisoformat(cached_entry["timestamp"])
        
        if datetime.now(timezone.utc) - cached_timestamp < ttl:
            print(f"[CACHE HIT] Returning cached reviews for place_id: {place_id}")
            cached_reviews = cached_entry.get("reviews", [])
            # Transform cached reviews back to ReviewData objects
            return [ReviewData(**review) for review in cached_reviews]
    except (FileNotFoundError, KeyError, TypeError):
        # Cache miss
        print(f"[CACHE MISS] Fetching fresh reviews for place_id: {place_id}")
    
    # 2. Fetch from Apify
    try:
        apify_api_key = db.secrets.get("APIFY_API_KEY")
        actor_id = "Xb8osYTtOjlsgI6k9"  # Google Maps Reviews scraper
        
        if not apify_api_key:
            print("[ERROR] Apify API key not configured")
            return []
        
        client = ApifyClientAsync(apify_api_key)
        actor_client = client.actor(actor_id)
        
        # Apify input configuration
        run_input = {
            "language": "de",  # German language for reviews
            "maxReviews": 99999,  # Get all reviews
            "personalData": True,
            "placeIds": [place_id],
            "reviewsSort": "newest",
            "reviewsOrigin": "all"
        }
        
        print(f"[APIFY] Starting actor run for place_id: {place_id}")
        run = await actor_client.call(run_input=run_input)
        
        # Fetch results from dataset
        dataset_items = []
        if run and run.get("defaultDatasetId"):
            dataset_client = client.dataset(run["defaultDatasetId"])
            dataset_items = (await dataset_client.list_items()).items
            print(f"[APIFY] Fetched {len(dataset_items)} reviews")
        
        # Transform Apify reviews to our format
        reviews = []
        for apify_review in dataset_items:
            review = transform_apify_review_to_review_data(apify_review)
            reviews.append(review)
        
        # 3. Cache the results
        cache_entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "reviews": [review.model_dump() for review in reviews]
        }
        db.storage.json.put(cache_key, cache_entry)
        print(f"[CACHE] Stored {len(reviews)} reviews for place_id: {place_id}")
        
        return reviews
        
    except Exception as e:
        print(f"[ERROR] Failed to fetch reviews from Apify: {str(e)}")
        return []

# =====================
# API Endpoints
# =====================
@router.get("/details/{place_id}")
async def get_place_details(
    place_id: str
) -> PlaceDetailsResponse:
    """
    Fetch detailed information about a place using Google Places API.
    Reviews are fetched via Apify scraper to get ALL reviews (not limited to 5).
    
    Args:
        place_id: The unique Google Places ID for the business/location
        
    Returns:
        PlaceDetailsResponse: Comprehensive place data including reviews and ratings
    """
    print(f"[DEBUG] Fetching Google Places details for place_id: {place_id}")
    
    # Get Google Places API key from secrets
    api_key = db.secrets.get("GOOGLE_PLACES_API_KEY")
    if not api_key:
        raise HTTPException(
            status_code=500, 
            detail="Google Places API key not configured"
        )
    
    # Define the fields we want to retrieve (excluding reviews - we get those from Apify)
    fields = [
        "place_id",
        "name", 
        "rating",
        "user_ratings_total",
        "formatted_address",
        "formatted_phone_number",
        "website",
        "business_status",
        "price_level",
        "types"
    ]
    
    # Make request to Google Places API for business details
    url = "https://maps.googleapis.com/maps/api/place/details/json"
    params = {
        "place_id": place_id,
        "fields": ",".join(fields),
        "key": api_key
    }
    
    try:
        # Fetch place details from Google API
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        if data.get("status") != "OK":
            error_message = data.get("error_message", "Unknown error")
            raise HTTPException(
                status_code=400,
                detail=f"Google Places API error: {error_message}"
            )
        
        result = data.get("result", {})
        
        # Fetch reviews from Apify (with caching)
        reviews = await fetch_reviews_from_apify(place_id)
        print(f"[REVIEWS] Fetched {len(reviews)} reviews for place_id: {place_id}")
        
        # Return structured response
        return PlaceDetailsResponse(
            place_id=result.get("place_id", place_id),
            name=result.get("name", ""),
            rating=result.get("rating"),
            user_ratings_total=result.get("user_ratings_total"),
            reviews=reviews,
            formatted_address=result.get("formatted_address"),
            formatted_phone_number=result.get("formatted_phone_number"),
            website=result.get("website"),
            business_status=result.get("business_status"),
            price_level=result.get("price_level"),
            types=result.get("types", [])
        )
        
    except requests.RequestException as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to fetch place details: {str(e)}"
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Internal server error: {str(e)}"
        )
