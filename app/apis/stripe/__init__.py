

"""
Stripe Subscription API

This API handles Stripe subscription management for Happy Client Flow:
- Create checkout sessions for new subscriptions
- Generate customer portal sessions for subscription management
- Check subscription status via database JOINs
- Process Stripe webhooks for subscription updates

Used by: Frontend subscription components, Stripe webhooks
"""

from datetime import datetime, timezone
from typing import List, Optional, Dict, Any
from fastapi import APIRouter, HTTPException, Request, Header, Depends
from pydantic import BaseModel
import stripe
import databutton as db
from supabase import create_client, Client
from app.libs.auth import require_auth
from app.env import Mode, mode
import asyncio

router = APIRouter(prefix="/stripe")

# Environment-based Stripe configuration
if mode == Mode.PROD:
    # Production: Use live Stripe keys
    stripe.api_key = db.secrets.get("STRIPE_SECRET_KEY_LIVE")
    STRIPE_WEBHOOK_SECRET = db.secrets.get("STRIPE_WEBHOOK_SECRET_LIVE")
else:
    # Development: Use test/sandbox Stripe keys
    stripe.api_key = db.secrets.get("STRIPE_SECRET_KEY_TEST")
    STRIPE_WEBHOOK_SECRET = db.secrets.get("STRIPE_WEBHOOK_SECRET_TEST")

# Initialize Supabase
supabase_url = db.secrets.get("SUPABASE_URL")
supabase_service_key = db.secrets.get("SUPABASE_SERVICE_KEY")
supabase: Client = create_client(supabase_url, supabase_service_key)

# Hardcoded price ID for Happy Client Flow Pro - replace with actual price ID
STRIPE_PRICE_ID = "price_1234567890"  # Replace with actual Stripe Price ID

# Pydantic Models
class CheckoutRequest(BaseModel):
    company_id: str
    success_url: str
    cancel_url: str

class CheckoutResponse(BaseModel):
    checkout_url: str
    session_id: str

class PortalRequest(BaseModel):
    company_id: str
    return_url: str

class PortalResponse(BaseModel):
    portal_url: str

class SubscriptionStatus(BaseModel):
    has_active_subscription: bool
    subscription_id: Optional[str] = None
    status: Optional[str] = None
    current_period_end: Optional[datetime] = None
    product_name: Optional[str] = None

@router.post("/create-checkout-session", response_model=CheckoutResponse)
async def create_checkout_session(request: CheckoutRequest, user_data: str = Depends(require_auth)):
    """
    Create a Stripe checkout session for subscribing to Happy Client Flow Pro
    """
    if not stripe.api_key:
        raise HTTPException(status_code=500, detail="Stripe not configured")
    try:
        # Get company info from database
        company_response = supabase.table('companies').select('*').eq('id', request.company_id).single().execute()
        
        if not company_response.data:
            raise HTTPException(status_code=404, detail="Company not found")
        
        company = company_response.data
        
        # Check if company already has an active subscription
        existing_sub = supabase.table('subscriptions').select('*').eq('company_id', request.company_id).eq('status', 'active').execute()
        
        if existing_sub.data:
            raise HTTPException(status_code=400, detail="Company already has an active subscription")
        
        # Create or retrieve Stripe customer
        customer_email = company.get('contact_email', '')
        customer_name = company.get('name', '')
        
        # Check if company already has a Stripe customer
        existing_customer_id = company.get('stripe_customer_id')
        
        if existing_customer_id:
            # Use existing customer
            try:
                customer = stripe.Customer.retrieve(existing_customer_id)
            except stripe.error.InvalidRequestError:
                # Customer doesn't exist in Stripe, create new one
                customer = stripe.Customer.create(
                    email=customer_email,
                    name=customer_name
                )
                # Update companies table with new customer ID
                supabase.table('companies').update({
                    'stripe_customer_id': customer.id
                }).eq('id', request.company_id).execute()
        else:
            # Try to find existing customer by email
            customers = stripe.Customer.list(email=customer_email, limit=1)
            
            if customers.data:
                customer = customers.data[0]
            else:
                # Create new customer
                customer = stripe.Customer.create(
                    email=customer_email,
                    name=customer_name
                )
            
            # Update companies table with customer ID
            supabase.table('companies').update({
                'stripe_customer_id': customer.id
            }).eq('id', request.company_id).execute()
        
        # Create checkout session
        session = stripe.checkout.Session.create(
            customer=customer.id,
            payment_method_types=['card'],
            line_items=[{
                'price': STRIPE_PRICE_ID,
                'quantity': 1,
            }],
            mode='subscription',
            success_url=request.success_url,
            cancel_url=request.cancel_url,
            metadata={
                'company_id': request.company_id
            }
        )
        
        return CheckoutResponse(
            checkout_url=session.url,
            session_id=session.id
        )
        
    except stripe.error.StripeError as e:
        raise HTTPException(status_code=400, detail=f"Stripe error: {str(e)}") from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal error: {str(e)}") from e

