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
import base64
import os
import urllib.parse
from fastapi import APIRouter, HTTPException, Request, Header
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
import stripe
import databutton as db
from supabase import create_client, Client
from app.env import Mode, mode

router = APIRouter(prefix="/stripe-connect")

# Global variables for lazy initialization
_stripe_initialized = False
_supabase_client: Optional[Client] = None
_stripe_connect_webhook_secret: Optional[str] = None

def _init_stripe():
    """Lazy initialization of Stripe and secrets"""
    global _stripe_initialized, _stripe_connect_webhook_secret
    if _stripe_initialized:
        return

    # Environment-based Stripe configuration
    if mode == Mode.PROD:
        # Production: Use live Stripe keys
        stripe.api_key = db.secrets.get("STRIPE_SECRET_KEY_LIVE")
        _stripe_connect_webhook_secret = db.secrets.get("STRIPE_CONNECT_WEBHOOK_SECRET_LIVE")
    else:
        # Development: Use test/sandbox Stripe keys (env var first, then databutton)
        stripe.api_key = os.environ.get("STRIPE_SECRET_KEY_TEST") or db.secrets.get("STRIPE_SECRET_KEY_TEST")
        _stripe_connect_webhook_secret = os.environ.get("STRIPE_CONNECT_WEBHOOK_SECRET_TEST") or "whsec_lARJBdIPYYfhjHJ2xzzQJqrt2tfiw7sW"

    _stripe_initialized = True

def _get_supabase() -> Client:
    """Lazy initialization of Supabase client"""
    global _supabase_client
    if _supabase_client is None:
        supabase_url = db.secrets.get("SUPABASE_URL")
        supabase_service_key = db.secrets.get("SUPABASE_SERVICE_KEY")
        _supabase_client = create_client(supabase_url, supabase_service_key)
    return _supabase_client

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


# --- Owner Stripe Connect Models (Standard OAuth) ---

class CreateOwnerOAuthLinkRequest(BaseModel):
    company_id: str
    return_url: str  # Frontend path to redirect to after OAuth (e.g. /settings or /onboarding)

class CreateOwnerOAuthLinkResponse(BaseModel):
    oauth_url: str

class OwnerConnectStatusResponse(BaseModel):
    connected: bool = False
    stripe_account_id: Optional[str] = None
    stripe_connect_enabled: bool = False
    stripe_connect_onboarded_at: Optional[str] = None

class DisconnectOwnerRequest(BaseModel):
    company_id: str

class CreateConnectedCustomerRequest(BaseModel):
    company_id: str
    email: str
    name: str
    phone: Optional[str] = None

class CreateConnectedCustomerResponse(BaseModel):
    customer_id: Optional[str] = None
    success: bool = False
    error: Optional[str] = None


def _get_stripe_connect_client_id() -> str:
    """Get the Stripe Connect Client ID based on environment"""
    if mode == Mode.PROD:
        return db.secrets.get("STRIPE_CONNECT_CLIENT_ID_LIVE")
    else:
        # Try environment variable first (for local dev), then fall back to databutton secrets
        return os.environ.get("STRIPE_CONNECT_CLIENT_ID_TEST") or db.secrets.get("STRIPE_CONNECT_CLIENT_ID_TEST")


def _get_backend_base_url() -> str:
    """Get the backend base URL for OAuth redirect URI"""
    if mode == Mode.PROD:
        return "https://happyclientflow-backend123.onrender.com"
    else:
        return "http://localhost:8000"


def _get_frontend_base_url() -> str:
    """Get the frontend base URL for redirects after OAuth"""
    if mode == Mode.PROD:
        return "https://app.happyclientflow.de"
    else:
        return "http://localhost:5173"


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
    _init_stripe()
    supabase = _get_supabase()

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
            except stripe._error.InvalidRequestError:
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
                    "card_payments": {"requested": True},
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

    except stripe._error.StripeError as e:
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
    _init_stripe()
    supabase = _get_supabase()

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
        except stripe._error.InvalidRequestError:
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

    except stripe._error.StripeError as e:
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
    _init_stripe()
    supabase = _get_supabase()

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
                'stripe_connect_details_submitted': details_submitted,
                'stripe_connect_charges_enabled': charges_enabled,
                'stripe_connect_payouts_enabled': payouts_enabled,
                'stripe_connect_onboarded_at': datetime.now(timezone.utc).isoformat() if (details_submitted and payouts_enabled) else None
            }).eq('id', referrer_id).execute()

            return AccountStatusResponse(
                account_id=account_id,
                status="active" if details_submitted else "pending",
                details_submitted=details_submitted,
                charges_enabled=charges_enabled,
                payouts_enabled=payouts_enabled,
                onboarded=(details_submitted and payouts_enabled)
            )

        except stripe._error.InvalidRequestError:
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
    _init_stripe()
    supabase = _get_supabase()

    if not stripe.api_key:
        raise HTTPException(status_code=500, detail="Stripe not configured")

    body = await request.body()

    try:
        event = stripe.Webhook.construct_event(
            body, stripe_signature, _stripe_connect_webhook_secret
        )
    except ValueError:
        print("[StripeConnect] Webhook - Invalid payload")
        raise HTTPException(status_code=400, detail="Invalid payload")
    except stripe._error.SignatureVerificationError:
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


