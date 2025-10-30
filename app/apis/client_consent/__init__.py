"""
This API is responsible for sending a confirmation email to the company owner
when a new client completes the consent form.
"""
from fastapi import APIRouter, Depends, Body
from pydantic import BaseModel, Field
import databutton as db
import sendgrid
from sendgrid.helpers.mail import Mail, From
import os
import supabase

# Supabase setup
supabase_url = db.secrets.get("SUPABASE_URL")
supabase_key = db.secrets.get("SUPABASE_SERVICE_KEY")
supabase_client = supabase.create_client(supabase_url, supabase_key)

router = APIRouter(prefix="/v1/client-consent", tags=["client-consent"])

class SendConsentConfirmationEmailRequest(BaseModel):
    company_id: str = Field(..., description="The ID of the company.")
    client_name: str = Field(..., description="The name of the client who gave consent.")

class ResponseMessage(BaseModel):
    message: str

def get_company_owner_details(company_id: str) -> dict:
    """Fetches the email and language of the company owner from the users table."""
    try:
        # Get the owner_id from the companies table
        company_response = supabase_client.from_("companies").select("owner_id").eq("id", company_id).single().execute()
        if not company_response.data:
            raise ValueError(f"Company with ID {company_id} not found.")
        
        owner_id = company_response.data.get("owner_id")
        if not owner_id:
            raise ValueError(f"Owner not found for company ID {company_id}.")

        # Get the user's details from the users table
        user_response = supabase_client.from_("users").select("email, language").eq("id", owner_id).single().execute()
        if not user_response.data:
            raise ValueError(f"User with ID {owner_id} not found.")
            
        return {
            "email": user_response.data.get("email"),
            "language": user_response.data.get("language", "en")  # Default to 'en'
        }

    except Exception as e:
        print(f"Error fetching company owner email: {e}")
        return None

def _send_consent_email(email_to: str, client_name: str, company_name: str, language: str = "en"):
    """Sends the actual consent confirmation email in the owner's preferred language."""
    if not email_to:
        print("No email address provided for company owner.")
        return False

    sg = sendgrid.SendGridAPIClient(api_key=db.secrets.get("SENDGRID_API_KEY"))
    sendgrid_from_email = "noreply@happyclientflow.de"

    if language == "de":
        subject = f"Neue Einwilligung von Ihrem Kunden: {client_name}"
        html_content = f"""
        <html>
            <body>
                <p>Hallo,</p>
                <p>ein neuer Kunde, <strong>{client_name}</strong>, hat soeben seine Einwilligung gegeben, von Ihrem Unternehmen <strong>{company_name}</strong> zum Zwecke des Feedbacks kontaktiert zu werden.</p>
                <p>Er wurde zu Ihrer Kundenliste in Happy Client Flow hinzugefügt.</p>
                <p>Mit freundlichen Grüßen</p>
                <p>Ihr Team von Happy Client Flow</p>
                <img src="https://static.databutton.com/public/722024f4-d06c-4ad3-9271-27bac1ebab31/CORO-FF_BlackTextNoSubheadline.png" alt="Happy Client Flow Logo" width="200"/>
            </body>
        </html>
        """
    else: # Default to English
        subject = f"New consent from your client: {client_name}"
        html_content = f"""
        <html>
            <body>
                <p>Hello,</p>
                <p>A new client, <strong>{client_name}</strong>, has just given their consent to be contacted for feedback by your company, <strong>{company_name}</strong>.</p>
                <p>They have been added to your client list in Happy Client Flow.</p>
                <p>Best regards,</p>
                <p>Your Happy Client Flow Team</p>
                <img src="https://static.databutton.com/public/722024f4-d06c-4ad3-9271-27bac1ebab31/CORO-FF_BlackTextNoSubheadline.png" alt="Happy Client Flow Logo" width="200"/>
            </body>
        </html>
        """

    message = Mail(
        from_email=From(sendgrid_from_email, "Happy Client Flow"),
        to_emails=email_to,
        subject=subject,
        html_content=html_content
    )
    
    try:
        response = sg.send(message)
        print(f"Email sent to {email_to}, status code: {response.status_code}")
        return response.status_code in [200, 202]
    except Exception as e:
        print(f"Error sending email: {e}")
        return False

@router.post("/send-consent-email", response_model=ResponseMessage, name="send_consent_confirmation_email")
async def send_consent_confirmation_email_api(payload: SendConsentConfirmationEmailRequest):
    """
    Receives company and client details and triggers sending the consent confirmation email.
    """
    company_response = supabase_client.from_("companies").select("name").eq("id", payload.company_id).single().execute()
    if not company_response.data:
        return {"message": "Company not found."}
    
    company_name = company_response.data.get("name")
    
    owner_details = get_company_owner_details(payload.company_id)
    if not owner_details:
        return {"message": "Could not retrieve company owner details."}
    
    email_sent = _send_consent_email(
        email_to=owner_details.get("email"),
        client_name=payload.client_name,
        company_name=company_name,
        language=owner_details.get("language")
    )
    
    if email_sent:
        return {"message": "Consent confirmation email sent successfully."}
    else:
        return {"message": "Failed to send consent confirmation email."}
