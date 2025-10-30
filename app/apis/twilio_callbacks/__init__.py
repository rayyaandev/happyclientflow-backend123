"""
Twilio Callbacks API

This API provides endpoints to handle callbacks from Twilio services,
such as status updates for sent SMS messages.
"""
from fastapi import APIRouter, Form, Response
from twilio.request_validator import RequestValidator
import databutton as db
import os

router = APIRouter(prefix="/twilio_callbacks")

@router.post("/status")
async def twilio_status_callback(
    MessageSid: str = Form(...),
    MessageStatus: str = Form(...),
):
    """
    Receives status updates from Twilio for SMS messages.
    Logs the message SID and its status.
    """
    print(f"Twilio Status Update: SID={MessageSid}, Status={MessageStatus}")
    
    # Here you can add logic to update your database with the new status
    # For example, find the message by its SID and update its status field.
    
    return Response(status_code=204)
