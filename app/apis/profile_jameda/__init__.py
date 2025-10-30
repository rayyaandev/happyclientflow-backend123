"""
This API module handles scraping of Jameda profile reviews using Apify.
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


class JamedaReview(BaseModel):
    rating: str  # Jameda uses string ratings like "5", "4"
    text: str
    date: str


class ScrapedProfile(BaseModel):
    url: str
    ratingValue: str = None  # Jameda uses string for overall rating
    reviewCount: int = None
    totalScraped: int = None
    reviews: list[JamedaReview]


class ScrapeResponse(BaseModel):
    data: list[ScrapedProfile]
    message: str
    pagination: PaginationMeta


# =====================
# Helper Functions
# =====================
def get_cache_key(url: str, page: int) -> str:
    """Generates a unique cache key for a given URL and page."""
    return f"jameda_scrape_all_{hashlib.md5(url.encode()).hexdigest()}"


# =====================
# API Endpoints
# =====================
@router.post("/profile_jameda", response_model=ScrapeResponse)
@single_request_per_url
async def profile_jameda(request: ScrapeRequest):
    """
    Scrapes a Jameda profile's review page using Apify, with a 24-hour cache TTL.
    """
    # Generate cache key
    cache_key = hashlib.md5(f"jameda_{request.url}_{request.page}".encode()).hexdigest()
    
    # 1. Try returning from cache
    try:
        cached_entry = db.storage.json.get(cache_key)
        cached_timestamp = datetime.fromisoformat(cached_entry["timestamp"])
        if datetime.now(timezone.utc) - cached_timestamp < timedelta(hours=24):
            cached_data = cached_entry["data"]
            if cached_data and len(cached_data) > 0:
                scraped_data = cached_data[0]
                all_reviews = scraped_data.get("reviews", [])
                
                # Convert reviews to JamedaReview format
                formatted_reviews = []
                for review in all_reviews:
                    formatted_reviews.append(JamedaReview(
                        rating=str(review.get("rating", "0")),
                        text=review.get("text", ""),
                        date=review.get("date", "")
                    ))
                
                response_data = [
                    ScrapedProfile(
                        url=scraped_data.get("url", request.url),
                        ratingValue=str(scraped_data.get("ratingValue", "0")),
                        reviewCount=scraped_data.get("reviewCount"),
                        totalScraped=scraped_data.get("totalScraped"),
                        reviews=formatted_reviews,
                    )
                ]
                pagination_meta = PaginationMeta(
                    current_page=request.page,
                    has_next_page=False,
                    total_pages=1,
                    total_reviews=scraped_data.get("reviewCount") or len(all_reviews),
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
        jameda_actor_id = "TdnVDjKfX5TypLXIm"  # Using the provided Jameda actor ID

        if not apify_api_key:
            raise HTTPException(status_code=500, detail="Apify API key is not configured.")

        client = ApifyClientAsync(apify_api_key)
        actor_client = client.actor(jameda_actor_id)

        # === Jameda pageFunction ===
        page_function_js = """
async function pageFunction(context) {
    const { log, request } = context; // Use jQuery from context

    const url = request.url;
    const $ = context.jQuery;
    log.info(`Scraping Jameda reviews from: ${url}`);

    const reviews = [];

    // ---- Extract overall rating + count ----
    let ratingValue = null;
    let reviewCount = 0;

    try {
        ratingValue = $('meta[itemprop="ratingValue"]').attr('content') || null;
    } catch (err) {
        log.warning(`Failed to get ratingValue: ${err.message}`);
    }

    try {
        reviewCount = parseInt($('meta[itemprop="reviewCount"]').attr('content') || '0', 10);
    } catch (err) {
        log.warning(`Failed to get reviewCount: ${err.message}`);
    }

    // ---- Helper to extract reviews ----
    function extractReviews() {
        const reviewEls = $('[data-test-id="opinion-block"]');
        const newReviews = [];

        reviewEls.each((_, el) => {
            const $el = $(el);
            const rating = $el.find('[itemprop="ratingValue"]').attr('content') || null;
            const text = $el.find('[itemprop="reviewBody"]').text().trim() || null;
            const date = $el.find('[itemprop="datePublished"]').attr('datetime') || null;

            newReviews.push({ rating, text, date });
        });
        return newReviews;
    }

    // ---- Keep clicking "Mehr anzeigen" until limit ----
    while (reviews.length < 100) {
        const newReviews = extractReviews();

        // Add only new ones (basic deduplication)
        for (const r of newReviews) {
            if (reviews.length >= 100) break;
            if (r.text && !reviews.some(existing => existing.text === r.text && existing.date === r.date)) {
                reviews.push(r);
            }
        }

        log.info(`Collected ${reviews.length} reviews so far`);

        // Find "Mehr anzeigen" button
        const loadMoreBtn = $('button[data-id="load-more-opinions"]:not([disabled])');
        if (!loadMoreBtn.length) {
            log.info('No more reviews to load.');
            break;
        }

        // Click the button
        loadMoreBtn.click();
        log.info('Clicked "Mehr anzeigen" button, waiting for more reviews...');

        // Wait for AJAX content to load
        await new Promise(resolve => setTimeout(resolve, 2500));
    }

    // ---- Return structured data ----
    return {
        url,
        ratingValue,
        reviewCount,
        totalScraped: reviews.length,
        reviews,
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

        # Convert reviews to JamedaReview format
        formatted_reviews = []
        for review in all_reviews:
            formatted_reviews.append(JamedaReview(
                rating=str(review.get("rating", "0")),
                text=review.get("text", ""),
                date=review.get("date", "")
            ))

        pagination_meta = PaginationMeta(
            current_page=request.page,
            has_next_page=False,
            total_pages=1,
            total_reviews=scraped_data.get("reviewCount") if dataset_items else len(all_reviews),
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
                ratingValue=str(scraped_data.get("ratingValue", "0")),
                reviewCount=scraped_data.get("reviewCount", 0),
                totalScraped=scraped_data.get("totalScraped", len(formatted_reviews)),
                reviews=formatted_reviews,
            )
        ]

        message = "Data scraped successfully." if all_reviews else "No data found, but request was cached."
        return ScrapeResponse(data=response_data, message=message, pagination=pagination_meta)

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"An error occurred during scraping: {str(e)}")