@router.post("/create-portal-session", response_model=PortalResponse)
async def create_portal_session(request: PortalRequest, current_user: str = Depends(require_auth)):
    """
    Create a Stripe customer portal session for subscription management
    """
    if not stripe.api_key:
        raise HTTPException(status_code=500, detail="Stripe not configured")
    
    print(f"[AUTH] Creating portal session for user: {current_user}")
    
    try:
        # Get subscription info to find Stripe customer
        sub_response = supabase.table('subscriptions').select('stripe_customer_id').eq('company_id', request.company_id).execute()
        
        if not sub_response.data:
            raise HTTPException(status_code=404, detail="No subscription found for this company")
        
        customer_id = sub_response.data[0]['stripe_customer_id']
        
        # Create portal session
        session = stripe.billing_portal.Session.create(
            customer=customer_id,
            return_url=request.return_url
        )
        
        return PortalResponse(portal_url=session.url)
        
    except stripe.error.StripeError as e:
        raise HTTPException(status_code=400, detail=f"Stripe error: {str(e)}") from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal error: {str(e)}") from e

@router.get("/subscription-status/{company_id}", response_model=SubscriptionStatus)
async def get_subscription_status(company_id: str, current_user: str = Depends(require_auth)):
    """
    Get current subscription status for a company using JOIN query
    """
    print(f"[AUTH] Getting subscription status for user: {current_user}")
    try:
        # Query with JOIN to get subscription details
        query = """
        SELECT 
            s.id as subscription_id,
            s.status,
            s.current_period_end,
            s.stripe_product_id
        FROM companies c
        LEFT JOIN subscriptions s ON c.id = s.company_id 
            AND s.status = 'active'
        WHERE c.id = %s
        """
        
        result = supabase.rpc('exec_sql', {'sql': query, 'params': [company_id]}).execute()
        
        if not result.data:
            return SubscriptionStatus(has_active_subscription=False)
        
        subscription_data = result.data[0] if result.data else {}
        
        has_active = bool(subscription_data.get('subscription_id'))
        
        return SubscriptionStatus(
            has_active_subscription=has_active,
            subscription_id=subscription_data.get('subscription_id'),
            status=subscription_data.get('status'),
            current_period_end=subscription_data.get('current_period_end'),
            product_name="Happy Client Flow Pro" if has_active else None
        )
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error checking subscription status: {str(e)}") from e

