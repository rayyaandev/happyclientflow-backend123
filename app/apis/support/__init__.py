# src/app/apis/support/__init__.py
# This API handles support ticket submissions from the contact form.
# Sends customer inquiries to service@happyclientflow.de via SendGrid.

import databutton as db
from fastapi import APIRouter, HTTPException, Body, Form, File, UploadFile, Depends
from pydantic import BaseModel, EmailStr
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail, Attachment, FileContent, FileName, FileType, Disposition
from app.env import mode, Mode
from app.libs.auth import require_auth
import base64
from typing import Optional
import datetime

router = APIRouter(prefix="/v1/support", tags=["support"])

# --- Pydantic Models ---
class SupportTicketRequest(BaseModel):
    name: str
    email: EmailStr
    subject: str
    message: str
    user_company: Optional[str] = None  # Will be populated from user session

class SupportTicketResponse(BaseModel):
    message: str
    ticket_id: str

# --- Helper Functions ---
def _send_support_ticket_email(ticket_id: str, user_name: str, user_email: EmailStr, subject: str, message: str, 
                              user_id: str, attachment: Optional[UploadFile] = None) -> bool:
    """
    Send support ticket email to service@happyclientflow.de
    """
    sendgrid_api_key = db.secrets.get("SENDGRID_API_KEY")
    
    # Use consistent from email pattern like other APIs
    if mode == Mode.PROD:
        sendgrid_from_email = "noreply@happyclientflow.de"
    else:
        sendgrid_from_email = "noreply@happyclientflow.de"
        
    support_email = "service@happyclientflow.de"
    
    if not sendgrid_api_key:
        print("ERROR: SendGrid API key not configured. Cannot send support email.")
        return False
    
    # Create email subject with prefix for easy filtering
    email_subject = f"[Support Ticket] {subject}"
    
    # Create HTML email body
    email_body = f"""
    <h3>New Support Ticket</h3>
    <p><strong>From:</strong> {user_name} ({user_email})</p>
    <p><strong>Subject:</strong> {subject}</p>
    <p><strong>Submitted:</strong> {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}</p>
    
    <h4>Message:</h4>
    <div style="background-color: #f8f9fa; padding: 15px; border-left: 4px solid #007bff; margin: 15px 0;">
        {message.replace(chr(10), '<br>')}
    </div>
    
    <hr>
    <p style="color: #666; font-size: 12px;">
        This support ticket was submitted via Happy Client Flow support form.<br>
        Please respond to: {user_email}
    </p>
    """
    
    # Create the email message
    mail_message = Mail(
        from_email=sendgrid_from_email,
        to_emails=support_email,
        subject=email_subject,
        html_content=email_body
    )
    
    # Add attachment if provided
    if attachment and attachment.size > 0:
        try:
            # Read file content
            file_content = attachment.file.read()
            encoded_file = base64.b64encode(file_content).decode()
            
            # Create attachment
            attached_file = Attachment(
                FileContent(encoded_file),
                FileName(attachment.filename),
                FileType(attachment.content_type or 'application/octet-stream'),
                Disposition('attachment')
            )
            mail_message.attachment = attached_file
            
        except Exception as e:
            print(f"Error processing attachment: {e}")
            # Continue without attachment rather than failing completely
    
    try:
        sg = SendGridAPIClient(sendgrid_api_key)
        response = sg.send(mail_message)
        print(f"Support ticket sent to {support_email} from {user_email}. Subject: {subject}. Status: {response.status_code}")
        return response.status_code in [200, 202]
    except Exception as e:
        print(f"Error sending support ticket email: {e}")
        return False

# --- API Endpoints ---
@router.post("/submit-ticket", response_model=SupportTicketResponse)
async def submit_support_ticket(
    name: str = Form(...),
    email: EmailStr = Form(...),
    subject: str = Form(...),
    message: str = Form(...),
    attachment: Optional[UploadFile] = File(None),
    current_user: str = Depends(require_auth)
):
    """
    Submit a support ticket via the contact form.
    Sends the ticket to service@happyclientflow.de via email.
    """
    
    # Validate required fields
    if not name.strip() or not email or not subject.strip() or not message.strip():
        raise HTTPException(status_code=400, detail="All required fields must be filled")
    
    # Note: current_user is just the user ID string, we don't have company info in JWT
    print(f"[AUTH] Support ticket submission for user: {current_user}")
    
    # Validate attachment size if provided (limit to 10MB)
    if attachment and attachment.size > 10485760:  # 10MB
        raise HTTPException(status_code=400, detail="Attachment size must be less than 10MB")
    
    # Generate a simple ticket ID for tracking
    ticket_id = f"HCF-{datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}"
    
    # Send the support email
    email_sent = _send_support_ticket_email(
        ticket_id=ticket_id,
        user_name=name,
        user_email=email,
        subject=subject,
        message=message,
        user_id=current_user,
        attachment=attachment
    )
    
    if not email_sent:
        raise HTTPException(status_code=500, detail="Failed to send support ticket email")
    
    return SupportTicketResponse(
        ticket_id=ticket_id,
        message="Support ticket submitted successfully. We will get back to you soon!"
    )
