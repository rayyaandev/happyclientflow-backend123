"""
This API module handles scraping of MyHammer profile reviews using Apify.
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
def normalize_myhammer_url(url: str, page: int = 1) -> str:
    """
    Normalizes MyHammer URL to ensure proper format with /bewertungen path and page parameter.
    
    Examples:
    - https://www.my-hammer.de/auftragnehmer/business-name -> https://www.my-hammer.de/auftragnehmer/business-name/bewertungen?page=1
    - https://www.my-hammer.de/auftragnehmer/business-name/bewertungen -> https://www.my-hammer.de/auftragnehmer/business-name/bewertungen?page=1
    """
    from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
    
    parsed = urlparse(url)
    
    # Ensure the path ends with /bewertungen
    path = parsed.path.rstrip('/')
    if not path.endswith('/bewertungen'):
        path += '/bewertungen'
    
    # Parse existing query parameters
    query_params = parse_qs(parsed.query)
    
    # Set or update the page parameter
    query_params['page'] = [str(page)]
    
    # Reconstruct the URL with proper query string formatting
    if query_params:
        new_query = urlencode(query_params, doseq=True)
    else:
        new_query = ''
    
    normalized_url = urlunparse((
        parsed.scheme,
        parsed.netloc,
        path,
        parsed.params,
        new_query,
        parsed.fragment
    ))
    
    return normalized_url


def get_cache_key(url: str, page: int) -> str:
    """Generates a unique cache key for a given URL and page."""
    return f"myhammer_scrape_all_{hashlib.md5(url.encode()).hexdigest()}"


# =====================
# API Endpoints
# =====================
@router.post("/profile_myhammer", response_model=ScrapeResponse)
@single_request_per_url
async def profile_myhammer(request: ScrapeRequest):
    """
    Scrapes a MyHammer profile's review page using Apify, with a 24-hour cache TTL.
    Automatically normalizes URLs to include /bewertungen path and page parameters.
    """
    # Normalize the URL to ensure proper format
    normalized_url = normalize_myhammer_url(request.url, request.page)
    
    cache_key = get_cache_key(normalized_url, request.page)
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
        myhammer_actor_id = "pBEnk9UMhriLoEj7d"

        if not apify_api_key:
            raise HTTPException(status_code=500, detail="Apify API key is not configured.")

        client = ApifyClientAsync(apify_api_key)
        actor_client = client.actor(myhammer_actor_id)

        # === MyHammer pageFunction ===
        page_function_js = """
async function pageFunction(context) {
    const { jQuery: $, request, log, enqueueRequest } = context;
    log.info(`Scraping MyHammer reviews from: ${request.url}`);

    const reviewsData = [];
    let count = 0;

    function scrapeReviewsOnPage() {
        const results = [];
        $('ul[data-testid="reviews-list"] > li').each((_, el) => {
            const $el = $(el);
            const authorName = 
                $el.find('header span[class*="sc-11dd1852-1"]').text().trim() || 
                $el.find('header h3').text().trim();
            const ratingText = $el.find('div[aria-valuetext]').attr('aria-valuetext') || '';
            const ratingStars = parseInt(ratingText.match(/\\d+/)?.[0] || 0, 10);
            const createdDate = $el.find('span[class*="sc-8c00053-3"]').text().trim();
            const title = $el.find('section h4').first().text().trim() || null;
            const body = $el.find('section div[class*="sc-af155b8-0"]').first().text().trim() || null;
            const reply = $el.find('div[class*="sc-be98206e-0"] + div[class*="sc-be98206e-0"]').text().trim() || null;

            results.push({
                authorName,
                ratingStars,
                createdDate,
                title,
                body,
                reply
            });
        });
        return results;
    }

    // SCRAPE ONCE and APPEND
    const scrapedResults = scrapeReviewsOnPage();
    count += scrapedResults.length;
    reviewsData.push(...scrapedResults);
    
    log.info(`At count: ${count} reviews scraped`);
    log.info(`At reviewsData.length: ${reviewsData.length} reviews scraped`);

    // === Extract summary ===
    const averageScore = Number(
        $('[data-testid="rating-average"] strong').first().text().trim() || null
    );
    const reviewsTitle = $('[data-testid="sp-profile-reviews-title"]').text().trim() || "";
    
    // Remove invisible chars / normalize spaces
    const normalized = reviewsTitle.replace(/\\s+/g, " ");
    const totalReviewsMatch = normalized.match(/\\((\\d+)\\)/);
    const totalReviews = totalReviewsMatch ? Number(totalReviewsMatch[1]) : null;

    // === Stop after 100 reviews ===
    if (reviewsData.length >= 100 || count >= 100) {
        log.info('Reached 100 reviews — stopping pagination.');
        return {
            url: request.url,
            average_score: averageScore,
            total_reviews: totalReviews,
            reviews: reviewsData.slice(0, 100),
        };
    }

    // === Pagination ===
    const $next = $('a[data-testid="pagination-next-page"][aria-disabled="false"]');
    if ($next.length > 0 && reviewsData.length < 100) {
        const nextUrl = new URL($next.attr('href'), request.loadedUrl).toString();
        log.info(`Enqueuing next page: ${nextUrl}`);
        await enqueueRequest({ url: nextUrl });
    } else {
        log.info('No more next page found or review limit reached.');
    }

    return {
        url: request.url,
        average_score: averageScore,
        total_reviews: totalReviews,
        reviews: reviewsData.slice(0, 100),
    };
}
"""

        run_input = {
            "startUrls": [{"url": normalized_url}],
            "pageFunction": page_function_js,
            "maxResultsPerCrawl": 10
        }

        run = await actor_client.call(run_input=run_input)
        dataset_items = []
        if run and run.get("defaultDatasetId"):
            dataset_client = client.dataset(run["defaultDatasetId"])
            dataset_items = (await dataset_client.list_items()).items

        all_reviews = []
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
