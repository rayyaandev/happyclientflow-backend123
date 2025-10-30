# src/app/apis/sms/__init__.py
# This API is for sending SMS messages using Twilio.

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
import databutton as db
from twilio.rest import Client

router = APIRouter()

class SmsRequest(BaseModel):
    to: str
    body: str

@router.post("/send-sms", tags=["sms"])
def send_sms(request: SmsRequest):
    """
    Sends an SMS message to a specified phone number using Twilio.
    - **to**: The recipient's phone number in E.164 format (e.g., +14155238886).
    - **body**: The text of the message to send.
    """
    try:
        # Initialize Twilio Client
        try:
            account_sid = db.secrets.get("TWILIO_ACCOUNT_SID")
            auth_token = db.secrets.get("TWILIO_AUTH_TOKEN")
            twilio_from_number = db.secrets.get("TWILIO_FROM_NUMBER")
            
            if not all([account_sid, auth_token, twilio_from_number]):
                raise HTTPException(
                    status_code=500,
                    detail="Twilio credentials are not configured. Please set TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, and TWILIO_FROM_NUMBER secrets.",
                )

            # Initialize Twilio client
            client = Client(account_sid, auth_token)

        except Exception as e:
            # Log the error for debugging
            print(f"Error initializing Twilio client: {e}")
            # Return a generic error response
            raise HTTPException(
                status_code=500, detail=f"Failed to initialize Twilio client: {str(e)}"
            )

        # Send the message
        message = client.messages.create(
            to=request.to,
            from_=twilio_from_number,
            body=request.body,
            status_callback="https://api.databutton.com/_projects/722024f4-d06c-4ad3-9271-27bac1ebab31/dbtn/devx/app/routes/twilio_callbacks/status",
        )

        return {"status": "success", "message_sid": message.sid}

    except Exception as e:
        # Log the error for debugging
        print(f"Error sending SMS with Twilio: {e}")
        # Return a generic error response
        raise HTTPException(
            status_code=500, detail=f"Failed to send SMS: {str(e)}"
        )
