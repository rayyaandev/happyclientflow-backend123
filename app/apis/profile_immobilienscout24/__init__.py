"""
This API module handles scraping of ImmobilienScout24 profile reviews using Apify.
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
import databutton as db
from apify_client import ApifyClientAsync
import hashlib
import json
from datetime import datetime, timedelta, timezone
from app.libs.api_utils import single_request_per_url

# =====================
# API Router
# =====================
router = APIRouter()


# =====================
# Pydantic Models
# =====================
class ScrapeRequest(BaseModel):
    url: str
    page: int = 1


class PaginationMeta(BaseModel):
    current_page: int
    has_next_page: bool
    total_pages: int = None
    total_reviews: int = None


class ScrapedProfile(BaseModel):
    url: str
    average_score: float | None = None
    total_reviews: int | None = None
    reviews: list


class ScrapeResponse(BaseModel):
    data: list[ScrapedProfile]
    message: str
    pagination: PaginationMeta


# =====================
# Helper Functions
# =====================
def get_cache_key(url: str, page: int) -> str:
    """Generates a unique cache key for a given URL and page."""
    return f"immobilienscout24_scrape_{hashlib.md5(f'{url}_{page}'.encode()).hexdigest()}"


# =====================
# API Endpoints
# =====================
@router.post("/profile_immobilienscout24", response_model=ScrapeResponse)
@single_request_per_url
async def profile_immobilienscout24(request: ScrapeRequest):
    """
    Scrapes an ImmobilienScout24 profile's review page using Apify, with a 24-hour cache TTL.
    """
    cache_key = get_cache_key(request.url, request.page)
    ttl = timedelta(hours=24)

    # 1. Try returning from cache
    try:
        cached_entry = db.storage.json.get(cache_key)
        cached_timestamp = datetime.fromisoformat(cached_entry["timestamp"])
        if datetime.now(timezone.utc) - cached_timestamp < ttl:
            cached_data = cached_entry["data"]
            if cached_data and len(cached_data) > 0:
                scraped_data = cached_data[0]
                all_reviews = scraped_data.get("reviews", [])
                response_data = [
                    ScrapedProfile(
                        url=scraped_data.get("url", request.url),
                        average_score=scraped_data.get("average_score"),
                        total_reviews=scraped_data.get("total_reviews"),
                        reviews=all_reviews,
                    )
                ]
                pagination_meta = PaginationMeta(
                    current_page=1,
                    has_next_page=False,
                    total_pages=1,
                    total_reviews=scraped_data.get("total_reviews") or len(all_reviews),
                )
                return ScrapeResponse(
                    data=response_data,
                    message="Data retrieved from cache.",
                    pagination=pagination_meta,
                )
    except Exception:
        pass

    # 2. Scrape using Apify
    try:
        apify_api_key = db.secrets.get("APIFY_API_KEY")
        # TODO: Replace with actual ImmobilienScout24 Apify actor ID
        immobilienscout24_actor_id = "YOUR_ACTOR_ID_HERE"

        if not apify_api_key:
            raise HTTPException(status_code=500, detail="Apify API key is not configured.")

        client = ApifyClientAsync(apify_api_key)
        actor_client = client.actor(immobilienscout24_actor_id)

        # === ImmobilienScout24 pageFunction ===
        # TODO: Implement the actual scraping logic for ImmobilienScout24
        page_function_js = """
async function pageFunction(context) {
    const { jQuery: $, request, log, enqueueRequest } = context;
    log.info(`Scraping ImmobilienScout24 reviews from: ${request.url}`);

    const reviewsData = [];

    // TODO: Implement ImmobilienScout24 review scraping logic
    // Example structure:
    // $('.review-item').each((_, el) => {
    //     const $el = $(el);
    //     reviewsData.push({
    //         authorName: $el.find('.author-name').text().trim(),
    //         ratingStars: parseInt($el.find('.rating').text().trim()),
    //         createdDate: $el.find('.date').text().trim(),
    //         title: $el.find('.review-title').text().trim() || null,
    //         body: $el.find('.review-body').text().trim() || null,
    //     });
    // });

    // TODO: Extract summary (average score, total reviews)
    const averageScore = null; // Extract from page
    const totalReviews = null; // Extract from page

    return {
        url: request.url,
        average_score: averageScore,
        total_reviews: totalReviews,
        reviews: reviewsData,
    };
}
"""

        run_input = {
            "startUrls": [{"url": request.url}],
            "pageFunction": page_function_js,
            "maxResultsPerCrawl": 10
        }

        run = await actor_client.call(run_input=run_input)
        dataset_items = []
        if run and run.get("defaultDatasetId"):
            dataset_client = client.dataset(run["defaultDatasetId"])
            dataset_items = (await dataset_client.list_items()).items

        all_reviews = []
        scraped_data = {}
        if dataset_items and len(dataset_items) > 0:
            scraped_data = dataset_items[0]
            all_reviews = scraped_data.get("reviews", [])

        pagination_meta = PaginationMeta(
            current_page=1,
            has_next_page=False,
            total_pages=1,
            total_reviews=scraped_data.get("total_reviews") if dataset_items else len(all_reviews),
        )

        # 3. Cache results
        db.storage.json.put(
            cache_key,
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "data": dataset_items,
            },
        )

        response_data = [
            ScrapedProfile(
                url=scraped_data.get("url", request.url),
                average_score=scraped_data.get("average_score"),
                total_reviews=scraped_data.get("total_reviews"),
                reviews=all_reviews,
            )
        ]

        message = "Data scraped successfully." if all_reviews else "No data found, but request was cached."
        return ScrapeResponse(data=response_data, message=message, pagination=pagination_meta)

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"An error occurred during scraping: {str(e)}")

