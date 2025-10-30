"""
This API module handles scraping of Trustpilot profile reviews using Apify.
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


class TrustpilotReview(BaseModel):
    author: str
    rating: int
    date: str
    title: str
    content: str


class ScrapedProfile(BaseModel):
    url: str
    name: str = None
    totalReviews: int = None
    rating: float = None
    reviewsScraped: int = None
    reviews: list[TrustpilotReview]


class ScrapeResponse(BaseModel):
    data: list[ScrapedProfile]
    message: str
    pagination: PaginationMeta


# =====================
# Helper Functions
# =====================
def normalize_trustpilot_url(url: str, page: int = 1) -> str:
    """
    Normalizes Trustpilot URL to ensure proper format with page parameter.
    
    Examples:
    - https://www.trustpilot.com/review/domain.com -> https://www.trustpilot.com/review/domain.com?page=1
    - https://www.trustpilot.com/review/domain.com/de -> https://www.trustpilot.com/review/domain.com/de?page=1
    """
    from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
    
    parsed = urlparse(url)
    
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
        parsed.path,
        parsed.params,
        new_query,
        parsed.fragment
    ))
    
    return normalized_url


def get_cache_key(url: str, page: int) -> str:
    """Generates a unique cache key for a given URL and page."""
    return f"trustpilot_scrape_all_{hashlib.md5(url.encode()).hexdigest()}"


# =====================
# API Endpoints
# =====================
@router.post("/profile_trustpilot", response_model=ScrapeResponse)
@single_request_per_url
async def profile_trustpilot(request: ScrapeRequest):
    """
    Scrapes a Trustpilot profile's review page using Apify, with a 24-hour cache TTL.
    Automatically normalizes URLs to include page parameters.
    """
    # Normalize the URL to ensure proper format
    normalized_url = normalize_trustpilot_url(request.url, request.page)
    
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
                
                # Convert reviews to TrustpilotReview format
                formatted_reviews = []
                for review in all_reviews:
                    formatted_reviews.append(TrustpilotReview(
                        author=review.get("author", ""),
                        rating=review.get("rating", 0),
                        date=review.get("date", ""),
                        title=review.get("title", ""),
                        content=review.get("content", "")
                    ))
                
                response_data = [
                    ScrapedProfile(
                        url=scraped_data.get("url", request.url),
                        name=scraped_data.get("name"),
                        totalReviews=scraped_data.get("totalReviews"),
                        rating=scraped_data.get("rating"),
                        reviewsScraped=len(formatted_reviews),
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
        trustpilot_actor_id = "Zq2h7ls7hDsqe2VgF"  # Using same actor as MyHammer for now

        if not apify_api_key:
            raise HTTPException(status_code=500, detail="Apify API key is not configured.")

        client = ApifyClientAsync(apify_api_key)
        actor_client = client.actor(trustpilot_actor_id)

        # === Trustpilot pageFunction ===
        page_function_js = """