# =============================================================================
# Owner Stripe Connect Endpoints (Standard OAuth for business owners)
# =============================================================================

@router.post("/owner/create-oauth-link", response_model=CreateOwnerOAuthLinkResponse)
async def create_owner_oauth_link(request: CreateOwnerOAuthLinkRequest):
    """
    Generate Stripe Connect OAuth authorization URL for a business owner.

    The business owner will be redirected to Stripe to authorize their account.
    After authorization, Stripe redirects to our callback endpoint which
    exchanges the code for a connected account ID.
    """
    _init_stripe()
    supabase = _get_supabase()

    if not stripe.api_key:
        raise HTTPException(status_code=500, detail="Stripe not configured")

    try:
        # Verify company exists
        company_response = supabase.table('companies') \
            .select('id, stripe_connect_account_id') \
            .eq('id', request.company_id) \
            .single() \
            .execute()

        if not company_response.data:
            raise HTTPException(status_code=404, detail="Company not found")

        # Check if already connected
        if company_response.data.get('stripe_connect_account_id'):
            raise HTTPException(status_code=400, detail="Company already has a connected Stripe account")

        # Build OAuth URL
        client_id = _get_stripe_connect_client_id()
        backend_base = _get_backend_base_url()
        redirect_uri = f"{backend_base}/routes/stripe-connect/owner/oauth/callback"

        # Encode company_id and return_url in state parameter
        state_data = f"{request.company_id}:{base64.urlsafe_b64encode(request.return_url.encode()).decode()}"

        oauth_url = (
            f"https://connect.stripe.com/oauth/authorize"
            f"?response_type=code"
            f"&client_id={client_id}"
            f"&scope=read_write"
            f"&redirect_uri={urllib.parse.quote(redirect_uri, safe='')}"
            f"&state={urllib.parse.quote(state_data, safe='')}"
        )

        print(f"[StripeConnect] Generated owner OAuth link for company {request.company_id}")

        return CreateOwnerOAuthLinkResponse(oauth_url=oauth_url)

    except HTTPException:
        raise
    except Exception as e:
        print(f"[StripeConnect] Error creating owner OAuth link: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Internal error: {str(e)}") from e


@router.get("/owner/oauth/callback")
async def owner_oauth_callback(request: Request):
    """
    Handle Stripe Connect OAuth callback for business owners.

    Stripe redirects here after the owner authorizes (or cancels).
    We exchange the authorization code for a connected account ID,
    store it in the companies table, and redirect to the frontend.
    """
    _init_stripe()
    supabase = _get_supabase()

    params = dict(request.query_params)
    code = params.get('code')
    state = params.get('state')
    error = params.get('error')
    error_description = params.get('error_description')

    frontend_base = _get_frontend_base_url()

    print(f"[StripeConnect] Owner OAuth callback received - code: {bool(code)}, state: {state}, error: {error}")

    # Decode state to get company_id and return path
    company_id = None
    return_path = "/settings"
    if state:
        try:
            parts = state.split(':', 1)
            company_id = parts[0]
            if len(parts) > 1:
                return_path = base64.urlsafe_b64decode(parts[1].encode()).decode()
        except Exception as decode_err:
            print(f"[StripeConnect] Error decoding state: {decode_err}")

    if error:
        print(f"[StripeConnect] Owner OAuth error: {error} - {error_description}")
        error_msg = urllib.parse.quote(error_description or error)
        return RedirectResponse(
            url=f"{frontend_base}{return_path}?stripe_connect_error={error_msg}"
        )

    if not code or not company_id:
        return RedirectResponse(
            url=f"{frontend_base}{return_path}?stripe_connect_error=missing_params"
        )

    try:
        # Exchange authorization code for connected account
        token_response = stripe.OAuth.token(grant_type='authorization_code', code=code)
        connected_account_id = token_response.get('stripe_user_id')

        if not connected_account_id:
            print("[StripeConnect] No stripe_user_id in token response")
            return RedirectResponse(
                url=f"{frontend_base}{return_path}?stripe_connect_error=no_account_id"
            )

        print(f"[StripeConnect] Owner OAuth success - account: {connected_account_id}, company: {company_id}")

        # Store in database
        supabase.table('companies').update({
            'stripe_connect_account_id': connected_account_id,
            'stripe_connect_enabled': True,
            'stripe_connect_onboarded_at': datetime.now(timezone.utc).isoformat(),
        }).eq('id', company_id).execute()

        return RedirectResponse(
            url=f"{frontend_base}{return_path}?stripe_connected=true"
        )

    except stripe._error.StripeError as e:
        print(f"[StripeConnect] Owner OAuth token exchange error: {str(e)}")
        error_msg = urllib.parse.quote(str(e))
        return RedirectResponse(
            url=f"{frontend_base}{return_path}?stripe_connect_error={error_msg}"
        )
    except Exception as e:
        print(f"[StripeConnect] Owner OAuth internal error: {str(e)}")
        error_msg = urllib.parse.quote(str(e))
        return RedirectResponse(
            url=f"{frontend_base}{return_path}?stripe_connect_error={error_msg}"
        )


