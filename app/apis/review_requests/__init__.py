"""
This API module handles sending review requests and creating reminders.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr
import databutton as db
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail, From
from supabase import create_client, Client
import requests
from twilio.rest import Client as TwilioClient
import json

from app.libs.auth import require_auth  # Updated to use require_auth
from app.env import mode, Mode

router = APIRouter(prefix="/v1/review-requests", tags=["review_requests"])

# --- Supabase Integration ---
def get_supabase_client() -> Client:
    supabase_url = db.secrets.get("SUPABASE_URL")
    supabase_key = db.secrets.get("SUPABASE_SERVICE_KEY")
    if not supabase_url or not supabase_key:
        raise HTTPException(status_code=500, detail="Supabase connection details not configured.")
    return create_client(supabase_url, supabase_key)

from typing import Optional

class SendReviewRequestPayload(BaseModel):
    client_id: str
    client_email: Optional[EmailStr] = None
    client_phone: Optional[str] = None
    channel: str
    template_id: str
    company_name: str
    first_name: str
    review_link: str
    title: Optional[str] = None
    last_name: Optional[str] = None
    product_name: Optional[str] = None

@router.post("/send")
async def send_review_request(
    payload: SendReviewRequestPayload,
    current_user: str = Depends(require_auth)
):
    """
    Sends a review request email to a client using a specified template.
    This endpoint fetches a template, interpolates variables, and sends the email.
    
    Args:
        payload: Review request data including client details and template info
        current_user: The authenticated user's ID from JWT token
    """
    print(f"[AUTH] Sending review request for user: {current_user}")
    
    supabase = get_supabase_client()
    
    # 1. Fetch user language from their profile
    user_language = 'de' # Default to German
    try:
        user_res = supabase.table("users").select("language").eq("id", current_user).single().execute()
        if user_res.data and user_res.data.get("language"):
            user_language = user_res.data["language"]
            print("setting user language as", user_res.data["language"])
        else:
            print(f"Warning: Could not find language for user {current_user}, defaulting to 'de'.")
    except Exception as e:
        print(f"Error fetching user language, defaulting to 'de': {e}")

    
    # 2. Translate title based on user's language
    translated_title = payload.title
    if user_language == 'de':
        if payload.title == 'Mr.':
            translated_title = 'Herr'
        elif payload.title == 'Mrs.':
            translated_title = 'Frau'
        # Note: If title is something else, it remains unchanged.
    
    # 3. Fetch the message template from Supabase
    try:
        template_res = supabase.table("message_templates").select("subject, body").eq("id", payload.template_id).single().execute()
        if not template_res.data:
            raise HTTPException(status_code=404, detail=f"Message template with ID {payload.template_id} not found.")
        template_data = template_res.data
    except Exception as e:
        print(f"Error fetching template: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch message template from database.")

    # 4. Interpolate variables into the email body and subject
    subject = template_data['subject']
    body = template_data['body']
    print(f"Body from Supabase: {body}")

    variables = {
        "{{title}}": translated_title, # Use the translated title
        "{{first_name}}": payload.first_name,
        "{{last_name}}": payload.last_name,
        "{{company_name}}": payload.company_name,
        "{{product_name}}": payload.product_name,
        "{{review_link}}": payload.review_link,
    }

    for key, value in variables.items():
        # Ensure value is a string and not None before replacing
        replace_with = value if value is not None else ""
        if subject:
            subject = subject.replace(key, replace_with)
        if body:
            body = body.replace(key, replace_with)

    # Replace newlines with HTML line breaks for email rendering
    if body:
        body = body.replace('\n', '<br>')
    else:
        # For SMS/WhatsApp, newlines should be preserved as is.
        pass
    
    print(f"Final body to SendGrid: {body}")

    # 5. Send message via the specified channel
    if payload.channel == "Email":
        # Send email via SendGrid
        sendgrid_api_key = db.secrets.get("SENDGRID_API_KEY")
        sendgrid_from_email = "noreply@happyclientflow.de"

        if not sendgrid_api_key:
            raise HTTPException(status_code=500, detail="SendGrid configuration is missing.")

        if not payload.client_email:
            raise HTTPException(status_code=400, detail="Client email is required for email channel.")

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
                raise HTTPException(status_code=502, detail=f"Failed to send email via SendGrid. Status: {response.status_code}")
        except Exception as e:
            print(f"Error sending email via SendGrid: {e}")
            raise HTTPException(status_code=502, detail="An unexpected error occurred while sending the email.")

        return {"message": "Review request email sent successfully."}

    elif payload.channel == "SMS":
        # Send SMS via Twilio
        twilio_account_sid = db.secrets.get("TWILIO_ACCOUNT_SID")
        twilio_auth_token = db.secrets.get("TWILIO_AUTH_TOKEN")
        twilio_from_number = db.secrets.get("TWILIO_FROM_NUMBER")

        if not all([twilio_account_sid, twilio_auth_token, twilio_from_number]):
            raise HTTPException(status_code=500, detail="Twilio SMS configuration is missing.")

        if not payload.client_phone:
            raise HTTPException(status_code=400, detail="Client phone number is required for SMS channel.")

        # The body for SMS should be plain text.
        sms_body = body.replace('<br>', '\n') if body else ''

        try:
            client = TwilioClient(twilio_account_sid, twilio_auth_token)
            message = client.messages.create(
                to=payload.client_phone,
                from_=twilio_from_number,
                body=sms_body,
                status_callback="https://api.databutton.com/_projects/722024f4-d06c-4ad3-9271-27bac1ebab31/dbtn/devx/app/routes/twilio_callbacks/status",
            )
            print(f"SMS sent successfully, SID: {message.sid}")
        except Exception as e:
            print(f"Error sending SMS via Twilio: {e}")
            raise HTTPException(status_code=502, detail="An unexpected error occurred while sending the SMS.")

        return {"message": "Review request SMS sent successfully."}
    
    elif payload.channel == "WhatsApp":
        twilio_account_sid = db.secrets.get("TWILIO_ACCOUNT_SID")
        twilio_auth_token = db.secrets.get("TWILIO_AUTH_TOKEN")
        twilio_from_number = db.secrets.get("TWILIO_FROM_NUMBER") # WhatsApp-enabled number

        if not all([twilio_account_sid, twilio_auth_token, twilio_from_number]):
            raise HTTPException(status_code=500, detail="Twilio WhatsApp configuration is missing.")

        if not payload.client_phone:
            raise HTTPException(status_code=400, detail="Client phone number is required for WhatsApp channel.")

        # Determine which template to use based on the template_id or another indicator
        # This assumes the template name or a field indicates formality.
        # We will fetch the rule_type from the message_template
        try:
            template_res = supabase.table("message_templates").select("rule_type").eq("id", payload.template_id).single().execute()
            if not template_res.data:
                raise HTTPException(status_code=404, detail=f"Message template with ID {payload.template_id} not found for WhatsApp rule check.")
            rule_type = template_res.data.get('rule_type')
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to determine formality from template: {e}")

        if rule_type == "formal":
            content_sid = "HX8991dce65c8ab28adea93518f63f3058" # whatsapp_outreach_formal_v2
            content_variables = {
                '1': payload.client_id,
                '2': translated_title,
                '3': payload.last_name,
                '4': payload.company_name
            }
        elif rule_type == "informal":
            content_sid = "HXe61bb8737035c5077500e6263d367afe" # whatsapp_outreach_informal_v2
            content_variables = {
                '1': payload.client_id,
                '2': payload.first_name,
                '3': payload.company_name
            }
        else:
            raise HTTPException(status_code=400, detail=f"Unsupported rule_type '{rule_type}' for WhatsApp message.")

        try:
            client = TwilioClient(twilio_account_sid, twilio_auth_token)
            message = client.messages.create(
                content_sid=content_sid,
                from_=f"whatsapp:{twilio_from_number}",
                to=f"whatsapp:{payload.client_phone}",
                content_variables=json.dumps(content_variables),
                status_callback="https://api.databutton.com/_projects/722024f4-d06c-4ad3-9271-27bac1ebab31/dbtn/devx/app/routes/twilio_callbacks/status",
            )
            print(f"WhatsApp message sent successfully using template {content_sid}, SID: {message.sid}")
        except Exception as e:
            print(f"Error sending WhatsApp message via Twilio: {e}")
            raise HTTPException(status_code=502, detail="An unexpected error occurred while sending the WhatsApp message.")

        return {"message": "Review request WhatsApp message sent successfully."}

    else:
        raise HTTPException(status_code=400, detail=f"Unsupported channel: {payload.channel}")
