"""
This API module handles scraping of ProvenExpert profile reviews using Apify.
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

class ScrapeResponse(BaseModel):
    data: list
    message: str
    pagination: PaginationMeta

# =====================
# Helper Functions
# =====================
def get_cache_key(url: str, page: int) -> str:
    """Generates a unique cache key for a given URL and page."""
    # For ProvenExpert, we cache all pages together since we scrape them all at once
    # But we still use page-specific keys for the API response
    return f"provenexpert_scrape_all_{hashlib.md5(url.encode()).hexdigest()}"

# =====================
# API Endpoints
# =====================
@router.post("/profile_provenexpert", response_model=ScrapeResponse)
@single_request_per_url
async def profile_provenexpert(request: ScrapeRequest):
    """
    Scrapes a ProvenExpert profile's review page using Apify, with a 24-hour cache TTL.
    """
    cache_key = get_cache_key(request.url, request.page)
    ttl = timedelta(hours=24)

    # 1. Check for cached data
    try:
        cached_entry = db.storage.json.get(cache_key)
        cached_timestamp = datetime.fromisoformat(cached_entry["timestamp"])
        
        if datetime.now(timezone.utc) - cached_timestamp < ttl:
            # Return all reviews from cached data (flattened)
            cached_data = cached_entry["data"]
            if cached_data and len(cached_data) > 0:
                scraped_data = cached_data[0]
                
                # Get all reviews (already flattened in cache)
                all_reviews = scraped_data.get("reviews", [])
                
                # IMPORTANT: Only count reviews we actually scraped (exclude external sources shown on the page)
                only_scraped_count = len(all_reviews)
                
                # Create response data with all reviews
                response_data = [{
                    "url": scraped_data.get("url", request.url),
                    "summaryScore": scraped_data.get("summaryScore", ""),
                    "summaryReviewsCount": str(only_scraped_count),
                    "reviews": all_reviews
                }]
                
                pagination_meta = PaginationMeta(
                    current_page=1,
                    has_next_page=False,  # No backend pagination
                    total_pages=1,
                    total_reviews=len(all_reviews)
                )
                
                return ScrapeResponse(
                    data=response_data,
                    message="Data retrieved from cache.",
                    pagination=pagination_meta
                )
    except (FileNotFoundError, KeyError, TypeError):
        # Cache miss if file not found, or entry is malformed
        pass

    # 2. Scrape data using Apify if not cached or cache is stale
    try:
        apify_api_key = db.secrets.get("APIFY_API_KEY")
        # Use the same actor ID as Anwalt.de for web scraping
        provenexpert_actor_id = "K0KedxhxRkmldOHtd"

        if not apify_api_key:
            raise HTTPException(status_code=500, detail="Apify API key is not configured.")

        client = ApifyClientAsync(apify_api_key)
        actor_client = client.actor(provenexpert_actor_id)
        
        # ProvenExpert-specific scraping function that organizes reviews into pages
        page_function_js = """
            async function pageFunction(context) {
                const { jQuery: $, request, log } = context;
                log.info(`Scraping reviews from: ${request.url}`);

                const reviewsData = [];

                // --- helper: scrape reviews from current page ---
                function scrapeCurrentPage() {
                    const REVIEWS_SELECTOR = '.peRatingItem';
                    const results = [];

                    $(REVIEWS_SELECTOR).each((index, element) => {
                        const $review = $(element);
                        const body = $review.find('.ratingFeedbackText').text().trim();
                        const authorName = $review.find('.ratingAuthor').text().trim();
                        const createdDate = $review.find('.ratingSubline span:first-child').text().trim();
                        const rating = $review.find('.goldText.semibold').text().trim();

                        results.push({
                            title: null,
                            body,
                            ratingStars: rating,
                            authorName,
                            createdDate,
                        });
                    });

                    return results;
                }

                // --- scrape first page ---
                reviewsData.push(...scrapeCurrentPage());

                // --- pagination loop ---
                let pageNum = 1;
                while (true) {
                    const $next = $('a.pagItem.next');
                    if ($next.length === 0) break;

                    // Extract the "Profile.ratingPage(...)" call from onclick
                    const onclick = $next.attr('onclick'); 
                    const match = onclick && onclick.match(/Profile\.ratingPage\((\d+),\s*'([^']+)'/);

                    if (!match) {
                        log.info("No valid onclick for next page. Stopping.");
                        break;
                    }

                    const nextPage = parseInt(match[1], 10);
                    const token = match[2];
                    log.info(`Going to next page ${nextPage}`);

                    // Call the site’s own function directly
                    window.Profile.ratingPage(nextPage, token);

                    // Wait for DOM to refresh (tweak delay if needed)
                    await new Promise(r => setTimeout(r, 4000));

                    // Scrape new reviews
                    reviewsData.push(...scrapeCurrentPage());

                    pageNum++;
                }

                // --- summary info ---
                const score = $('.semibold.goldText span:first-child').text().trim(); 
                const reviewsCount = $('.ratingValueLabel').next('div').find('span:first-child').text().trim();

                return {
                    url: request.url,
                    summaryScore: score,
                    summaryReviewsCount: reviewsCount,
                    reviews: reviewsData,
                };
            }


        """

        # ProvenExpert scraper fetches all pages at once, so we always use the base URL
        page_url = f"{request.url}/#ratings"
            
        run_input = {
            "startUrls": [ 
                {"url": page_url}
            ],
            "pageFunction": page_function_js,
        }

        # Run the actor
        run = await actor_client.call(run_input=run_input)

        # Fetch the results from the run's default Dataset
        if run and run.get('defaultDatasetId'):
            dataset_client = client.dataset(run['defaultDatasetId'])
            dataset_items = (await dataset_client.list_items()).items
        else:
            dataset_items = []
        
        # ProvenExpert now returns all reviews in a flat list
        all_reviews = []
        
        if dataset_items and len(dataset_items) > 0:
            scraped_data = dataset_items[0]
            
            # Get all reviews (already in flat structure from scraper)
            all_reviews = scraped_data.get("reviews", [])
        
        # Simple pagination metadata - frontend handles pagination
        pagination_meta = PaginationMeta(
            current_page=1,
            has_next_page=False,  # No backend pagination needed
            total_pages=1,
            total_reviews=len(all_reviews)
        )

        if not dataset_items:
            # We still cache empty results to avoid re-scraping pages with no data
            pass

        # 3. Cache the complete scraped data (all pages) with a timestamp
        new_cache_entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "data": dataset_items  # This contains all pages
        }
        db.storage.json.put(cache_key, new_cache_entry)

        # Ensure summaryReviewsCount reflects ONLY scraped reviews length (exclude external sources)
        transformed_data = []
        if dataset_items and len(dataset_items) > 0:
            first = dict(dataset_items[0])
            first["summaryReviewsCount"] = str(len(all_reviews))
            transformed_data.append(first)
        else:
            transformed_data = dataset_items

        # Return all reviews in flat structure
        message = "Data scraped successfully." if all_reviews else "No data found, but request was cached."
        return ScrapeResponse(data=transformed_data, message=message, pagination=pagination_meta)

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"An error occurred during scraping: {str(e)}")