async function pageFunction(context) {
    const { jQuery: $, request, log, enqueueRequest } = context;
    log.info(`🔍 Starting Trustpilot scraping from: ${request.url}`);
    
    // Log page structure for debugging
    log.info(`📄 Page title: ${$('title').text()}`);
    log.info(`📄 Total elements on page: ${$('*').length}`);

    const reviewsData = [];
    let count = 0;
    function scrapeReviewsOnPage() {
        const results = [];
        
        // Try multiple selectors to find review cards
        log.info(`🔍 Looking for review cards...`);
        
        let reviewCards = $('div[data-testid="service-review-card-v2"]');
        log.info(`📊 Found ${reviewCards.length} cards with data-testid="service-review-card-v2"`);
        
        if (reviewCards.length === 0) {
            // Try alternative selectors
            reviewCards = $('div[data-service-review-card-paper]');
            log.info(`📊 Fallback: Found ${reviewCards.length} cards with data-service-review-card-paper`);
        }
        
        if (reviewCards.length === 0) {
            // Try even more generic selectors
            reviewCards = $('article, .review, [class*="review"], [data-testid*="review"]');
            log.info(`📊 Generic fallback: Found ${reviewCards.length} potential review elements`);
        }
        
        if (reviewCards.length === 0) {
            log.error(`❌ No review cards found! Available data-testid attributes:`);
            $('[data-testid]').each((i, el) => {
                if (i < 10) { // Log first 10 to avoid spam
                    log.info(`   - ${$(el).attr('data-testid')}`);
                }
            });
        }
        
        reviewCards.each((index, el) => {
            const $el = $(el);
            log.info(`\\n🔍 Processing review card ${index + 1}/${reviewCards.length}`);
            
            // Extract author name with multiple attempts
            log.info(`👤 Looking for author name...`);
            let author = $el.find('span[data-consumer-name-typography="true"]').text().trim();
            log.info(`   - span[data-consumer-name-typography="true"]: "${author}"`);
            
            if (!author) {
                author = $el.find('a[data-consumer-profile-link="true"] span').first().text().trim();
                log.info(`   - a[data-consumer-profile-link="true"] span: "${author}"`);
            }
            
            if (!author) {
                author = $el.find('.styles_consumerName__xKr9c, [class*="consumerName"], [class*="author"]').text().trim();
                log.info(`   - Generic author selectors: "${author}"`);
            }
            
            // Extract rating with detailed logging
            log.info(`⭐ Looking for rating...`);
            let rating = 0;
            let ratingElement = $el.find('div[data-service-review-rating]');
            log.info(`   - div[data-service-review-rating] found: ${ratingElement.length}`);
            
            if (ratingElement.length > 0) {
                const ratingAttr = ratingElement.attr('data-service-review-rating');
                log.info(`   - data-service-review-rating attribute: "${ratingAttr}"`);
                rating = parseInt(ratingAttr) || 0;
                
                if (!rating) {
                    const imgElement = ratingElement.find('img');
                    if (imgElement.length > 0) {
                        const altText = imgElement.attr('alt') || '';
                        log.info(`   - img alt text: "${altText}"`);
                        const ratingMatch = altText.match(/Rated (\\d+) out of 5 stars/);
                        rating = ratingMatch ? parseInt(ratingMatch[1]) : 0;
                    }
                }
            }
            log.info(`   - Final rating: ${rating}`);
            
            // Extract date with detailed logging
            log.info(`📅 Looking for date...`);
            let dateElement = $el.find('time[data-service-review-date-time-ago="true"]');
            log.info(`   - time[data-service-review-date-time-ago="true"] found: ${dateElement.length}`);
            
            if (dateElement.length === 0) {
                dateElement = $el.find('time');
                log.info(`   - generic time elements found: ${dateElement.length}`);
            }
            
            const date = dateElement.length > 0 ? 
                        (dateElement.attr('datetime') || dateElement.text().trim()) : '';
            log.info(`   - Final date: "${date}"`);
            
            // Extract title with detailed logging
            log.info(`📝 Looking for title...`);
            let title = $el.find('h2[data-service-review-title-typography="true"]').text().trim();
            log.info(`   - h2[data-service-review-title-typography="true"]: "${title}"`);
            
            if (!title) {
                title = $el.find('a[data-review-title-typography="true"] h2').text().trim();
                log.info(`   - a[data-review-title-typography="true"] h2: "${title}"`);
            }
            
            if (!title) {
                title = $el.find('h1, h2, h3, h4, [class*="title"], [class*="heading"]').first().text().trim();
                log.info(`   - Generic title selectors: "${title}"`);
            }
            
            // Extract content with detailed logging
            log.info(`📄 Looking for content...`);
            let content = $el.find('p[data-service-review-text-typography="true"]').text().trim();
            log.info(`   - p[data-service-review-text-typography="true"]: "${content.substring(0, 100)}..."`);
            
            if (!content) {
                content = $el.find('div[data-review-content="true"] p').text().trim();
                log.info(`   - div[data-review-content="true"] p: "${content.substring(0, 100)}..."`);
            }
            
            if (!content) {
                content = $el.find('p, .content, [class*="content"], [class*="text"]').first().text().trim();
                log.info(`   - Generic content selectors: "${content.substring(0, 100)}..."`);
            }
            
            // Validation and final logging
            log.info(`✅ Review data summary:`);
            log.info(`   - Author: "${author}"`);
            log.info(`   - Rating: ${rating}`);
            log.info(`   - Date: "${date}"`);
            log.info(`   - Title: "${title}"`);
            log.info(`   - Content length: ${content.length}`);
            
            // Only add review if we have essential data
            if (author && (content || title)) {
                const reviewData = { 
                    author, 
                    rating, 
                    date, 
                    title: title || (content ? content.substring(0, 50) + '...' : 'No title'), 
                    content: content || title || ''
                };
                
                log.info(`✅ Adding review to results`);
                results.push(reviewData);
            } else {
                log.warning(`❌ Skipping review - missing essential data`);
                log.warning(`   - Has author: ${!!author}`);
                log.warning(`   - Has content: ${!!content}`);
                log.warning(`   - Has title: ${!!title}`);
            }
        });
        count += results.length;
        return results;
    }

    reviewsData.push(...scrapeReviewsOnPage());
    
    log.info(`\\n📊 SUMMARY EXTRACTION`);
    log.info(`📊 Total reviews found: ${reviewsData.length}`);

    // === Extract summary with detailed logging ===
    log.info(`🏢 Looking for business rating...`);
    let ratingElement = $('p[data-rating-typography="true"]').first();
    log.info(`   - p[data-rating-typography="true"] found: ${ratingElement.length}`);
    
    if (ratingElement.length === 0) {
        ratingElement = $('span[data-rating-typography="true"]').first();
        log.info(`   - span[data-rating-typography="true"] found: ${ratingElement.length}`);
    }
    
    const rating = ratingElement.length > 0 ? parseFloat(ratingElement.text().trim()) : null;
    log.info(`   - Final rating: ${rating}`);
    
    log.info(`🏢 Looking for business name...`);
    let nameElement = $('span.title_displayName__9lGaz').first();
    log.info(`   - span.title_displayName__9lGaz found: ${nameElement.length}`);
    
    if (nameElement.length === 0) {
        nameElement = $('#business-unit-title h1 span').first();
        log.info(`   - #business-unit-title h1 span found: ${nameElement.length}`);
    }
    
    if (nameElement.length === 0) {
        nameElement = $('h1.title_title__pKuza span').first();
        log.info(`   - h1.title_title__pKuza span found: ${nameElement.length}`);
    }
    
    const name = nameElement.length > 0 ? nameElement.text().trim().replace(/\\s*&nbsp;\\s*$/, '') : 
                 $('h1').first().text().trim() || 
                 'Unknown Business';
    log.info(`   - Final name: "${name}"`);
    
    log.info(`📊 Looking for total review count...`);
    let totalReviews = null;
    let totalReviewsElement = $('span.styles_reviewsAndRating__OIRXy').first();
    log.info(`   - span.styles_reviewsAndRating__OIRXy found: ${totalReviewsElement.length}`);
    
    if (totalReviewsElement.length > 0) {
        const reviewText = totalReviewsElement.text().trim();
        log.info(`   - Review text: "${reviewText}"`);
        log.info(`   - Review text length: ${reviewText.length}`);
        log.info(`   - Review text char codes: ${Array.from(reviewText).map(c => c.charCodeAt(0)).join(', ')}`);
        
        // Extract number from "Reviews 32,174" using string manipulation
        // Split by "Reviews" and get the part after it
        const parts = reviewText.split('Reviews');
        log.info(`   - Split parts: ${JSON.stringify(parts)}`);
        
        if (parts.length > 1) {
            const numberPart = parts[1].trim();
            log.info(`   - Number part after Reviews: "${numberPart}"`);
            
            // Remove any non-digit and non-comma characters, then parse
            const cleanNumber = numberPart.replace(/[^0-9,]/g, '');
            log.info(`   - Clean number string: "${cleanNumber}"`);
            
            if (cleanNumber && cleanNumber.length > 1) {  // Must have more than just comma
                totalReviews = parseInt(cleanNumber.replace(/,/g, ''));
                log.info(`   - Successfully extracted count: ${totalReviews}`);
            } else {
                log.warning(`   - No valid number found after cleaning: "${cleanNumber}"`);
                
                // Alternative approach: extract digits only
                const digitsOnly = numberPart.replace(/[^0-9]/g, '');
                log.info(`   - Digits only: "${digitsOnly}"`);
                
                if (digitsOnly && digitsOnly.length > 0) {
                    totalReviews = parseInt(digitsOnly);
                    log.info(`   - Successfully extracted count from digits: ${totalReviews}`);
                }
            }
        } else {
            log.warning(`   - Could not split text by 'Reviews'`);
        }
    }
    
    if (totalReviews === null) {
        // Fallback: look for any element containing review count
        totalReviewsElement = $('*:contains("Reviews")').filter(function() {
            return $(this).text().match(/Reviews\\s+[\\d,]+/);
        }).first();
        log.info(`   - Fallback review count elements found: ${totalReviewsElement.length}`);
        
        if (totalReviewsElement.length > 0) {
            const reviewText = totalReviewsElement.text();
            const reviewMatch = reviewText.match(/Reviews[\\s\\u00A0]+([\\d,]+)/) || 
                               reviewText.match(/([\\d,]+)$/);
            if (reviewMatch) {
                totalReviews = parseInt(reviewMatch[1].replace(/,/g, ''));
                log.info(`   - Fallback extracted count: ${totalReviews}`);
            }
        }
    }
    
    if (totalReviews === null) {
        log.info(`   - No review count found`);
    } else {
        log.info(`   - Final review count: ${totalReviews}`);
    }

    // === Final Results ===
    log.info(`\\n🎯 FINAL RESULTS SUMMARY`);
    log.info(`   - URL: ${request.url}`);
    log.info(`   - Business Name: "${name}"`);
    log.info(`   - Overall Rating: ${rating}`);
    log.info(`   - Total Reviews Available: ${totalReviews}`);
    log.info(`   - Reviews Scraped: ${reviewsData.length}`);
    
    // === Stop after 100 reviews ===
    if (reviewsData.length >= 100 || count >= 100) {
        log.info('✅ Reached 100 reviews — stopping pagination.');
        const finalResult = {
            url: request.url,
            name: name,
            rating: rating,
            totalReviews: totalReviews,
            reviewsScraped: reviewsData.length,
            reviews: reviewsData.slice(0, 100),
        };
        log.info(`🎯 Returning final result with ${finalResult.reviews.length} reviews`);
        return finalResult;
    }

    // === Pagination ===
    log.info(`🔄 Looking for pagination...`);
    let $next = $('a[data-pagination-button-next-link="true"]');
    log.info(`   - Next button with data-pagination-button-next-link found: ${$next.length}`);
    
    if ($next.length === 0) {
        $next = $('a[name="pagination-button-next"]');
        log.info(`   - Next button with name="pagination-button-next" found: ${$next.length}`);
    }
    
    if ($next.length === 0) {
        $next = $('a[rel="next"]');
        log.info(`   - Next button with rel="next" found: ${$next.length}`);
    }
    
    log.info(`   - Next button disabled: ${$next.attr('disabled') || $next.attr('aria-disabled')}`);
    log.info(`   - Current review count: ${reviewsData.length}`);
    
    if ($next.length > 0 && !$next.attr('disabled') && $next.attr('aria-disabled') !== 'true' && reviewsData.length < 100) {
        const nextHref = $next.attr('href');
        log.info(`   - Next href: ${nextHref}`);
        
        if (nextHref) {
            const nextUrl = new URL(nextHref, request.loadedUrl).toString();
            log.info(`🔄 Enqueuing next page: ${nextUrl}`);
            await enqueueRequest({ url: nextUrl });
        } else {
            log.warning(`❌ Next button found but no href attribute`);
        }
    } else {
        log.info('⏹️ No more next page found or review limit reached.');
        if ($next.length > 0) {
            log.info(`   - Button exists but disabled or aria-disabled: ${$next.attr('disabled')} / ${$next.attr('aria-disabled')}`);
        }
    }

    const finalResult = {
        url: request.url,
        name: name,
        rating: rating,
        totalReviews: totalReviews,
        reviewsScraped: reviewsData.length,
        reviews: reviewsData.slice(0, 100),
    };
    
    log.info(`🎯 Returning final result with ${finalResult.reviews.length} reviews`);
    return finalResult;
}
"""

        run_input = {
            "startUrls": [{"url": normalized_url}],
            "pageFunction": page_function_js,
            "maxResultsPerCrawl": 10,
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

        # Convert reviews to TrustpilotReview format
        formatted_reviews = []
        for review in all_reviews:
            formatted_reviews.append(TrustpilotReview(
                author=review.get("author", ""),
                rating=review.get("rating", 0),
                date=review.get("date", ""),
                title=review.get("title", ""),
                content=review.get("content", "")
            ))

        pagination_meta = PaginationMeta(
            current_page=request.page,
            has_next_page=False,
            total_pages=1,
            total_reviews=scraped_data.get("totalReviews") if dataset_items else len(all_reviews),
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
                name=scraped_data.get("name", "Unknown Business"),
                totalReviews=scraped_data.get("totalReviews"),
                rating=scraped_data.get("rating"),
                reviewsScraped=len(formatted_reviews),
                reviews=formatted_reviews,
            )
        ]

        message = "Data scraped successfully." if all_reviews else "No data found, but request was cached."
        return ScrapeResponse(data=response_data, message=message, pagination=pagination_meta)

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"An error occurred during scraping: {str(e)}")