@router.post("/webhook")
async def stripe_webhook(request: Request, stripe_signature: str = Header(None)):
    """
    Handle Stripe webhooks for subscription events
    """
    if not stripe.api_key:
        raise HTTPException(status_code=500, detail="Stripe not configured")
    
    body = await request.body()
    
    try:
        event = stripe.Webhook.construct_event(
            body, stripe_signature, STRIPE_WEBHOOK_SECRET
        )
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid payload")
    except stripe.error.SignatureVerificationError:
        raise HTTPException(status_code=400, detail="Invalid signature")
    
    print(f"Received Stripe webhook: {event['type']}")
    
    try:
        # Handle checkout completion - this is where we capture the company mapping if this occurs before subscription.completed
        if event['type'] == 'checkout.session.completed':
            session = event['data']['object']
            customer_id = session.get('customer')
            
            # Try to get company_id from metadata first (most reliable)
            company_id = session.get('metadata', {}).get('company_id')

            # Fallback to client_reference_id if metadata is not available
            if not company_id:
                company_id = session.get('client_reference_id')

            if not company_id:
                print("Warning: No company reference found in checkout session")
                return {"status": "error", "message": "No company reference found"}

            if not customer_id:
                print("Warning: No customer ID found in checkout session")
                return {"status": "error", "message": "No customer ID found"}

            # Update company with stripe_customer_id
            supabase.table('companies').update({'stripe_customer_id': customer_id}).eq('id', company_id).execute()

            # Check for a floating subscription and update it
            # This may occur if customer.subscription.created occurs before this
            supabase.table('subscriptions').update({'company_id': company_id}).eq('stripe_customer_id', customer_id).execute()
            
            print(f"Successfully processed checkout for company {company_id} and customer {customer_id}")
            return {"status": "success"}

        # Handle subscription events - use the stored mapping
        elif event['type'] in ['customer.subscription.created', 'customer.subscription.updated',
                              'customer.subscription.deleted', 'invoice.payment_succeeded']:

            event_object = event['data']['object']
            customer_id = event_object.get('customer')

            # For invoice events, the customer is on the subscription, not the invoice
            if not customer_id and event_object.get('object') == 'invoice':
                subscription_id = event_object.get('subscription')
                if subscription_id:
                    try:
                        subscription = stripe.Subscription.retrieve(subscription_id)
                        customer_id = subscription.customer
                    except stripe.error.StripeError as e:
                        print(f"Error retrieving subscription {subscription_id} for invoice: {e}")

            if not customer_id:
                print(f"Could not determine customer from {event['type']} event")
                return {"status": "error", "message": "Could not determine customer"}

            # Look up company by the stored stripe_customer_id
            # If subscription event occurs first before session event, company will have no information, kept as None
            company_result = supabase.table('companies').select('id').eq('stripe_customer_id', customer_id).execute()
            company_id = company_result.data[0]['id'] if company_result.data else None

            # Handle specific subscription events
            if event['type'] in ['customer.subscription.created', 'customer.subscription.updated']:
                subscription = event_object
                subscription_data = {
                    'company_id': company_id,
                    'stripe_subscription_id': subscription['id'],
                    'stripe_customer_id': customer_id,
                    'stripe_product_id': subscription['items']['data'][0]['price']['product'] if subscription.get('items', {}).get('data') else None,
                    'stripe_price_id': subscription['items']['data'][0]['price']['id'] if subscription.get('items', {}).get('data') else None,
                    'status': subscription['status'],
                    'current_period_start': datetime.fromtimestamp(subscription['current_period_start'], tz=timezone.utc).isoformat(),
                    'current_period_end': datetime.fromtimestamp(subscription['current_period_end'], tz=timezone.utc).isoformat(),
                    'updated_at': datetime.now(timezone.utc).isoformat()
                }
                
                # Upsert subscription, allowing for floating subscriptions
                supabase.table('subscriptions').upsert(subscription_data, on_conflict='stripe_subscription_id').execute()
                print(f"Upserted subscription for company {company_id or 'unassigned'} with status {subscription['status']}")

            elif event['type'] == 'customer.subscription.deleted':
                subscription = event_object
                supabase.table('subscriptions').update({
                    'status': 'canceled',
                    'canceled_at': datetime.now(timezone.utc).isoformat(),
                    'updated_at': datetime.now(timezone.utc).isoformat()
                }).eq('stripe_subscription_id', subscription['id']).execute()
                print(f"Canceled subscription for company {company_id or 'unassigned'}")
            elif event['type'] == 'invoice.payment_succeeded':
                print(f"Payment succeeded for company {company_id}")
                # Could add logic here to update payment status if needed
        return {"status": "success"}
        
    except ValueError as e:
        print(f"Webhook signature verification failed: {str(e)}")
        raise HTTPException(status_code=400, detail="Invalid signature") from e
    except Exception as e:
        print(f"Webhook processing failed: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Webhook processing failed: {str(e)}") from e

