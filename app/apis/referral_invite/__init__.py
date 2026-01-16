"""
This API module handles sending referral program invitations.
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, EmailStr
import databutton as db
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail, From
from supabase import create_client, Client
from typing import Optional

router = APIRouter(prefix="/v1/referral-invites", tags=["referral_invites"])

# --- Supabase Integration ---
def get_supabase_client() -> Client:
    supabase_url = db.secrets.get("SUPABASE_URL")
    supabase_key = db.secrets.get("SUPABASE_SERVICE_KEY")
    if not supabase_url or not supabase_key:
        raise HTTPException(status_code=500, detail="Supabase connection details not configured.")
    return create_client(supabase_url, supabase_key)


class SendReferralInvitePayload(BaseModel):
    client_id: str
    client_email: EmailStr
    client_name: str
    company_id: str
    company_name: str


@router.post("/send")
async def send_referral_invite(
    payload: SendReferralInvitePayload
):
    """
    Sends a referral program invitation email to a customer.
    This is triggered when:
    - Customer gives positive feedback (recommends the company)
    - Company has referral_program_enabled = true
    - Company has automatic_invite = true
    """
    print(f"[REFERRAL] Sending referral invite to: {payload.client_email}")
    
    supabase = get_supabase_client()
    
    # 1. Verify company has referral program enabled
    try:
        company_res = supabase.table("companies").select(
            "referral_program_enabled, automatic_invite, commission_amount, commission_currency"
        ).eq("id", payload.company_id).single().execute()
        
        if not company_res.data:
            raise HTTPException(status_code=404, detail="Company not found.")
        
        company_data = company_res.data
        if not company_data.get("referral_program_enabled"):
            raise HTTPException(status_code=400, detail="Referral program is not enabled for this company.")
        
        if not company_data.get("automatic_invite"):
            raise HTTPException(status_code=400, detail="Automatic invites are not enabled for this company.")
            
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error fetching company: {e}")
        raise HTTPException(status_code=500, detail="Failed to verify company settings.")

    # 2. Check if customer is already a referrer
    try:
        existing_referrer = supabase.table("referrers").select("id").eq(
            "customer_id", payload.client_id
        ).execute()
        
        if existing_referrer.data and len(existing_referrer.data) > 0:
            print(f"Customer {payload.client_id} is already a referrer.")
            return {"success": True, "message": "Customer is already enrolled in referral program."}
    except Exception as e:
        print(f"Error checking existing referrer: {e}")
        # Continue anyway - we'll just send the invite

    # 3. Fetch the referral invite template
    # Look for a template with template_type = 'REFERRAL' or a specific name
    try:
        template_res = supabase.table("message_templates").select(
            "id, subject, body"
        ).eq("company_id", payload.company_id).eq(
            "template_type", "Referral"
        ).limit(1).execute()
        
        if not template_res.data or len(template_res.data) == 0:
            # Fallback: Use a default referral invite template
            subject = f"Earn money by referring friends to {payload.company_name}!"
            body = f"""Hi {payload.client_name},

Thank you for recommending us! We're excited to invite you to our referral program.

For every successful referral, you'll earn {company_data.get('commission_amount', 'a commission')} {company_data.get('commission_currency', 'EUR')}!

👉 Sign up here to become a referrer: {{{{referral_link}}}}

It's quick and easy - just provide your payout details and start earning!

