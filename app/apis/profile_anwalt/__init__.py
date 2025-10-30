"""
This API module handles scraping of anwalt.de profile reviews using Apify.
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
import databutton as db
from apify_client import ApifyClientAsync
from firecrawl import FirecrawlApp
import requests
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

class FirecrawlRequest(BaseModel):
    url: str

class ZenRowsRequest(BaseModel):
    url: str
    apikey: str = "be561c60456543078b193964821d32cb50c3d64b"

# =====================
# Helper Functions
# =====================
def get_cache_key(url: str, page: int) -> str:
    """Generates a unique cache key for a given URL and page."""
    return f"anwalt_scrape_reviews_{hashlib.md5(f'{url}_{page}'.encode()).hexdigest()}"

# =====================
# API Endpoints
# =====================
@router.post("/profile_anwalt", response_model=ScrapeResponse)
@single_request_per_url
async def profile_anwalt(request: ScrapeRequest):
    """
    Scrapes an anwalt.de profile's review page using Apify, with a 24-hour cache TTL.
    """
    cache_key = get_cache_key(request.url, request.page)
    ttl = timedelta(hours=24)

    # 1. Check for cached data
    try:
        cached_entry = db.storage.json.get(cache_key)
        cached_timestamp = datetime.fromisoformat(cached_entry["timestamp"])
        
        if datetime.now(timezone.utc) - cached_timestamp < ttl:
            return ScrapeResponse(
                data=cached_entry["data"], 
                message="Data retrieved from cache.",
                pagination=cached_entry["pagination"]
            )
    except (FileNotFoundError, KeyError, TypeError):
        # Cache miss if file not found, or entry is malformed
        pass

    # 2. Scrape data using Apify if not cached or cache is stale
    try:
        apify_api_key = db.secrets.get("APIFY_API_KEY")
        # Use the correct Actor ID provided by the user
        anwalt_actor_id = "K0KedxhxRkmldOHtd"

        if not apify_api_key:
            raise HTTPException(status_code=500, detail="Apify API key is not configured.")

        client = ApifyClientAsync(apify_api_key)
        actor_client = client.actor(anwalt_actor_id)
        
        # The user-provided Javascript function to be executed on the page
        page_function_js = """
            async function pageFunction(context) {
            const $ = context.jQuery;
            const { request, log } = context;
            // Assuming 'enqueueRequest' is available on the context object for queuing new URLs
            const { enqueueRequest } = context; 

            log.info(`Scraping URL: ${request.url}`);

            const reviewsData = [];
            const REVIEWS_SELECTOR = '[data-test-id*="rating-item"]';

            const $reviews = $(REVIEWS_SELECTOR);

            if ($reviews.length === 0 && request.url.includes("bewertungen")) {
                log.error("The main review selector found ZERO elements. Check the selector or if the content loaded correctly.");
            }

            // --- 1. SCRAPE REVIEWS ---
            if (request.url.includes("bewertungen")) {
                $(REVIEWS_SELECTOR).each((index, element) => {
                    const $review = $(element);
                    let title = $review.find('.text-slate-900').text().trim();
                    const pillText = $review.find('.anw-pill').first().text().trim();
                    if (pillText) {
                        title = `${title} [${pillText}]`;
                    }

                    const body = $review.find('.text-slate-700').text().trim();

                    const authorAndDateText = $review.find('[data-test-id*="description"]').text().trim();

                    let authorName = 'N/A';
                    let createdDate = 'N/A';

                    if (authorAndDateText.startsWith('von ')) {
                        const parts = authorAndDateText.split(' am ');
                        if (parts.length > 1) {
                            authorName = parts[0].replace('von', '').trim();
                            createdDate = parts[1].replace(/um\s*|Uhr/g, '').trim();
                        } else {
                            authorName = authorAndDateText;
                        }
                    }

                    const rating = $review.find('svg.anw-rating.filled').length;

                    reviewsData.push({
                        title: title,
                        body: body,
                        ratingStars: rating,
                        authorName: authorName,
                        createdDate: createdDate,
                        authorAndDateText: authorAndDateText
                    });
                });
            }

            const score = $('.anw-h1').first().text().trim();
            const reviewsCount = $('.text-sm.text-neutral-600').first().text().trim();

            // --- 2. HANDLE PAGINATION (The Fix!) ---
            const $nextLink = $('li[data-test-id="arrow-page-right"] a');
            let nextUrl = null;

            // Check if the link element exists and has an 'href' attribute
            if ($nextLink.length > 0) {
                const relativeUrl = $nextLink.attr('href');
                
                if (relativeUrl) {
                    // Convert the relative URL (e.g., '?p=2') to an absolute URL
                    nextUrl = new URL(relativeUrl, request.url).href;
                    
                    // 🚨 THIS IS THE KEY TO ENQUEUING THE NEXT PAGE 🚨
                    await enqueueRequest({ url: nextUrl });
                    log.info(`Enqueued next page: ${nextUrl}`);
                }
            }

            // --- 3. RETURN DATA ---
            return {
                url: request.url,
                summaryScore: score,
                summaryReviewsCount: reviewsCount,
                reviews: reviewsData,
                // Optional: you can still return the nextUrl for logging/debugging
                nextUrl: nextUrl 
            };
        }
        """

        # Prepare the input for the actor run, now including the page function
        run_input = {
            "startUrls": [ 
                {"url": f"{request.url}/bewertungen.php?p={request.page}"}
            ],
            "pageFunction": page_function_js,
            "proxyConfiguration": {
                "useApifyProxy": True,
                "apifyProxyGroups": ["RESIDENTIAL"],
                "apifyProxyCountry": "DE"
            },
        }

        # Run the actor, providing the input for this specific run
        run = await actor_client.call(run_input=run_input)

        # Fetch the results from the run's default Dataset
        if run and run.get('defaultDatasetId'):
            dataset_client = client.dataset(run['defaultDatasetId'])
            dataset_items = (await dataset_client.list_items()).items
        else:
            dataset_items = []
        
        # Anwalt scraper now returns all pages with reviews, flatten them into a single list
        all_reviews = []
        total_reviews = None
        
        if dataset_items and len(dataset_items) > 0:
            # Extract total reviews from summary (from first page)
            if dataset_items[0].get("summaryReviewsCount"):
                import re
                total_match = re.search(r'\d+', dataset_items[0]["summaryReviewsCount"])
                if total_match:
                    total_reviews = int(total_match.group())
            
            # Flatten all reviews from all pages
            for page_data in dataset_items:
                page_reviews = page_data.get("reviews", [])
                all_reviews.extend(page_reviews)
            
            # Update the first item to contain all reviews in flat structure
            dataset_items = [{
                "url": dataset_items[0].get("url", request.url),
                "summaryScore": dataset_items[0].get("summaryScore", ""),
                "summaryReviewsCount": dataset_items[0].get("summaryReviewsCount", ""),
                "reviews": all_reviews
            }]
        
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

        # 3. Cache the new data with a timestamp
        new_cache_entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "data": dataset_items,
            "pagination": pagination_meta.dict()
        }
        db.storage.json.put(cache_key, new_cache_entry)

        message = "Data scraped successfully." if dataset_items else "No data found, but request was cached."
        return ScrapeResponse(data=dataset_items, message=message, pagination=pagination_meta)

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"An error occurred during scraping: {str(e)}")


@router.post("/profile_anwalt_firecrawl")
async def profile_anwalt_firecrawl(request: FirecrawlRequest):
    """
    Test endpoint to scrape anwalt.de profile using Firecrawl.
    Test URL: https://www.anwalt.de/andrea-schmidt/bewertungen.php?p=1
    """
    try:
        # Get Firecrawl API key from secrets
        firecrawl_api_key = db.secrets.get("FIRECRAWL_API_KEY")
        
        if not firecrawl_api_key:
            raise HTTPException(status_code=500, detail="Firecrawl API key is not configured.")
        
        # Initialize Firecrawl app
        app = FirecrawlApp(api_key=firecrawl_api_key)
        
        # Scrape the URL with custom extraction schema for anwalt.de reviews
        scrape_result = app.scrape_url(
            url=request.url,
            params={
                'extractorOptions': {
                    'extractionSchema': {
                        'type': 'object',
                        'properties': {
                            'summaryScore': {
                                'type': 'string',
                                'description': 'Overall rating score displayed prominently on the page'
                            },
                            'summaryReviewsCount': {
                                'type': 'string', 
                                'description': 'Total number of reviews text'
                            },
                            'reviews': {
                                'type': 'array',
                                'items': {
                                    'type': 'object',
                                    'properties': {
                                        'title': {
                                            'type': 'string',
                                            'description': 'Review title or heading'
                                        },
                                        'body': {
                                            'type': 'string',
                                            'description': 'Review content/text'
                                        },
                                        'ratingStars': {
                                            'type': 'integer',
                                            'description': 'Number of stars given (1-5)'
                                        },
                                        'authorName': {
                                            'type': 'string',
                                            'description': 'Name of the review author'
                                        },
                                        'createdDate': {
                                            'type': 'string',
                                            'description': 'Date when the review was created'
                                        }
                                    }
                                }
                            }
                        }
                    },
                     "mode": "llm-extraction"
                }
            }
        )
        
        # Extract the data from Firecrawl response
        extracted_data = scrape_result.get('data', {}).get('extracted_data', {})
        
        # Format response similar to Apify endpoint
        formatted_data = [{
            'url': request.url,
            'summaryScore': extracted_data.get('summaryScore', ''),
            'summaryReviewsCount': extracted_data.get('summaryReviewsCount', ''),
            'reviews': extracted_data.get('reviews', [])
        }]
        
        # Create pagination metadata
        reviews_count = len(extracted_data.get('reviews', []))
        pagination_meta = PaginationMeta(
            current_page=1,
            has_next_page=False,
            total_pages=1,
            total_reviews=reviews_count
        )
        
        return ScrapeResponse(
            data=formatted_data,
            message=f"Data scraped successfully using Firecrawl. Found {reviews_count} reviews.",
            pagination=pagination_meta
        )
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"An error occurred during Firecrawl scraping: {str(e)}")


@router.post("/profile_anwalt_zenrows")
async def profile_anwalt_zenrows(request: ZenRowsRequest):
    """
    Scrape anwalt.de profile using ZenRows API.
    Test URL: https://anwalt.de/andrea-schmidt/bewertungen.php?p=1
    """
    try:
        # Prepare ZenRows API request
        params = {
            'url': request.url,
            'apikey': request.apikey,
        }
        
        # Make request to ZenRows API
        response = requests.get('https://api.zenrows.com/v1/', params=params)
        
        if response.status_code != 200:
            raise HTTPException(
                status_code=response.status_code, 
                detail=f"ZenRows API request failed: {response.text}"
            )
        
        # Return raw HTML content for now
        # In a production environment, you would parse this HTML to extract reviews
        html_content = response.text
        
        # For demonstration, return the HTML length and first 500 characters
        preview = html_content[:500] + "..." if len(html_content) > 500 else html_content
        
        return {
            "url": request.url,
            "status": "success",
            "message": f"Successfully scraped {len(html_content)} characters from {request.url}",
            "html_length": len(html_content),
            "html_preview": preview,
            "full_html": html_content  # Include full HTML for processing
        }
        
    except requests.RequestException as e:
        raise HTTPException(status_code=500, detail=f"Request error: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"An error occurred during ZenRows scraping: {str(e)}")
