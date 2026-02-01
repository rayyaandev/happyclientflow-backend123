"""
Payouts API

This API handles payout operations for the referral program:
- Trigger payouts when leads become customers
- Process Stripe Connect transfers for instant payouts
- Create pending records for PayPal/Bank (manual processing)
- Track payout status
- Send email notifications to referrers

Used by: Frontend Lead Management page
"""

from datetime import datetime, timezone
from typing import Optional
import os
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
import stripe
import databutton as db
from supabase import create_client, Client
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail, From
from app.env import Mode, mode

router = APIRouter(prefix="/payouts")

# Global variables for lazy initialization
_stripe_initialized = False
_supabase_client: Optional[Client] = None


def _init_stripe():
    """Lazy initialization of Stripe"""
    global _stripe_initialized
    if _stripe_initialized:
        return

    if mode == Mode.PROD:
        stripe.api_key = db.secrets.get("STRIPE_SECRET_KEY_LIVE")
    else:
        stripe.api_key = os.environ.get("STRIPE_SECRET_KEY_TEST") or db.secrets.get("STRIPE_SECRET_KEY_TEST")

    _stripe_initialized = True


def _get_supabase() -> Client:
    """Lazy initialization of Supabase client"""
    global _supabase_client
    if _supabase_client is None:
        supabase_url = db.secrets.get("SUPABASE_URL")
        supabase_service_key = db.secrets.get("SUPABASE_SERVICE_KEY")
        _supabase_client = create_client(supabase_url, supabase_service_key)
    return _supabase_client


def _send_payout_notification_email(
    referrer_email: str,
    referrer_name: str,
    amount: float,
    currency: str,
    payout_method: str,
    company_name: str,
    status: str,
) -> bool:
    """
    Send an email notification to the referrer about their payout.

    Args:
        referrer_email: Email address of the referrer
        referrer_name: Name of the referrer
        amount: Payout amount
        currency: Currency code (e.g., 'EUR')
        payout_method: Payment method used (stripe_connect, paypal, bank)
        company_name: Name of the company
        status: Payout status (completed, pending, processing)

    Returns:
        True if email sent successfully, False otherwise
    """
    try:
        sendgrid_api_key = db.secrets.get("SENDGRID_API_KEY")
        if not sendgrid_api_key:
            print("[Payouts] SendGrid API key not configured, skipping email")
            return False

        # Format payout method for display
        method_display = {
            'stripe_connect': 'Stripe (Direct Deposit)',
            'paypal': 'PayPal',
            'bank': 'Bank Transfer',
        }.get(payout_method, payout_method)

        # Format status message
        if status == 'completed':
            status_message = f"Your payout of <strong>{amount:.2f} {currency}</strong> has been successfully transferred to your {method_display} account."
            subject = f"🎉 Payout Received - {amount:.2f} {currency} from {company_name}"
        elif status == 'processing':
            status_message = f"Your payout of <strong>{amount:.2f} {currency}</strong> is being processed and will be transferred to your {method_display} account shortly."
            subject = f"💰 Payout Processing - {amount:.2f} {currency} from {company_name}"
        else:  # pending
            status_message = f"Your payout of <strong>{amount:.2f} {currency}</strong> has been queued and will be sent to your {method_display} account soon."
            subject = f"💰 Payout Pending - {amount:.2f} {currency} from {company_name}"

        # Get first name for personalization
        first_name = referrer_name.split()[0] if referrer_name else "there"

        body = f"""
        <div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto;">
            <h2 style="color: #2563eb;">Congratulations, {first_name}! 🎉</h2>

            <p>Great news! Your referral has converted into a customer.</p>

            <div style="background-color: #f0fdf4; border: 1px solid #86efac; border-radius: 8px; padding: 20px; margin: 20px 0;">
                <p style="margin: 0; font-size: 18px;">{status_message}</p>
            </div>

            <p><strong>Payout Details:</strong></p>
            <ul style="list-style: none; padding: 0;">
                <li>💵 Amount: <strong>{amount:.2f} {currency}</strong></li>
                <li>💳 Method: {method_display}</li>
                <li>📊 Status: {status.capitalize()}</li>
            </ul>

            <p>Thank you for being a valued referrer! Keep sharing and earning.</p>

            <p style="color: #6b7280; font-size: 14px; margin-top: 30px;">
                Best regards,<br>
                The {company_name} Team
            </p>
        </div>
        """

        message = Mail(
            from_email=From("noreply@happyclientflow.de", "Happy Client Flow"),
            to_emails=referrer_email,
            subject=subject,
            html_content=body,
        )

        sg = SendGridAPIClient(sendgrid_api_key)
        response = sg.send(message)

        print(f"[Payouts] Payout notification email sent to {referrer_email}, status: {response.status_code}")
        return response.status_code in [200, 201, 202]

    except Exception as e:
        print(f"[Payouts] Failed to send payout notification email: {str(e)}")
        return False