@router.post("/webhook-v2")
async def stripe_webhook_v2(request: Request, stripe_signature: str = Header(None)):
    """
    A simple test webhook endpoint to log incoming requests from Stripe.
    """
    print("--- Received request on /webhook-v2 ---")
    
    # Log headers
    headers = dict(request.headers)
    print("Headers:")
    for key, value in headers.items():
        print(f"  {key}: {value}")
        
    # Log body
    body = await request.body()
    print("Body:")
    print(body.decode('utf-8'))

    try:
        event = stripe.Webhook.construct_event(
            body, stripe_signature, STRIPE_WEBHOOK_SECRET
        )
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid payload")
    except stripe.error.SignatureVerificationError:
        raise HTTPException(status_code=400, detail="Invalid signature")
    
    print("--- End of request on /webhook-v2 ---")
    
    return {"status": "received", "event": event['type']}

# Utility functions for checkout session completion
async def handle_checkout_completed(session):
    """
    Handle checkout session completion
    """
    try:
        company_id = session.get('client_reference_id')
        customer_id = session['customer']
        
        if not company_id:
            print("Warning: No client_reference_id found in checkout session")
            return
            
        # Store the stripe_customer_id in the companies table
        supabase.table('companies').update({
            'stripe_customer_id': customer_id,
            'updated_at': datetime.now(timezone.utc).isoformat()
        }).eq('id', company_id).execute()
        
        print(f"Updated company {company_id} with Stripe customer ID {customer_id}")
        
    except Exception as e:
        print(f"Error handling checkout completed: {str(e)}")

async def find_company_with_retry(customer_id: str, max_retries: int = 10, delay: float = 3.0) -> Optional[str]:
    """
    Find company by stripe_customer_id with retry mechanism and fallback lookups
    
    Args:
        customer_id: Stripe customer ID
        max_retries: Maximum number of retry attempts (default: 10)
        delay: Delay between retries in seconds (default: 3.0)
    
    Returns:
        Company ID if found, None otherwise
    """
    for attempt in range(max_retries + 1):
        try:
            # Primary lookup: by stripe_customer_id
            company_result = supabase.table('companies').select('*').eq('stripe_customer_id', customer_id).execute()
            
            if company_result.data:
                company_id = company_result.data[0]['id']
                print(f"Found company {company_id} for customer {customer_id} on attempt {attempt + 1}")
                return company_id
            
            # Fallback lookup: by customer email if primary fails
            try:
                stripe_customer = stripe.Customer.retrieve(customer_id)
                if stripe_customer.email:
                    email_result = supabase.table('companies').select('*').eq('contact_email', stripe_customer.email).execute()
                    
                    if email_result.data:
                        company_id = email_result.data[0]['id']
                        print(f"Found company {company_id} by email fallback for customer {customer_id} on attempt {attempt + 1}")
                        
                        # Update the company with stripe_customer_id for future lookups
                        supabase.table('companies').update({
                            'stripe_customer_id': customer_id
                        }).eq('id', company_id).execute()
                        
                        return company_id
            except stripe.error.StripeError as e:
                print(f"Stripe API error during fallback lookup: {str(e)}")
            
            # If this is the last attempt, don't wait
            if attempt < max_retries:
                print(f"Company not found for customer {customer_id}, attempt {attempt + 1}/{max_retries + 1}. Retrying in {delay} seconds...")
                await asyncio.sleep(delay)
            else:
                print(f"Company not found for customer {customer_id} after {max_retries + 1} attempts")
                
        except Exception as e:
            print(f"Error during company lookup attempt {attempt + 1}: {str(e)}")
            if attempt < max_retries:
                await asyncio.sleep(delay)
    
    return None
