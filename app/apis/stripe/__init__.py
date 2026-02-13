

"""
Stripe Subscription API

This API handles Stripe subscription management for Happy Client Flow:
- Create checkout sessions for new subscriptions
- Generate customer portal sessions for subscription management
- Check subscription status via database JOINs
- Process Stripe webhooks for subscription updates

Used by: Frontend subscription components, Stripe webhooks
"""

import os
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any
from fastapi import APIRouter, HTTPException, Request, Header, Depends
from pydantic import BaseModel
import stripe
import databutton as db
import os
import json
from supabase import create_client, Client
from app.libs.auth import require_auth
from app.libs.pricing_config import (
    resolve_plan_from_lookup_key,
    is_extra_seat_lookup_key,
    get_plan_lookup_key,
    get_extra_seat_lookup_key,
    PLANS,
)
from app.env import Mode, mode
import asyncio

router = APIRouter(prefix="/stripe")

# ---------------------
# Debug helpers
# ---------------------
def _secret_debug_info(value: Optional[str]) -> str:
    """
    Return safe info about a secret without exposing it.
    """
    if not value:
        return "missing"
    v = str(value)
    prefix = "whsec_" if v.startswith("whsec_") else ("sk_live_" if v.startswith("sk_live_") else ("sk_test_" if v.startswith("sk_test_") else "set"))
    return f"{prefix} (len={len(v)})"

def _get_secret(key: str) -> Optional[str]:
    """Get secret from env var first, then fallback to databutton"""
    value = os.environ.get(key)
    if value:
        return value
    try:
        return db.secrets.get(key)
    except Exception as e:
        print(f"[STRIPE] Warning: Failed to get secret {key}: {e}")
        return None

# Initialize Stripe with fallback to databutton secrets
stripe.api_key = _get_secret("STRIPE_SECRET_KEY_TEST")
STRIPE_WEBHOOK_SECRET = _get_secret("STRIPE_WEBHOOK_SECRET")

# Startup debug (safe)
print("[STRIPE] Startup config")
print(f"[STRIPE] DATABUTTON_SERVICE_TYPE={os.environ.get('DATABUTTON_SERVICE_TYPE')!r} -> mode={mode}")
print(f"[STRIPE] stripe.api_key: {_secret_debug_info(stripe.api_key)}")
print(f"[STRIPE] STRIPE_WEBHOOK_SECRET: {_secret_debug_info(STRIPE_WEBHOOK_SECRET)}")

# Initialize Supabase
supabase_url = _get_secret("SUPABASE_URL")
supabase_service_key = _get_secret("SUPABASE_SERVICE_KEY")
supabase: Client = create_client(supabase_url, supabase_service_key)

