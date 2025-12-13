"""
Sentiment Analysis API

This API generates AI-powered sentiment analysis for customer reviews.
It creates ONE combined sentiment per company from ALL profiles.
Regenerates when total review count increases by 5.

Flow:
1. Frontend fetches reviews from all profile scrapers
2. Frontend calls this endpoint with combined reviews + total count
3. This endpoint checks if regeneration is needed (count increased by 5+)
4. If yes → generate new sentiment with OpenAI, save to DB
5. If no → return existing sentiment from DB
"""
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime, timezone
import databutton as db
from supabase import Client, create_client
from openai import OpenAI

router = APIRouter(prefix="/sentiment", tags=["Sentiment Analysis"])

# ===============================================================================
# OpenAI Client
# ===============================================================================
try:
    openai_client = OpenAI(api_key=db.secrets.get("OPENAI_API_KEY"))
except Exception as e:
    print(f"Error initializing OpenAI client: {e}")
    openai_client = None

# ===============================================================================
# Supabase Client
# ===============================================================================
def get_supabase_service_client() -> Client:
    """Initializes and returns a Supabase client with the service role key."""
    supabase_url = db.secrets.get("SUPABASE_URL")
    service_key = db.secrets.get("SUPABASE_SERVICE_KEY")
    if not supabase_url or not service_key:
        raise HTTPException(status_code=500, detail="Supabase configuration missing.")
    return create_client(supabase_url, service_key)

# ===============================================================================
# Pydantic Models
# ===============================================================================
class ReviewInput(BaseModel):
    """Single review input - flexible to handle different profile types"""
    text: str  # The review content/text
    rating: Optional[float] = None
    source: Optional[str] = None  # e.g., "google", "proven_expert" - optional for context

class SentimentRequest(BaseModel):
    """Request to get or generate sentiment for a company (combined from all profiles)"""
    company_id: str
    current_total_review_count: int  # Total review count across ALL profiles
    reviews: List[ReviewInput]  # Combined reviews from all profiles (max 30 will be used)

class SentimentResponse(BaseModel):
    """Response containing the sentiment analysis"""
    company_id: str
    sentiment: str
    last_review_count: int
    was_regenerated: bool  # True if sentiment was just generated, False if from cache
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

# ===============================================================================
# Sentiment Generation Prompt
# ===============================================================================
SENTIMENT_PROMPT = """You are analyzing customer reviews for a local business.

The input contains the {review_count} most recent stored reviews from various review platforms.

Your task is to generate an "Overall Sentiment" summary that describes the general mood and customer perception of the business based on these reviews.

Guidelines:
- Do NOT list or quote individual reviews.
- Do NOT mention reviewer names.
- Focus on overall emotional tone and recurring themes.
- Be neutral, analytical, and professional.
- Avoid marketing language or exaggeration.

Structure the output in 3 short paragraphs:
1. Overall sentiment and general customer perception
2. Most common positive themes mentioned by customers
3. Recurring improvement areas (if any), phrased constructively

Length:
- 80–120 words total.

Input (last {review_count} reviews):
{reviews}"""

# ===============================================================================
# Helper Functions
# ===============================================================================
def format_reviews_for_prompt(reviews: List[ReviewInput], max_reviews: int = 30) -> str:
    """Format reviews into a string for the AI prompt"""
    # Take only the most recent reviews (up to max_reviews)
    limited_reviews = reviews[:max_reviews]
    
    formatted = []
    for i, review in enumerate(limited_reviews, 1):
        rating_str = f" (Rating: {review.rating})" if review.rating else ""
        source_str = f" [{review.source}]" if review.source else ""
        formatted.append(f"Review {i}{source_str}{rating_str}: {review.text}")
    
    return "\n\n".join(formatted)


