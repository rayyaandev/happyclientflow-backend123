"""
This API module handles scraping of Booking.com profile reviews using Apify.
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


class BookingReview(BaseModel):
    authorName: str = None
    rating: float = None
    title: str = None
    content: str = None
    date: str = None
    country: str = None


class ScrapedProfile(BaseModel):
    url: str
    hotelName: str = None
    overallRating: float = None
    totalReviews: int = None
    totalScraped: int = None
    reviews: list[BookingReview]


class ScrapeResponse(BaseModel):
    data: list[ScrapedProfile]
    message: str
    pagination: PaginationMeta


# =====================
# Helper Functions
# =====================
def normalize_booking_url(url: str) -> str:
    """
    Normalizes Booking.com URL to ensure proper format with #tab-reviews anchor.
    
    Examples:
    - https://www.booking.com/hotel/de/hotel-name.html -> https://www.booking.com/hotel/de/hotel-name.html#tab-reviews
    - https://www.booking.com/hotel/de/hotel-name.html#tab-reviews -> https://www.booking.com/hotel/de/hotel-name.html#tab-reviews
    """
    from urllib.parse import urlparse, urlunparse
    
    parsed = urlparse(url)
    
    # Ensure the fragment is set to tab-reviews
    fragment = "tab-reviews"
    
    # Reconstruct the URL
    normalized_url = urlunparse((
        parsed.scheme,
        parsed.netloc,
        parsed.path,
        parsed.params,
        parsed.query,
        fragment
    ))
    
    return normalized_url


def get_cache_key(url: str, page: int) -> str:
    """Generates a unique cache key for a given URL and page."""
    return f"booking_scrape_all_{hashlib.md5(url.encode()).hexdigest()}"


# =====================
# API Endpoints
# =====================
@router.post("/profile_booking", response_model=ScrapeResponse)
@single_request_per_url
async def profile_booking(request: ScrapeRequest):
    """
    Scrapes a Booking.com profile's review page using Apify, with a 24-hour cache TTL.
    Automatically normalizes URLs to include #tab-reviews anchor.
    """
    # Normalize the URL to ensure proper format
    normalized_url = normalize_booking_url(request.url)
    
    # Generate cache key
    cache_key = hashlib.md5(f"booking_{normalized_url}_{request.page}".encode()).hexdigest()
    
    # 1. Try returning from cache
    try:
        cached_entry = db.storage.json.get(cache_key)
        cached_timestamp = datetime.fromisoformat(cached_entry["timestamp"])
        if datetime.now(timezone.utc) - cached_timestamp < timedelta(hours=24):
            cached_data = cached_entry["data"]
            if cached_data and len(cached_data) > 0:
                scraped_data = cached_data[0]
                all_reviews = scraped_data.get("reviews", [])
                
                # Convert reviews to BookingReview format
                formatted_reviews = []
                for review in all_reviews:
                    formatted_reviews.append(BookingReview(
                        authorName=review.get("authorName"),
                        rating=review.get("rating"),
                        title=review.get("title"),
                        content=review.get("content"),
                        date=review.get("date"),
                        country=review.get("country")
                    ))
                
                response_data = [
                    ScrapedProfile(
                        url=scraped_data.get("url", request.url),
                        hotelName=scraped_data.get("hotelName"),
                        overallRating=scraped_data.get("overallRating"),
                        totalReviews=scraped_data.get("totalReviews"),
                        totalScraped=scraped_data.get("totalScraped"),
                        reviews=formatted_reviews,
                    )
                ]
                pagination_meta = PaginationMeta(
                    current_page=request.page,
                    has_next_page=False,
                    total_pages=1,
                    total_reviews=scraped_data.get("totalReviews") or len(all_reviews),
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
        booking_actor_id = "s780flaJuoRMl9Gla"  # Using the same actor as other endpoints

        if not apify_api_key:
            raise HTTPException(status_code=500, detail="Apify API key is not configured.")

        client = ApifyClientAsync(apify_api_key)
        actor_client = client.actor(booking_actor_id)

        # === Booking.com pageFunction ===
        page_function_js = """