# Pydantic Models
class CheckoutRequest(BaseModel):
    company_id: str
    success_url: str
    cancel_url: str
    plan_type: str  # "starter" or "business"
    billing_cycle: str  # "monthly" or "annual"
    extra_seats: int = 0  # additional seats beyond the plan's included users

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
    Create a Stripe checkout session for subscribing to a Happy Client Flow plan.
    Supports Starter/Business plans with monthly/annual billing and extra seats.
    """
    if not stripe.api_key:
        raise HTTPException(status_code=500, detail="Stripe not configured")

    # Validate plan parameters
    if request.plan_type not in PLANS:
        raise HTTPException(status_code=400, detail=f"Invalid plan_type: {request.plan_type}. Must be 'starter' or 'business'.")
    if request.billing_cycle not in ('monthly', 'annual'):
        raise HTTPException(status_code=400, detail=f"Invalid billing_cycle: {request.billing_cycle}. Must be 'monthly' or 'annual'.")
    if request.extra_seats < 0:
        raise HTTPException(status_code=400, detail="extra_seats cannot be negative.")

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
        existing_customer_id = company.get('stripe_customer_id')

        if existing_customer_id:
            try:
                customer = stripe.Customer.retrieve(existing_customer_id)
            except stripe.InvalidRequestError:
                customer = stripe.Customer.create(
                    email=customer_email,
                    name=customer_name
                )
                supabase.table('companies').update({
                    'stripe_customer_id': customer.id
                }).eq('id', request.company_id).execute()
        else:
            customers = stripe.Customer.list(email=customer_email, limit=1)
            if customers.data:
                customer = customers.data[0]
            else:
                customer = stripe.Customer.create(
                    email=customer_email,
                    name=customer_name
                )
            supabase.table('companies').update({
                'stripe_customer_id': customer.id
            }).eq('id', request.company_id).execute()

        # Resolve Stripe price IDs via lookup_keys
        plan_lookup_key = get_plan_lookup_key(request.plan_type, request.billing_cycle)
        base_prices = stripe.Price.list(lookup_keys=[plan_lookup_key], active=True)
        if not base_prices.data:
            raise HTTPException(status_code=500, detail=f"Stripe price not found for lookup_key: {plan_lookup_key}")

        line_items = [{'price': base_prices.data[0].id, 'quantity': 1}]

        # Add extra seat line item if needed
        if request.extra_seats > 0:
            seat_lookup_key = get_extra_seat_lookup_key(request.billing_cycle)
            seat_prices = stripe.Price.list(lookup_keys=[seat_lookup_key], active=True)
            if not seat_prices.data:
                raise HTTPException(status_code=500, detail=f"Stripe price not found for lookup_key: {seat_lookup_key}")
            line_items.append({'price': seat_prices.data[0].id, 'quantity': request.extra_seats})

        # Create checkout session with plan metadata
        checkout_params = {
            'customer': customer.id,
            'payment_method_types': ['card'],
            'line_items': line_items,
            'mode': 'subscription',
            'success_url': request.success_url,
            'cancel_url': request.cancel_url,
            'subscription_data': {
                'metadata': {
                    'company_id': request.company_id,
                    'plan_type': request.plan_type,
                    'billing_cycle': request.billing_cycle,
                    'extra_seats': str(request.extra_seats),
                }
            },
            'metadata': {
                'company_id': request.company_id,
                'plan_type': request.plan_type,
                'billing_cycle': request.billing_cycle,
                'extra_seats': str(request.extra_seats),
            },
        }

        # Apply 30% discount coupon for annual subscriptions
        if request.billing_cycle == 'annual':
            checkout_params['discounts'] = [{'coupon': 'J8nDaYwq'}]

        session = stripe.checkout.Session.create(**checkout_params)

        return CheckoutResponse(
            checkout_url=session.url,
            session_id=session.id
        )

    except HTTPException:
        raise
    except stripe.StripeError as e:
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

        print(f"[PORTAL] Customer ID: {customer_id}")
        print(f"[PORTAL] Using Stripe key prefix: {stripe.api_key[:12]}...")

        # List portal configurations to debug
        configs = stripe.billing_portal.Configuration.list(limit=5)
        for cfg in configs.data:
            print(f"[PORTAL] Config id={cfg.id}, is_default={cfg.is_default}, active={cfg.active}, features.subscription_update.enabled={cfg.features.subscription_update.enabled if hasattr(cfg.features, 'subscription_update') else 'N/A'}")

        # Create portal session with explicit configuration
        session = stripe.billing_portal.Session.create(
            customer=customer_id,
            return_url=request.return_url,
            configuration="bpc_1Sxkx3FSVJlbi6y6Dl8ELUGT",
        )

        print(f"[PORTAL] Session URL: {session.url}")
        print(f"[PORTAL] Session config: {session.configuration}")

        return PortalResponse(portal_url=session.url)
        
    except stripe.StripeError as e:
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

class CompanyUserCount(BaseModel):
    active_users: int
    pending_invites: int
    total: int

@router.get("/company-user-count/{company_id}", response_model=CompanyUserCount)
async def get_company_user_count(company_id: str, current_user: str = Depends(require_auth)):
    """
    Get the count of active users and pending invites for a company.
    Used by the pricing page and team management to show seat usage.
    """
    try:
        users_result = supabase.table('users').select('id', count='exact').eq('company_id', company_id).execute()
        invites_result = supabase.table('invites').select('id', count='exact').eq('company_id', company_id).eq('status', 'Pending').execute()

        active_users = users_result.count or 0
        pending_invites = invites_result.count or 0

        return CompanyUserCount(
            active_users=active_users,
            pending_invites=pending_invites,
            total=active_users + pending_invites,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error counting users: {str(e)}") from e

class UpdateSeatsRequest(BaseModel):
    company_id: str
    new_extra_seats: int  # New total of extra seats (not a delta)

class ChangePlanRequest(BaseModel):
    company_id: str
    new_plan_type: str  # "starter" or "business"


@router.post("/update-seats")
async def update_seats(request: UpdateSeatsRequest, user_data: str = Depends(require_auth)):
    """
    Update the number of extra seats on an existing subscription.
    Modifies the Stripe subscription line item for extra seats.
    Stripe webhook will auto-update local DB when subscription.updated fires.
    """
    if not stripe.api_key:
        raise HTTPException(status_code=500, detail="Stripe not configured")

    if request.new_extra_seats < 0:
        raise HTTPException(status_code=400, detail="Extra seats cannot be negative.")

    try:
        # Get current subscription
        sub_result = supabase.table('subscriptions').select('*').eq(
            'company_id', request.company_id
        ).in_('status', ['active', 'trialing']).maybe_single().execute()

        if not sub_result.data:
            raise HTTPException(status_code=404, detail="No active subscription found.")

        sub = sub_result.data
        plan_type = sub.get('plan_type', 'starter')
        billing_cycle = sub.get('billing_cycle', 'monthly')
        included_users = sub.get('included_users', 3)
        new_max = included_users + request.new_extra_seats

        # Validate: new max must accommodate current users + pending invites
        from app.libs.user_limits import check_user_limit
        limit_info = await check_user_limit(request.company_id, supabase)
        current_users = limit_info['current_users']

        if new_max < current_users:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot reduce to {new_max} seats. You currently have {current_users} users/invites. Remove users first."
            )

        # Retrieve the Stripe subscription
        try:
            stripe_sub = stripe.Subscription.retrieve(sub['stripe_subscription_id'])
        except stripe.InvalidRequestError:
            raise HTTPException(
                status_code=404,
                detail="Subscription not found in Stripe. It may have been deleted or created in a different environment. Please re-subscribe."
            )

        # Find existing line items
        base_item = None
        seat_item = None
        print(f"[UPDATE-SEATS] Subscription items count: {len(stripe_sub['items']['data'])}")
        for item in stripe_sub['items']['data']:
            price_obj = item.get('price', {})
            lookup_key = price_obj.get('lookup_key', '') or ''
            print(f"[UPDATE-SEATS] Item id={item['id']}, price_id={price_obj.get('id')}, lookup_key={lookup_key!r}, qty={item.get('quantity')}")
            if resolve_plan_from_lookup_key(lookup_key):
                base_item = item
            elif is_extra_seat_lookup_key(lookup_key):
                seat_item = item

        print(f"[UPDATE-SEATS] base_item={'found' if base_item else 'NOT FOUND'}, seat_item={'found' if seat_item else 'NOT FOUND'}")
        print(f"[UPDATE-SEATS] Requested new_extra_seats={request.new_extra_seats}, billing_cycle={billing_cycle}")

        items_update = []

        if request.new_extra_seats > 0:
            # Resolve extra seat price
            seat_lookup_key = get_extra_seat_lookup_key(billing_cycle)
            seat_prices = stripe.Price.list(lookup_keys=[seat_lookup_key], active=True)
            print(f"[UPDATE-SEATS] Looking for seat price with lookup_key={seat_lookup_key!r}, found={len(seat_prices.data)} prices")
            if not seat_prices.data:
                raise HTTPException(status_code=500, detail=f"Extra seat price not found for lookup_key '{seat_lookup_key}'. Make sure you've created a price with this lookup key in your Stripe dashboard.")

            if seat_item:
                # Update existing seat line item quantity
                items_update.append({
                    'id': seat_item['id'],
                    'quantity': request.new_extra_seats,
                })
            else:
                # Add new seat line item
                items_update.append({
                    'price': seat_prices.data[0].id,
                    'quantity': request.new_extra_seats,
                })
        elif seat_item:
            # Remove seat line item (set to deleted)
            items_update.append({
                'id': seat_item['id'],
                'deleted': True,
            })

        print(f"[UPDATE-SEATS] items_update={items_update}")

        if items_update:
            modified_sub = stripe.Subscription.modify(
                sub['stripe_subscription_id'],
                items=items_update,
                proration_behavior='always_invoice',
                billing_cycle_anchor='now',
                metadata={
                    'company_id': request.company_id,
                    'plan_type': plan_type,
                    'billing_cycle': billing_cycle,
                    'extra_seats': str(request.new_extra_seats),
                }
            )
            print(f"[UPDATE-SEATS] Stripe subscription modified successfully. Status: {modified_sub.get('status')}")

            # Update Supabase immediately so frontend gets fresh data without waiting for webhook
            # Note: max_users is a generated column, so we only update the source columns
            supabase.table('subscriptions').update({
                'extra_seats': request.new_extra_seats,
            }).eq('id', sub['id']).execute()
        else:
            print(f"[UPDATE-SEATS] WARNING: items_update is empty, no Stripe modification made!")

        return {"status": "success", "new_extra_seats": request.new_extra_seats, "new_max_users": new_max}

    except HTTPException:
        raise
    except stripe.StripeError as e:
        raise HTTPException(status_code=400, detail=f"Stripe error: {str(e)}") from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal error: {str(e)}") from e


@router.post("/change-plan")
async def change_plan(request: ChangePlanRequest, user_data: str = Depends(require_auth)):
    """
    Change the base plan (starter <-> business) on an existing subscription.
    Swaps the base plan price on the Stripe subscription with proration.
    Stripe webhook will auto-update local DB when subscription.updated fires.
    """
    if not stripe.api_key:
        raise HTTPException(status_code=500, detail="Stripe not configured")

    if request.new_plan_type not in PLANS:
        raise HTTPException(status_code=400, detail=f"Invalid plan type: {request.new_plan_type}")

    try:
        # Get current subscription
        sub_result = supabase.table('subscriptions').select('*').eq(
            'company_id', request.company_id
        ).in_('status', ['active', 'trialing']).maybe_single().execute()

        if not sub_result.data:
            raise HTTPException(status_code=404, detail="No active subscription found.")

        sub = sub_result.data
        current_plan = sub.get('plan_type', 'starter')
        billing_cycle = sub.get('billing_cycle', 'monthly')
        extra_seats = sub.get('extra_seats', 0)

        if current_plan == request.new_plan_type:
            raise HTTPException(status_code=400, detail="Already on this plan.")

        new_included = PLANS[request.new_plan_type]['included_users']
        new_max = new_included + extra_seats

        # If downgrading, validate user count fits
        if PLANS.get(current_plan, {}).get('included_users', 0) > new_included:
            from app.libs.user_limits import check_user_limit
            limit_info = await check_user_limit(request.company_id, supabase)
            current_users = limit_info['current_users']

            if current_users > new_max:
                raise HTTPException(
                    status_code=400,
                    detail=f"Cannot downgrade: you have {current_users} users/invites but the new plan allows only {new_max}. Remove users first."
                )

        # Retrieve the Stripe subscription
        try:
            stripe_sub = stripe.Subscription.retrieve(sub['stripe_subscription_id'])
        except stripe.InvalidRequestError:
            raise HTTPException(
                status_code=404,
                detail="Subscription not found in Stripe. It may have been deleted or created in a different environment. Please re-subscribe."
            )

        # Find the base plan line item
        base_item = None
        for item in stripe_sub['items']['data']:
            lookup_key = item.get('price', {}).get('lookup_key', '') or ''
            if resolve_plan_from_lookup_key(lookup_key):
                base_item = item
                break

        if not base_item:
            raise HTTPException(status_code=500, detail="Could not find base plan item on Stripe subscription.")

        # Resolve new plan price
        new_lookup_key = get_plan_lookup_key(request.new_plan_type, billing_cycle)
        new_prices = stripe.Price.list(lookup_keys=[new_lookup_key], active=True)
        if not new_prices.data:
            raise HTTPException(status_code=500, detail=f"Price not found for {new_lookup_key}")

        # Swap the base plan — charge immediately and reset billing cycle
        stripe.Subscription.modify(
            sub['stripe_subscription_id'],
            items=[{
                'id': base_item['id'],
                'price': new_prices.data[0].id,
            }],
            proration_behavior='always_invoice',
            billing_cycle_anchor='now',
            metadata={
                'company_id': request.company_id,
                'plan_type': request.new_plan_type,
                'billing_cycle': billing_cycle,
                'extra_seats': str(extra_seats),
            }
        )

        # Update Supabase immediately so frontend gets fresh data without waiting for webhook
        # Note: max_users is a generated column, so we only update the source columns
        supabase.table('subscriptions').update({
            'plan_type': request.new_plan_type,
            'included_users': new_included,
        }).eq('id', sub['id']).execute()

        return {
            "status": "success",
            "new_plan_type": request.new_plan_type,
            "new_included_users": new_included,
            "new_max_users": new_max,
        }

    except HTTPException:
        raise
    except stripe.StripeError as e:
        raise HTTPException(status_code=400, detail=f"Stripe error: {str(e)}") from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal error: {str(e)}") from e


@router.post("/webhook")
async def stripe_webhook(request: Request, stripe_signature: str = Header(None)):
    """
    Handle Stripe webhooks for subscription events
    """
    if not stripe.api_key:
        raise HTTPException(status_code=500, detail="Stripe not configured")
    
    body = await request.body()
    # Safe runtime debug
    print("[STRIPE] Webhook received")
    print(f"[STRIPE] mode={mode} DATABUTTON_SERVICE_TYPE={os.environ.get('DATABUTTON_SERVICE_TYPE')!r}")
    print(f"[STRIPE] stripe.api_key: {(stripe.api_key)}")
    print(f"[STRIPE] STRIPE_WEBHOOK_SECRET: {(STRIPE_WEBHOOK_SECRET)}")
    print(f"[STRIPE] stripe_signature header present: {bool(stripe_signature)}")
    if stripe_signature:
        # Print a short prefix only; full header is sensitive.
        print(f"[STRIPE] stripe_signature prefix: {stripe_signature[:24]}...")
    print(f"[STRIPE] raw body length: {len(body)} bytes")
    
    try:
        event = stripe.Webhook.construct_event(
            body, stripe_signature, STRIPE_WEBHOOK_SECRET
        )
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid payload")
    except Exception as e:
        # Stripe library versions differ: exception may live under stripe._error
        sig_err_type = getattr(getattr(stripe, "_error", None), "SignatureVerificationError", None)
        if sig_err_type and isinstance(e, sig_err_type):
            # Helpful debug: inspect unverified payload for mode/type (do NOT trust it for business logic)
            try:
                payload = json.loads(body.decode("utf-8"))
                livemode = payload.get("livemode")
                event_type = payload.get("type")
                event_id = payload.get("id")
                print(f"[STRIPE] Unverified payload hints: livemode={livemode} type={event_type} id={event_id}")
            except Exception:
                print("[STRIPE] Could not parse payload JSON for livemode/type debug")
            print(f"[STRIPE] Signature verification failed: {str(e)}")
            raise HTTPException(status_code=400, detail="Invalid signature")
        # Fallback: re-raise unexpected errors
        print(f"[STRIPE] Unexpected error during signature verification: {type(e).__name__}: {e}")
        raise
    
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
                    except stripe.StripeError as e:
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

                # Extract plan metadata from subscription items
                items = subscription.get('items', {}).get('data', [])
                plan_info = None
                extra_seats = 0

                for item in items:
                    price = item.get('price', {})
                    lookup_key = price.get('lookup_key', '') or ''

                    # Check if this item is a base plan
                    resolved = resolve_plan_from_lookup_key(lookup_key)
                    if resolved:
                        plan_info = resolved

                    # Check if this item is extra seats
                    if is_extra_seat_lookup_key(lookup_key):
                        extra_seats = item.get('quantity', 0)

                if plan_info:
                    subscription_data['plan_type'] = plan_info['plan_type']
                    subscription_data['billing_cycle'] = plan_info['billing_cycle']
                    subscription_data['included_users'] = plan_info['included_users']
                    subscription_data['extra_seats'] = extra_seats
                    print(f"[STRIPE] Plan resolved: {plan_info['plan_type']} ({plan_info['billing_cycle']}), extra_seats={extra_seats}")
                else:
                    # Fallback: check checkout session metadata
                    checkout_meta = subscription.get('metadata', {})
                    if checkout_meta.get('plan_type'):
                        from app.libs.pricing_config import PLANS
                        pt = checkout_meta['plan_type']
                        bc = checkout_meta.get('billing_cycle', 'monthly')
                        es = int(checkout_meta.get('extra_seats', '0'))
                        if pt in PLANS:
                            subscription_data['plan_type'] = pt
                            subscription_data['billing_cycle'] = bc
                            subscription_data['included_users'] = PLANS[pt]['included_users']
                            subscription_data['extra_seats'] = es
                            print(f"[STRIPE] Plan from metadata: {pt} ({bc}), extra_seats={es}")

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
    except stripe.SignatureVerificationError:
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
            except stripe.StripeError as e:
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