def generate_sentiment_with_ai(reviews: List[ReviewInput]) -> str:
    """Generate sentiment analysis using OpenAI Responses API"""
    if not openai_client:
        raise HTTPException(status_code=500, detail="OpenAI client is not configured.")
    
    if not reviews:
        raise HTTPException(status_code=400, detail="No reviews provided for sentiment analysis.")
    
    # Format reviews for the prompt (max 30)
    reviews_text = format_reviews_for_prompt(reviews)
    review_count = min(len(reviews), 30)  # Actual number of reviews being analyzed
    prompt = SENTIMENT_PROMPT.format(reviews=reviews_text, review_count=review_count)
    
    try:
        # Using OpenAI Responses API
        response = openai_client.responses.create(
            model="gpt-4o-mini",
            input=prompt
        )
        
        # Extract text from response
        sentiment = response.output_text
        
        if not sentiment:
            raise HTTPException(status_code=500, detail="OpenAI returned an empty response.")
        
        return sentiment.strip()
    
    except Exception as e:
        print(f"Error generating sentiment with OpenAI: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to generate sentiment: {str(e)}")


# ===============================================================================
# API Endpoint
# ===============================================================================
@router.post("/analyze", response_model=SentimentResponse)
def analyze_sentiment(
    request: SentimentRequest,
    supabase: Client = Depends(get_supabase_service_client)
):
    """
    Get or generate sentiment analysis for a company.
    Combines reviews from ALL profiles into ONE sentiment per company.
    
    Logic:
    - If no existing sentiment → generate new one
    - If existing sentiment and total review_count increased by 5+ → regenerate
    - Otherwise → return cached sentiment
    
    Request body:
    - company_id: The company's UUID
    - current_total_review_count: Total review count across ALL profiles
    - reviews: Combined array of reviews from all profiles (max 30 will be used)
    """
    company_id = request.company_id
    current_count = request.current_total_review_count
    
    try:
        # 1. Check for existing sentiment record for this company
        existing = supabase.from_("reviews_sentiment").select(
            "id, sentiment, last_review_count, created_at, updated_at"
        ).eq("company_id", company_id).execute()
        
        existing_record = existing.data[0] if existing.data else None
        
        # 2. Determine if we need to regenerate
        should_regenerate = False
        
        if not existing_record:
            # No existing record → generate new
            should_regenerate = True
            print(f"No existing sentiment for company {company_id}. Generating new.")
        else:
            last_count = existing_record.get("last_review_count", 0) or 0
            # Check if increased by 5 or more
            if current_count >= last_count + 5:
                should_regenerate = True
                print(f"Total review count increased by 5+ ({last_count} → {current_count}). Regenerating sentiment.")
            else:
                print(f"Total review count not increased enough ({last_count} → {current_count}). Using cached sentiment.")
        
        # 3. Generate or return cached
        if should_regenerate:
            # Generate new sentiment
            sentiment_text = generate_sentiment_with_ai(request.reviews)
            now = datetime.now(timezone.utc).isoformat()
            
            if existing_record:
                # Update existing record
                supabase.from_("reviews_sentiment").update({
                    "sentiment": sentiment_text,
                    "last_review_count": current_count,
                    "updated_at": now
                }).eq("id", existing_record["id"]).execute()
                
                return SentimentResponse(
                    company_id=company_id,
                    sentiment=sentiment_text,
                    last_review_count=current_count,
                    was_regenerated=True,
                    created_at=existing_record.get("created_at"),
                    updated_at=now
                )
            else:
                # Insert new record
                supabase.from_("reviews_sentiment").insert({
                    "company_id": company_id,
                    "sentiment": sentiment_text,
                    "last_review_count": current_count,
                    "created_at": now,
                    "updated_at": now
                }).execute()
                
                return SentimentResponse(
                    company_id=company_id,
                    sentiment=sentiment_text,
                    last_review_count=current_count,
                    was_regenerated=True,
                    created_at=now,
                    updated_at=now
                )
        else:
            # Return cached sentiment
            return SentimentResponse(
                company_id=company_id,
                sentiment=existing_record["sentiment"],
                last_review_count=existing_record.get("last_review_count", 0),
                was_regenerated=False,
                created_at=existing_record.get("created_at"),
                updated_at=existing_record.get("updated_at")
            )
    
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error in sentiment analysis: {e}")
        raise HTTPException(status_code=500, detail=f"An error occurred: {str(e)}")