@router.get("/owner/status/{company_id}", response_model=OwnerConnectStatusResponse)
async def get_owner_connect_status(company_id: str):
    """
    Get the Stripe Connect status for a business owner's company.
    """
    _init_stripe()
    supabase = _get_supabase()

    try:
        company_response = supabase.table('companies') \
            .select('stripe_connect_account_id, stripe_connect_enabled, stripe_connect_onboarded_at') \
            .eq('id', company_id) \
            .single() \
            .execute()

        if not company_response.data:
            raise HTTPException(status_code=404, detail="Company not found")

        data = company_response.data
        account_id = data.get('stripe_connect_account_id')

        # Optionally verify the account still exists in Stripe
        if account_id:
            try:
                stripe.Account.retrieve(account_id)
            except stripe._error.InvalidRequestError:
                # Account no longer exists in Stripe, clean up
                print(f"[StripeConnect] Owner account {account_id} no longer exists in Stripe")
                supabase.table('companies').update({
                    'stripe_connect_account_id': None,
                    'stripe_connect_enabled': False,
                    'stripe_connect_onboarded_at': None,
                }).eq('id', company_id).execute()
                return OwnerConnectStatusResponse(connected=False)

        return OwnerConnectStatusResponse(
            connected=bool(account_id),
            stripe_account_id=account_id,
            stripe_connect_enabled=data.get('stripe_connect_enabled', False),
            stripe_connect_onboarded_at=data.get('stripe_connect_onboarded_at'),
        )

    except HTTPException:
        raise
    except Exception as e:
        print(f"[StripeConnect] Error getting owner connect status: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Internal error: {str(e)}") from e


@router.post("/owner/disconnect")
async def disconnect_owner_stripe(request: DisconnectOwnerRequest):
    """
    Disconnect a business owner's Stripe account.

    Deauthorizes the account on Stripe's side and clears the connection
    from the database. Also disables the referral program since it
    requires a connected Stripe account.
    """
    _init_stripe()
    supabase = _get_supabase()

    try:
        company_response = supabase.table('companies') \
            .select('stripe_connect_account_id') \
            .eq('id', request.company_id) \
            .single() \
            .execute()

        if not company_response.data:
            raise HTTPException(status_code=404, detail="Company not found")

        account_id = company_response.data.get('stripe_connect_account_id')
        if not account_id:
            raise HTTPException(status_code=400, detail="No Stripe account connected")

        # Deauthorize on Stripe side
        try:
            client_id = _get_stripe_connect_client_id()
            stripe.OAuth.deauthorize(client_id=client_id, stripe_user_id=account_id)
            print(f"[StripeConnect] Deauthorized owner account {account_id}")
        except stripe._error.StripeError as e:
            print(f"[StripeConnect] Error deauthorizing owner account: {str(e)}")
            # Continue cleanup even if deauth fails

        # Clear from database and disable referral program
        supabase.table('companies').update({
            'stripe_connect_account_id': None,
            'stripe_connect_enabled': False,
            'stripe_connect_onboarded_at': None,
            'referral_program_enabled': False,
        }).eq('id', request.company_id).execute()

        print(f"[StripeConnect] Disconnected and cleaned up company {request.company_id}")

        return {"status": "disconnected"}

    except HTTPException:
        raise
    except Exception as e:
        print(f"[StripeConnect] Error disconnecting owner: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Internal error: {str(e)}") from e


@router.post("/owner/create-customer", response_model=CreateConnectedCustomerResponse)
async def create_connected_customer(request: CreateConnectedCustomerRequest):
    """
    Create a Stripe Customer under the business owner's connected account.

    Called when a lead submits the referral landing page form.
    The customer is created on the connected account so the business
    owner can later bill them directly.
    """
    _init_stripe()
    supabase = _get_supabase()

    try:
        # Get connected account
        company_response = supabase.table('companies') \
            .select('stripe_connect_account_id, stripe_connect_enabled') \
            .eq('id', request.company_id) \
            .single() \
            .execute()

        if not company_response.data:
            return CreateConnectedCustomerResponse(success=False, error="Company not found")

        account_id = company_response.data.get('stripe_connect_account_id')
        if not account_id:
            return CreateConnectedCustomerResponse(success=False, error="No connected Stripe account")

        # Create customer on the connected account
        customer = stripe.Customer.create(
            email=request.email,
            name=request.name,
            phone=request.phone,
            stripe_account=account_id,
        )

        print(f"[StripeConnect] Created customer {customer.id} on connected account {account_id}")

        return CreateConnectedCustomerResponse(customer_id=customer.id, success=True)

    except stripe._error.StripeError as e:
        print(f"[StripeConnect] Error creating connected customer: {str(e)}")
        return CreateConnectedCustomerResponse(success=False, error=str(e))
    except Exception as e:
        print(f"[StripeConnect] Internal error creating customer: {str(e)}")
        return CreateConnectedCustomerResponse(success=False, error=str(e))