async function pageFunction(context) {
    const { jQuery: $, request, log, page } = context;
    log.info(`Starting Booking.com scrape from: ${request.url}`);

    // Wait for reviews to load - they might be loaded dynamically
    log.info('Waiting for reviews to load...');
    
    // Try multiple selectors and wait for content to appear
    const reviewSelectors = [
        '.be659bb4c2[data-testid="review-card"]',
        '[data-testid="review-card"]',
        '.c-review',
        '.review-item'
    ];
    
    let reviewsLoaded = false;
    for (let i = 0; i < 10 && !reviewsLoaded; i++) {
        await new Promise(resolve => setTimeout(resolve, 2000)); // Wait 2 seconds
        
        for (const selector of reviewSelectors) {
            if ($(selector).length > 0) {
                log.info(`Found reviews with selector: ${selector}`);
                reviewsLoaded = true;
                break;
            }
        }
        
        if (!reviewsLoaded) {
            log.info(`Attempt ${i + 1}: No reviews found yet, waiting...`);
        }
    }
    
    if (!reviewsLoaded) {
        log.info('No reviews found after waiting. Checking page content...');
        log.info(`Page title: ${$('title').text()}`);
        log.info(`Page has ${$('*').length} total elements`);
    }

    const reviews = [];
    
    // Extract hotel name and overall rating
    const hotelName = $('h2[data-testid="property-name"]').text().trim() || 
                     $('.pp-header__title').text().trim() || 
                     $('h1').first().text().trim() || null;
    
    // Extract overall rating from the score section
    const overallRatingText = $('.bc946a29db').first().text().trim() || 
                             $('[data-testid="review-score-value"]').text().trim() || 
                             $('.bui-review-score__badge').text().trim() || null;
    const overallRating = overallRatingText ? parseFloat(overallRatingText.replace('Scored ', '').replace(',', '.')) : null;
    
    // Extract total reviews count
    const totalReviewsText = $('.fff1944c52.fb14de7f14.eaa8455879').text().trim() || 
                            $('[data-testid="review-score-subtitle"]').text().trim() || 
                            $('.bui-review-score__text').text().trim() || null;
    const totalReviews = totalReviewsText ? parseInt(totalReviewsText.replace(/[^0-9,]/g, '').replace(/,/g, '')) : null;
    
    log.info(`Hotel: ${hotelName}, Overall Rating: ${overallRating}, Total Reviews: ${totalReviews}`);

    // Function to scrape reviews from current page
    function scrapeCurrentPage() {
        // Try multiple selectors to find reviews
        let reviewElements = $();
        const selectors = [
            '.be659bb4c2[data-testid="review-card"]',
            '[data-testid="review-card"]',
            '.c-review',
            '.review-item'
        ];
        
        for (const selector of selectors) {
            reviewElements = $(selector);
            if (reviewElements.length > 0) {
                log.info(`Using selector: ${selector} - found ${reviewElements.length} reviews`);
                break;
            }
        }
        
        const newReviews = [];
        
        reviewElements.each((index, element) => {
            const $review = $(element);
            
            // Extract author name - look for the traveller name in the reviewer section
            const authorName = $review.find('.b08850ce41.f546354b44').text().trim() || 
                              $review.find('[data-testid="review-author-name"]').text().trim() || null;
            
            // Extract country from flag section
            const country = $review.find('.d838fb5f41.aea5eccb71').text().trim() || null;
            
            // Extract rating from review score section
            const ratingText = $review.find('[data-testid="review-score"] .bc946a29db').text().trim();
            const rating = ratingText ? parseFloat(ratingText.replace('Scored ', '').replace(',', '.')) : null;
            
            // Extract review title
            const title = $review.find('[data-testid="review-title"]').text().trim() || null;
            
            // Extract date
            const date = $review.find('[data-testid="review-date"]').text().trim().replace('Reviewed: ', '') || null;
            
            // Extract positive and negative content
            let content = '';
            const positiveText = $review.find('[data-testid="review-positive-text"] .b99b6ef58f.d14152e7c3 span').text().trim();
            const negativeText = $review.find('[data-testid="review-negative-text"] .b99b6ef58f.d14152e7c3 span').text().trim();
            
            if (positiveText && negativeText) {
                content = `Positive: ${positiveText} | Negative: ${negativeText}`;
            } else if (positiveText) {
                content = positiveText;
            } else if (negativeText) {
                content = negativeText;
            }
            
            log.info(`Review ${index + 1} - Author: "${authorName}", Title: "${title}", Content length: ${content.length}, Rating: ${rating}`);
            
            if (authorName || content) {
                newReviews.push({
                    authorName,
                    rating,
                    title,
                    content: content || null,
                    date,
                    country
                });
            }
        });
        
        log.info(`Extracted ${newReviews.length} reviews from current page`);
        return newReviews;
    }

    // Scrape initial page
    reviews.push(...scrapeCurrentPage());
    log.info(`Scraped ${reviews.length} reviews from initial page`);

    // Try to load more reviews by clicking pagination
    let pageNum = 1;
    log.info(`Starting pagination. Current reviews: ${reviews.length}`);
    
    while (reviews.length < 50 && pageNum < 10) { // Reduced limits to avoid detection
        // Look for the "Next page" button based on the actual HTML structure
        const nextButton = $('button[aria-label="Next page"]:not([disabled])');
        
        if (nextButton.length === 0 || nextButton.prop('disabled')) {
            log.info('No more pages available');
            break;
        }

        pageNum++;
        log.info(`Going to page ${pageNum}`);
        
        // Add random human-like delay before clicking
        const randomDelay = Math.random() * 2000 + 1000; // 1-3 seconds
        await new Promise(resolve => setTimeout(resolve, randomDelay));
        
        nextButton[0].click();

        // Wait for page to load with realistic delay
        const pageLoadDelay = Math.random() * 3000 + 4000; // 4-7 seconds
        await new Promise(resolve => setTimeout(resolve, pageLoadDelay));

        const newReviews = scrapeCurrentPage();
        reviews.push(...newReviews);
        
        if (newReviews.length === 0) {
            log.info('No new reviews found. Stopping pagination.');
            break;
        }
        
        // Add delay between pages to appear more human-like
        if (pageNum < 10) {
            const betweenPagesDelay = Math.random() * 2000 + 1000; // 1-3 seconds
            await new Promise(resolve => setTimeout(resolve, betweenPagesDelay));
        }
    }

    // Remove duplicates based on content and author
    const uniqueReviews = [];
    const seen = new Set();
    
    for (const review of reviews) {
        const key = `${review.authorName}_${review.content}_${review.date}`;
        if (!seen.has(key)) {
            seen.add(key);
            uniqueReviews.push(review);
        }
    }

    return {
        url: request.url,
        hotelName,
        overallRating,
        totalReviews,
        totalScraped: uniqueReviews.length,
        reviews: uniqueReviews
    };
}
"""

        run_input = {
            "startUrls": [{"url": normalized_url}],
            "pageFunction": page_function_js,
            "maxResultsPerCrawl": 10,
            "proxyConfiguration": {
                "useApifyProxy": True,
                "apifyProxyGroups": ["RESIDENTIAL"],
                "apifyProxyCountry": "US"
            },
            "maxRequestRetries": 3,
            "requestTimeoutSecs": 120,
            "handlePageTimeoutSecs": 300,
            "launchOptions": {
                "useChrome": True,
                "stealth": True
            }
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

        # Convert reviews to BookingReview format
        formatted_reviews = []
        for review in all_reviews:
            formatted_reviews.append(BookingReview(
                authorName=review.get("authorName"),
                rating=review.get("rating"),
                title=review.get("title"),
                content=review.get("content"),
                date=review.get("date"),
                country=review.get("country")
            ))

        pagination_meta = PaginationMeta(
            current_page=request.page,
            has_next_page=False,
            total_pages=1,
            total_reviews=scraped_data.get("totalReviews") or len(all_reviews),
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
                hotelName=scraped_data.get("hotelName"),
                overallRating=scraped_data.get("overallRating"),
                totalReviews=scraped_data.get("totalReviews"),
                totalScraped=len(formatted_reviews),
                reviews=formatted_reviews,
            )
        ]

        message = "Data scraped successfully." if all_reviews else "No data found, but request was cached."
        return ScrapeResponse(data=response_data, message=message, pagination=pagination_meta)

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"An error occurred during scraping: {str(e)}")
