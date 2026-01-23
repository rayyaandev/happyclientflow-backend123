"""
Stripe Connect API

This API handles Stripe Connect integration for Happy Client Flow referral program:
- Create Stripe Express accounts for referrers
- Generate OAuth onboarding URLs
- Handle OAuth callbacks
- Process Stripe Connect webhooks for account updates
- Check account status

Used by: Frontend referral signup components, Stripe Connect webhooks
"""

from datetime import datetime, timezone
from typing import Optional, Dict, Any
from fastapi import APIRouter, HTTPException, Request, Header
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
import stripe
import databutton as db
from supabase import create_client, Client
from app.env import Mode, mode

router = APIRouter(prefix="/stripe-connect")

# Environment-based Stripe configuration
if mode == Mode.PROD:
    # Production: Use live Stripe keys
    stripe.api_key = db.secrets.get("STRIPE_SECRET_KEY_LIVE")
    STRIPE_CONNECT_CLIENT_ID = db.secrets.get("STRIPE_CONNECT_CLIENT_ID_LIVE")
    STRIPE_CONNECT_WEBHOOK_SECRET = db.secrets.get("STRIPE_CONNECT_WEBHOOK_SECRET_LIVE")
else:
    # Development: Use test/sandbox Stripe keys
    stripe.api_key = db.secrets.get("STRIPE_SECRET_KEY_TEST")
    STRIPE_CONNECT_CLIENT_ID = db.secrets.get("STRIPE_CONNECT_CLIENT_ID_TEST") or "ca_TWYtgz8CqADQz1KSx3pWoNwcq5NufrhG"
    STRIPE_CONNECT_WEBHOOK_SECRET = db.secrets.get("STRIPE_CONNECT_WEBHOOK_SECRET_TEST") or "whsec_lARJBdIPYYfhjHJ2xzzQJqrt2tfiw7sW"

# Initialize Supabase
supabase_url = db.secrets.get("SUPABASE_URL")
supabase_service_key = db.secrets.get("SUPABASE_SERVICE_KEY")
supabase: Client = create_client(supabase_url, supabase_service_key)

# Pydantic Models
class CreateAccountLinkRequest(BaseModel):
    referrer_id: str
    country_code: str
    return_url: str
    refresh_url: str

class CreateAccountLinkResponse(BaseModel):
    account_id: str
    onboarding_url: str
    expires_at: int

class RefreshAccountLinkRequest(BaseModel):
    referrer_id: str
    return_url: str
    refresh_url: str

class AccountStatusResponse(BaseModel):
    account_id: Optional[str] = None
    status: Optional[str] = None
    details_submitted: bool = False
    charges_enabled: bool = False
    payouts_enabled: bool = False
    onboarded: bool = False


@router.post("/create-account-link", response_model=CreateAccountLinkResponse)
async def create_account_link(request: CreateAccountLinkRequest):
    """
    Create a Stripe Connect Express account and generate onboarding link

    This endpoint:
    1. Checks if referrer already has a Stripe account
    2. Creates new Express account if needed (or reuses existing)
    3. Generates AccountLink for OAuth onboarding
    4. Returns onboarding URL for frontend redirect
    """
    if not stripe.api_key:
        raise HTTPException(status_code=500, detail="Stripe not configured")

    try:
        # Fetch referrer from database
        referrer_response = supabase.table('referrers')\
            .select('*, clients!inner(email, first_name, last_name)')\
            .eq('id', request.referrer_id)\
            .single()\
            .execute()

        if not referrer_response.data:
            raise HTTPException(status_code=404, detail="Referrer not found")

        referrer = referrer_response.data
        customer = referrer.get('clients', {})
        customer_email = customer.get('email', '')
        customer_name = f"{customer.get('first_name', '')} {customer.get('last_name', '')}".strip()

        # Check if referrer already has a Stripe Connect account
        existing_account_id = referrer.get('stripe_connect_account_id')

        if existing_account_id:
            # Verify account still exists in Stripe
            try:
                account = stripe.Account.retrieve(existing_account_id)
                print(f"[StripeConnect] Using existing account {existing_account_id} for referrer {request.referrer_id}")
            except stripe.error.InvalidRequestError:
                # Account doesn't exist, create new one
                existing_account_id = None
                print(f"[StripeConnect] Existing account {existing_account_id} not found in Stripe, will create new one")

        # Create new Stripe Express account if needed
        if not existing_account_id:
            account = stripe.Account.create(
                type="express",
                country=request.country_code,
                email=customer_email,
                capabilities={
                    "transfers": {"requested": True},
                },
                business_type="individual",
                metadata={
                    "referrer_id": request.referrer_id,
                    "customer_email": customer_email,
                }
            )

            # Store account ID in database
            supabase.table('referrers').update({
                'stripe_connect_account_id': account.id,
                'stripe_connect_status': account.status,
                'country_code': request.country_code,
            }).eq('id', request.referrer_id).execute()

            print(f"[StripeConnect] Created new account {account.id} for referrer {request.referrer_id}")
        else:
            account = stripe.Account.retrieve(existing_account_id)

        # Create account link for onboarding
        account_link = stripe.AccountLink.create(
            account=account.id,
            refresh_url=request.refresh_url,
            return_url=request.return_url,
            type="account_onboarding",
            collect="eventually_due",
        )

        print(f"[StripeConnect] Generated onboarding link for account {account.id}")

        return CreateAccountLinkResponse(
            account_id=account.id,
            onboarding_url=account_link.url,
            expires_at=account_link.expires_at
        )

    except stripe.error.StripeError as e:
        print(f"[StripeConnect] Stripe error: {str(e)}")
        raise HTTPException(status_code=400, detail=f"Stripe error: {str(e)}") from e
    except Exception as e:
        print(f"[StripeConnect] Internal error: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Internal error: {str(e)}") from e