Best regards,
The {payload.company_name} Team"""
        else:
            template_data = template_res.data[0]
            subject = template_data['subject'] or f"Join our referral program - {payload.company_name}"
            body = template_data['body']
            
    except Exception as e:
        print(f"Error fetching template: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch referral invite template.")

    # 4. Generate the referral signup link
    # In production, use your actual domain
    base_url = "http://localhost:5173"  # Change this to your production URL
    referral_link = f"{base_url}/referral-signup?customer={payload.client_id}"

    # 5. Interpolate variables into the email body and subject
    variables = {
        "{{first_name}}": payload.client_name.split()[0] if payload.client_name else "",
        "{{client_name}}": payload.client_name,
        "{{company_name}}": payload.company_name,
        "{{referral_link}}": referral_link,
        "{{commission_amount}}": str(company_data.get('commission_amount', '')),
        "{{commission_currency}}": company_data.get('commission_currency', 'EUR'),
    }

    for key, value in variables.items():
        replace_with = value if value is not None else ""
        if subject:
            subject = subject.replace(key, replace_with)
        if body:
            body = body.replace(key, replace_with)

    # Replace newlines with HTML line breaks for email rendering
    if body:
        body = body.replace('\n', '<br>')

    # 6. Send email via SendGrid
    sendgrid_api_key = db.secrets.get("SENDGRID_API_KEY")
    sendgrid_from_email = "noreply@happyclientflow.de"

    if not sendgrid_api_key:
        raise HTTPException(status_code=500, detail="SendGrid configuration is missing.")

    message = Mail(
        from_email=From(sendgrid_from_email, "Happy Client Flow"),
        to_emails=payload.client_email,
        subject=subject,
        html_content=body
    )

    try:
        sg = SendGridAPIClient(sendgrid_api_key)
        response = sg.send(message)
        if response.status_code >= 300:
            print(f"SendGrid error response: {response.body}")
            raise HTTPException(
                status_code=502, 
                detail=f"Failed to send email via SendGrid. Status: {response.status_code}"
            )
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error sending email via SendGrid: {e}")
        raise HTTPException(status_code=502, detail="An unexpected error occurred while sending the email.")

    return {"success": True, "message": "Referral invite email sent successfully."}




class SendReferralCodePayload(BaseModel):
    customer_id: str
    customer_email: EmailStr
    customer_name: str
    company_id: str
    company_name: str
    referral_code: str
    referral_link: str


@router.post("/send-code")
async def send_referral_code_email(payload: SendReferralCodePayload):
    """
    Sends the referral code and link to a customer after they've signed up
    for the referral program. This is a public endpoint (no auth required)
    since it's called from the referral signup page.
    """
    print(f"[REFERRAL] Sending referral code to: {payload.customer_email}")
    
    supabase = get_supabase_client()
    
    # Fetch the referral code template (you'll need to create this template)
    try:
        template_res = supabase.table("message_templates").select("subject, body").eq("name", "referral_code").single().execute()
        if template_res.data:
            subject = template_res.data['subject']
            body = template_res.data['body']
        else:
            # Fallback template
            subject = f"Your {payload.company_name} Referral Link"
            body = f"""
            <p>Hi {payload.customer_name},</p>
            
            <p>Welcome to the {payload.company_name} Referral Program! 🎉</p>
            
            <p>Here's your unique referral link:</p>
            
            <div style="background-color: #f3f4f6; padding: 16px; border-radius: 8px; margin: 16px 0;">
                <p style="margin: 0; font-family: monospace; font-size: 14px; word-break: break-all;">
                    <a href="{payload.referral_link}">{payload.referral_link}</a>
                </p>
            </div>
            
            <p><strong>Your Referral Code:</strong> {payload.referral_code}</p>
            
            <p>Share this link with friends and family. When they become customers, 
            you'll earn a commission!</p>
            
            <p>Best regards,<br>{payload.company_name}</p>
            """
    except Exception as e:
        print(f"Error fetching template, using fallback: {e}")
        subject = f"Your {payload.company_name} Referral Link"
        body = f"""
        <p>Hi {payload.customer_name},</p>
        
        <p>Welcome to the {payload.company_name} Referral Program! 🎉</p>
        
        <p>Here's your unique referral link:</p>
        
        <div style="background-color: #f3f4f6; padding: 16px; border-radius: 8px; margin: 16px 0;">
            <p style="margin: 0; font-family: monospace; font-size: 14px; word-break: break-all;">
                <a href="{payload.referral_link}">{payload.referral_link}</a>
            </p>
        </div>
        
        <p><strong>Your Referral Code:</strong> {payload.referral_code}</p>
        
        <p>Share this link with friends and family. When they become customers, 
        you'll earn a commission!</p>
        
        <p>Best regards,<br>{payload.company_name}</p>
        """
    
    # Replace variables in template
    variables = {
        "{{customer_name}}": payload.customer_name,
        "{{company_name}}": payload.company_name,
        "{{referral_code}}": payload.referral_code,
        "{{referral_link}}": payload.referral_link,
    }
    
    for key, value in variables.items():
        if subject:
            subject = subject.replace(key, value or "")
        if body:
            body = body.replace(key, value or "")
    
    # Send email via SendGrid
    sendgrid_api_key = db.secrets.get("SENDGRID_API_KEY")
    sendgrid_from_email = "noreply@happyclientflow.de"
    
    if not sendgrid_api_key:
        raise HTTPException(status_code=500, detail="SendGrid configuration is missing.")
    
    message = Mail(
        from_email=From(sendgrid_from_email, "Happy Client Flow"),
        to_emails=payload.customer_email,
        subject=subject,
        html_content=body
    )
    
    try:
        sg = SendGridAPIClient(sendgrid_api_key)
        response = sg.send(message)
        if response.status_code >= 300:
            print(f"SendGrid error response: {response.body}")
            raise HTTPException(status_code=502, detail=f"Failed to send email. Status: {response.status_code}")
    except Exception as e:
        print(f"Error sending email via SendGrid: {e}")
        raise HTTPException(status_code=502, detail="An unexpected error occurred while sending the email.")
    
    return {"success": True, "message": "Referral code email sent successfully."}


    