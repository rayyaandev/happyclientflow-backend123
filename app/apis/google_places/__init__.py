"""
Google Places API Integration

This API provides functionality to fetch place details from Google Places API,
including business information, reviews, ratings, and other relevant data.

Used by: Dashboard analytics, review management features
Endpoints:
- GET /google-places/details/{place_id} - Fetch detailed place information
"""

import requests
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import List, Optional
import databutton as db
from app.libs.auth import require_auth

router = APIRouter(prefix="/google-places")

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

@router.get("/details/{place_id}")
def get_place_details(
    place_id: str,
    current_user: str = Depends(require_auth)
) -> PlaceDetailsResponse:
    """
    Fetch detailed information about a place using Google Places API.
    
    Args:
        place_id: The unique Google Places ID for the business/location
        current_user: The authenticated user's ID from JWT token
        
    Returns:
        PlaceDetailsResponse: Comprehensive place data including reviews and ratings
    """
    print(f"[AUTH] Fetching Google Places details for user: {current_user}")

    print(f"DEBUG place_id: {place_id}")
    
    # Get Google Places API key from secrets
    api_key = db.secrets.get("GOOGLE_PLACES_API_KEY")
    print(f"DEBUG api_key: {api_key}")
    if not api_key:
        raise HTTPException(
            status_code=500, 
            detail="Google Places API key not configured"
        )
    
    # Define the fields we want to retrieve
    fields = [
        "place_id",
        "name", 
        "rating",
        "user_ratings_total",
        "reviews",
        "formatted_address",
        "formatted_phone_number",
        "website",
        "business_status",
        "price_level",
        "types"
    ]
    
    # Make request to Google Places API
    url = "https://maps.googleapis.com/maps/api/place/details/json"
    params = {
        "place_id": place_id,
        "fields": ",".join(fields),
        "key": api_key,
        "reviews_no_translations": "true"  # Get reviews in original language
    }
    
    try:
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
        
        # Parse reviews data
        reviews = []
        for review in result.get("reviews", []):
            reviews.append(ReviewData(
                author_name=review.get("author_name", "Anonymous"),
                rating=review.get("rating", 0),
                text=review.get("text", ""),
                time=review.get("time", 0),
                profile_photo_url=review.get("profile_photo_url"),
                relative_time_description=review.get("relative_time_description", "")
            ))
        
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