@router.post("/refresh-account-link", response_model=CreateAccountLinkResponse)
async def refresh_account_link(request: RefreshAccountLinkRequest):
    """
    Refresh an expired Stripe Connect account link

    Account links expire after 60 minutes. This endpoint generates a new one.
    """
    if not stripe.api_key:
        raise HTTPException(status_code=500, detail="Stripe not configured")

    try:
        # Fetch referrer from database
        referrer_response = supabase.table('referrers')\
            .select('stripe_connect_account_id')\
            .eq('id', request.referrer_id)\
            .single()\
            .execute()

        if not referrer_response.data:
            raise HTTPException(status_code=404, detail="Referrer not found")

        account_id = referrer_response.data.get('stripe_connect_account_id')

        if not account_id:
            raise HTTPException(status_code=400, detail="Referrer has no Stripe Connect account")

        # Verify account exists
        try:
            account = stripe.Account.retrieve(account_id)
        except stripe.error.InvalidRequestError:
            raise HTTPException(status_code=404, detail="Stripe account not found")

        # Create new account link
        account_link = stripe.AccountLink.create(
            account=account_id,
            refresh_url=request.refresh_url,
            return_url=request.return_url,
            type="account_onboarding",
            collect="eventually_due",
        )

        print(f"[StripeConnect] Refreshed onboarding link for account {account_id}")

        return CreateAccountLinkResponse(
            account_id=account_id,
            onboarding_url=account_link.url,
            expires_at=account_link.expires_at
        )

    except stripe.error.StripeError as e:
        print(f"[StripeConnect] Stripe error: {str(e)}")
        raise HTTPException(status_code=400, detail=f"Stripe error: {str(e)}") from e
    except Exception as e:
        print(f"[StripeConnect] Internal error: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Internal error: {str(e)}") from e


@router.get("/oauth/callback")
async def oauth_callback(request: Request):
    """
    Handle Stripe Connect OAuth callback

    Stripe redirects here after user completes or exits onboarding.
    We simply redirect back to the app. Actual status updates come via webhooks.
    """
    # Extract query params
    params = dict(request.query_params)

    print(f"[StripeConnect] OAuth callback received with params: {params}")

    # Stripe doesn't send useful params on success/failure here
    # Account updates come via webhooks
    # Just log and return success

    return {
        "status": "callback_received",
        "message": "Stripe Connect callback processed. Account updates will be sent via webhook."
    }


@router.get("/account-status/{referrer_id}", response_model=AccountStatusResponse)
async def get_account_status(referrer_id: str):
    """
    Get current Stripe Connect account status for a referrer

    Used by frontend to poll account status after OAuth redirect
    """
    try:
        # Fetch referrer from database
        referrer_response = supabase.table('referrers')\
            .select('stripe_connect_account_id, stripe_connect_status, stripe_connect_details_submitted, stripe_connect_payouts_enabled')\
            .eq('id', referrer_id)\
            .single()\
            .execute()

        if not referrer_response.data:
            raise HTTPException(status_code=404, detail="Referrer not found")

        referrer = referrer_response.data
        account_id = referrer.get('stripe_connect_account_id')

        if not account_id:
            # No Stripe account yet
            return AccountStatusResponse(
                account_id=None,
                status=None,
                details_submitted=False,
                charges_enabled=False,
                payouts_enabled=False,
                onboarded=False
            )

        # Fetch fresh account data from Stripe
        try:
            account = stripe.Account.retrieve(account_id)

            # Extract capabilities
            capabilities = account.get('capabilities', {})
            transfers_capability = capabilities.get('transfers', 'inactive')

            # Determine if onboarding is complete
            details_submitted = account.get('details_submitted', False)
            charges_enabled = account.get('charges_enabled', False)
            payouts_enabled = transfers_capability == 'active'

            # Update database with fresh data
            supabase.table('referrers').update({
                'stripe_connect_status': account.status,
                'stripe_connect_details_submitted': details_submitted,
                'stripe_connect_charges_enabled': charges_enabled,
                'stripe_connect_payouts_enabled': payouts_enabled,
                'stripe_connect_onboarded_at': datetime.now(timezone.utc).isoformat() if (details_submitted and payouts_enabled) else None
            }).eq('id', referrer_id).execute()

            return AccountStatusResponse(
                account_id=account_id,
                status=account.status,
                details_submitted=details_submitted,
                charges_enabled=charges_enabled,
                payouts_enabled=payouts_enabled,
                onboarded=(details_submitted and payouts_enabled)
            )

        except stripe.error.InvalidRequestError:
            # Account doesn't exist in Stripe
            return AccountStatusResponse(
                account_id=account_id,
                status="deleted",
                details_submitted=False,
                charges_enabled=False,
                payouts_enabled=False,
                onboarded=False
            )

    except Exception as e:
        print(f"[StripeConnect] Error getting account status: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Internal error: {str(e)}") from e