# Pydantic Models
class TriggerPayoutRequest(BaseModel):
    lead_id: str
    company_id: str


class TriggerPayoutResponse(BaseModel):
    success: bool
    payout_id: Optional[str] = None
    status: Optional[str] = None  # 'pending', 'processing', 'completed', 'failed'
    amount: Optional[float] = None
    currency: Optional[str] = None
    payout_method: Optional[str] = None
    error: Optional[str] = None


class PayoutStatusResponse(BaseModel):
    payout_id: str
    status: str
    amount: float
    currency: str
    payout_method: str
    created_at: str
    processed_at: Optional[str] = None
    completed_at: Optional[str] = None
    error_message: Optional[str] = None


@router.post("/trigger", response_model=TriggerPayoutResponse)
async def trigger_payout(request: TriggerPayoutRequest):
    """
    Trigger a payout for a referrer when a lead becomes a customer.

    Flow:
    1. Validate lead exists, is 'became_customer', and hasn't had payout triggered
    2. Verify lead has a referrer
    3. Fetch referrer payout method and company commission settings
    4. Create payout record
    5. If Stripe Connect: Create transfer immediately
    6. If PayPal/Bank: Leave as pending for manual processing
    7. Update lead with payout_triggered = true
    """
    _init_stripe()
    supabase = _get_supabase()

    try:
        print(f"[Payouts] Triggering payout for lead {request.lead_id}")

        # 1. Fetch lead and validate (use limit(1) instead of single() to avoid exceptions)
        lead_response = supabase.table('lead_management')\
            .select('id, status, payout_triggered, payout_id, referred_by')\
            .eq('id', request.lead_id)\
            .limit(1)\
            .execute()

        print(f"[Payouts] Lead query response: {lead_response.data}")

        if not lead_response.data or len(lead_response.data) == 0:
            return TriggerPayoutResponse(
                success=False,
                error="Lead not found"
            )

        lead = lead_response.data[0]

        # Validate lead status
        if lead.get('status') != 'became_customer':
            return TriggerPayoutResponse(
                success=False,
                error="Payout can only be triggered for leads with 'became_customer' status"
            )

        # Check if payout already triggered
        if lead.get('payout_triggered'):
            return TriggerPayoutResponse(
                success=False,
                error="Payout has already been triggered for this lead"
            )

        # 2. Verify lead has a referrer
        referred_by = lead.get('referred_by')
        print(f"[Payouts] Lead data: {lead}")
        print(f"[Payouts] referred_by value: {referred_by}")

        if not referred_by:
            return TriggerPayoutResponse(
                success=False,
                error="Cannot trigger payout for direct leads (no referrer)"
            )

        # 3. Get referrer info directly
        # referred_by now contains the referrer's customer_id (clients.id)
        # So we can look up the referrer record directly
        print(f"[Payouts] Looking up referrer with customer_id = {referred_by}")

        referrer_response = supabase.table('referrers')\
            .select('id, customer_id, payout_method, payout_details, stripe_connect_account_id, stripe_connect_payouts_enabled')\
            .eq('customer_id', referred_by)\
            .limit(1)\
            .execute()

        print(f"[Payouts] Referrer lookup response: {referrer_response.data}")

        if not referrer_response.data or len(referrer_response.data) == 0:
            # Debug: List all referrers to see what customer_ids exist
            all_referrers = supabase.table('referrers').select('id, customer_id').execute()
            print(f"[Payouts] All referrers: {all_referrers.data}")

            return TriggerPayoutResponse(
                success=False,
                error=f"Referrer record not found for customer_id: {referred_by}"
            )

        referrer = referrer_response.data[0]
        payout_method = referrer.get('payout_method')

        if not payout_method:
            return TriggerPayoutResponse(
                success=False,
                error="Referrer has no payout method configured"
            )

        # 4. Get company commission settings
        print(f"[Payouts] Looking up company: {request.company_id}")
        company_response = supabase.table('companies')\
            .select('id, name, commission_amount, commission_currency')\
            .eq('id', request.company_id)\
            .limit(1)\
            .execute()

        print(f"[Payouts] Company lookup response: {company_response.data}")

        if not company_response.data or len(company_response.data) == 0:
            return TriggerPayoutResponse(
                success=False,
                error="Company not found"
            )

        company = company_response.data[0]
        company_name = company.get('name', 'Your Partner Company')
        commission_amount = company.get('commission_amount')
        commission_currency = company.get('commission_currency') or 'EUR'

        # 4b. Get referrer's email and name from clients table
        referrer_client_response = supabase.table('clients')\
            .select('email, first_name, last_name')\
            .eq('id', referred_by)\
            .limit(1)\
            .execute()

        referrer_email = None
        referrer_name = "Referrer"
        if referrer_client_response.data and len(referrer_client_response.data) > 0:
            referrer_client = referrer_client_response.data[0]
            referrer_email = referrer_client.get('email')
            first_name = referrer_client.get('first_name', '')
            last_name = referrer_client.get('last_name', '')
            referrer_name = f"{first_name} {last_name}".strip() or "Referrer"
            print(f"[Payouts] Referrer email: {referrer_email}, name: {referrer_name}")

        print(f"[Payouts] Commission: {commission_amount} {commission_currency}")

        if not commission_amount or commission_amount <= 0:
            return TriggerPayoutResponse(
                success=False,
                error="Company has no commission amount configured"
            )

        # 5. Create payout record
        payout_data = {
            'referrer_id': referrer['id'],
            'lead_id': request.lead_id,
            'company_id': request.company_id,
            'amount': float(commission_amount),
            'currency': commission_currency,
            'payout_method': payout_method,
            'payout_details': referrer.get('payout_details'),
            'status': 'pending',
            'created_at': datetime.now(timezone.utc).isoformat(),
        }

        print(f"[Payouts] Inserting payout record: {payout_data}")

        payout_response = supabase.table('payouts')\
            .insert(payout_data)\
            .execute()

        print(f"[Payouts] Payout insert response: {payout_response.data}")

        if not payout_response.data:
            return TriggerPayoutResponse(
                success=False,
                error="Failed to create payout record"
            )

        payout = payout_response.data[0]
        payout_id = payout['id']
        final_status = 'pending'

        print(f"[Payouts] Created payout record {payout_id} with method {payout_method}")

        # 6. Process based on payout method
        if payout_method == 'stripe_connect':
            stripe_account_id = referrer.get('stripe_connect_account_id')
            payouts_enabled = referrer.get('stripe_connect_payouts_enabled')

            if not stripe_account_id:
                # Update payout as failed
                supabase.table('payouts').update({
                    'status': 'failed',
                    'error_message': 'Referrer has no Stripe Connect account',
                }).eq('id', payout_id).execute()

                return TriggerPayoutResponse(
                    success=False,
                    error="Referrer has no Stripe Connect account configured"
                )

            if not payouts_enabled:
                # Update payout as failed
                supabase.table('payouts').update({
                    'status': 'failed',
                    'error_message': 'Stripe Connect payouts not enabled for referrer',
                }).eq('id', payout_id).execute()

                return TriggerPayoutResponse(
                    success=False,
                    error="Stripe Connect payouts not enabled for this referrer"
                )

            # Create Stripe Transfer
            try:
                amount_cents = int(commission_amount * 100)

                transfer = stripe.Transfer.create(
                    amount=amount_cents,
                    currency=commission_currency.lower(),
                    destination=stripe_account_id,
                    transfer_group=f"payout_{payout_id}",
                    metadata={
                        'payout_id': payout_id,
                        'lead_id': request.lead_id,
                        'referrer_id': referrer['id'],
                    }
                )

                print(f"[Payouts] Created Stripe Transfer {transfer.id}")

                # Update payout with transfer info
                # Stripe transfers are usually instant to connected accounts
                final_status = 'completed' if transfer.get('reversed') is False else 'processing'

                supabase.table('payouts').update({
                    'status': final_status,
                    'stripe_transfer_id': transfer.id,
                    'processed_at': datetime.now(timezone.utc).isoformat(),
                    'completed_at': datetime.now(timezone.utc).isoformat() if final_status == 'completed' else None,
                }).eq('id', payout_id).execute()

            except stripe.StripeError as e:
                print(f"[Payouts] Stripe error: {str(e)}")
                supabase.table('payouts').update({
                    'status': 'failed',
                    'error_message': str(e),
                    'error_code': getattr(e, 'code', None),
                }).eq('id', payout_id).execute()

                return TriggerPayoutResponse(
                    success=False,
                    payout_id=payout_id,
                    status='failed',
                    error=f"Stripe transfer failed: {str(e)}"
                )

        # For PayPal and Bank, leave status as 'pending' for manual processing
        # No automatic processing implemented

        # 7. Update lead with payout info
        supabase.table('lead_management').update({
            'payout_triggered': True,
            'payout_id': payout_id,
        }).eq('id', request.lead_id).execute()

        # 8. Update referrer's total commission earned
        supabase.table('referrers').update({
            'total_comission_earned': referrer.get('total_comission_earned', 0) + commission_amount,
        }).eq('id', referrer['id']).execute()

        # 9. Send payout notification email to referrer
        if referrer_email:
            _send_payout_notification_email(
                referrer_email=referrer_email,
                referrer_name=referrer_name,
                amount=commission_amount,
                currency=commission_currency,
                payout_method=payout_method,
                company_name=company_name,
                status=final_status,
            )
        else:
            print(f"[Payouts] No email found for referrer, skipping notification")

        print(f"[Payouts] Payout triggered successfully: {payout_id} with status {final_status}")

        return TriggerPayoutResponse(
            success=True,
            payout_id=payout_id,
            status=final_status,
            amount=commission_amount,
            currency=commission_currency,
            payout_method=payout_method,
        )

    except Exception as e:
        print(f"[Payouts] Error: {str(e)}")
        return TriggerPayoutResponse(
            success=False,
            error=f"Internal error: {str(e)}"
        )


@router.get("/status/{payout_id}", response_model=PayoutStatusResponse)
async def get_payout_status(payout_id: str):
    """Get the status of a specific payout"""
    supabase = _get_supabase()

    try:
        payout_response = supabase.table('payouts')\
            .select('*')\
            .eq('id', payout_id)\
            .single()\
            .execute()

        if not payout_response.data:
            raise HTTPException(status_code=404, detail="Payout not found")

        payout = payout_response.data

        return PayoutStatusResponse(
            payout_id=payout['id'],
            status=payout['status'],
            amount=float(payout['amount']),
            currency=payout['currency'],
            payout_method=payout['payout_method'],
            created_at=payout['created_at'],
            processed_at=payout.get('processed_at'),
            completed_at=payout.get('completed_at'),
            error_message=payout.get('error_message'),
        )

    except HTTPException:
        raise
    except Exception as e:
        print(f"[Payouts] Error fetching payout status: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Internal error: {str(e)}")