@router.post("/webhook")
async def stripe_connect_webhook(request: Request, stripe_signature: str = Header(None)):
    """
    Handle Stripe Connect webhooks for account updates

    Events handled:
    - account.updated: Account status changed (onboarding complete, capabilities enabled)
    - account.external_account.created: Bank account connected
    - capability.updated: Payout capability status changed
    """
    if not stripe.api_key:
        raise HTTPException(status_code=500, detail="Stripe not configured")

    body = await request.body()

    try:
        event = stripe.Webhook.construct_event(
            body, stripe_signature, STRIPE_CONNECT_WEBHOOK_SECRET
        )
    except ValueError:
        print("[StripeConnect] Webhook - Invalid payload")
        raise HTTPException(status_code=400, detail="Invalid payload")
    except stripe.error.SignatureVerificationError:
        print("[StripeConnect] Webhook - Invalid signature")
        raise HTTPException(status_code=400, detail="Invalid signature")

    print(f"[StripeConnect] Received webhook: {event['type']}")

    try:
        # Handle account.updated event
        if event['type'] == 'account.updated':
            account = event['data']['object']
            account_id = account['id']

            print(f"[StripeConnect] Processing account.updated for {account_id}")

            # Find referrer by account ID
            referrer_response = supabase.table('referrers')\
                .select('id')\
                .eq('stripe_connect_account_id', account_id)\
                .execute()

            if not referrer_response.data:
                print(f"[StripeConnect] No referrer found for account {account_id}")
                return {"status": "warning", "message": "No referrer found for account"}

            referrer_id = referrer_response.data[0]['id']

            # Extract account details
            capabilities = account.get('capabilities', {})
            transfers_capability = capabilities.get('transfers', 'inactive')

            details_submitted = account.get('details_submitted', False)
            charges_enabled = account.get('charges_enabled', False)
            payouts_enabled = transfers_capability == 'active'

            # Update referrer record
            update_data = {
                'stripe_connect_status': account.get('status'),
                'stripe_connect_details_submitted': details_submitted,
                'stripe_connect_charges_enabled': charges_enabled,
                'stripe_connect_payouts_enabled': payouts_enabled,
            }

            # Set onboarded timestamp if just completed
            if details_submitted and payouts_enabled:
                # Check if not already onboarded
                existing_referrer = supabase.table('referrers').select('stripe_connect_onboarded_at').eq('id', referrer_id).single().execute()
                if not existing_referrer.data.get('stripe_connect_onboarded_at'):
                    update_data['stripe_connect_onboarded_at'] = datetime.now(timezone.utc).isoformat()

            supabase.table('referrers').update(update_data).eq('id', referrer_id).execute()

            print(f"[StripeConnect] Updated referrer {referrer_id} - payouts_enabled: {payouts_enabled}")

            return {"status": "success", "referrer_id": referrer_id}

        # Handle external_account.created event
        elif event['type'] == 'account.external_account.created':
            external_account = event['data']['object']
            account_id = external_account.get('account')

            print(f"[StripeConnect] Bank account connected for {account_id}")

            # This is informational - main status update comes from account.updated
            return {"status": "success", "message": "Bank account connected"}

        # Handle capability.updated event
        elif event['type'] == 'capability.updated':
            capability = event['data']['object']
            account_id = capability.get('account')

            print(f"[StripeConnect] Capability updated for {account_id}: {capability.get('status')}")

            # This triggers account.updated event, so no action needed here
            return {"status": "success", "message": "Capability updated"}

        else:
            print(f"[StripeConnect] Unhandled event type: {event['type']}")
            return {"status": "ignored", "event_type": event['type']}

    except Exception as e:
        print(f"[StripeConnect] Webhook processing failed: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Webhook processing failed: {str(e)}") from e
