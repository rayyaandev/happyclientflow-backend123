"""
Brevo Email Automation API

Provides endpoints for integrating with Brevo email marketing platform.
Used to automatically add new users to email automation sequences.

Endpoints:
- POST /brevo/add-contact: Add new contact to Brevo list

Used in: Onboarding completion flow
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, EmailStr
import requests
import databutton as db
from typing import Optional

router = APIRouter()

class AddContactRequest(BaseModel):
    email: EmailStr
    first_name: str
    last_name: str
    company_name: Optional[str] = None

class AddContactResponse(BaseModel):
    success: bool
    message: str
    contact_id: Optional[int] = None

@router.post("/add-contact")
def add_contact_to_brevo(body: AddContactRequest) -> AddContactResponse:
    """
    Add a new contact to Brevo and assign to "New Users - Month 1" list.
    
    This endpoint is called when a user completes onboarding to automatically
    add them to email automation sequences.
    """
    try:
        # Get Brevo API key from secrets
        api_key = db.secrets.get("BREVO_API_KEY")
        print(f"Attempting to use Brevo API key. Key exists: {bool(api_key)}. Key starts with: {api_key[:4] if api_key else 'None'}")

        if not api_key:
            print("❌ Brevo API key not found in secrets.")
            raise HTTPException(status_code=500, detail="Brevo API key not configured")
        
        # Brevo API endpoint
        url = "https://api.brevo.com/v3/contacts"
        
        # Request headers
        headers = {
            "accept": "application/json",
            "content-type": "application/json",
            "api-key": api_key,
        }
        
        # Prepare contact data
        contact_data = {
            "email": body.email,
            "attributes": {
                "FIRSTNAME": body.first_name,
                "LASTNAME": body.last_name,
            },
            # Add to "New Users - Month 1" list (ID: 18)
            "listIds": [18]
        }
        
        # Add company name if provided
        if body.company_name:
            contact_data["attributes"]["COMPANY"] = body.company_name
        
        print(f"Sending data to Brevo: {contact_data}")
        
        # Make API call to Brevo
        response = requests.post(url, headers=headers, json=contact_data)
        
        print(f"Received response from Brevo. Status: {response.status_code}. Body: {response.text}")

        if response.status_code == 201:
            # Success - contact created
            result = response.json()
            contact_id = result.get("id")
            print(f"✅ Contact added to Brevo successfully: ID {contact_id}")
            
            return AddContactResponse(
                success=True,
                message="Contact added to Brevo successfully",
                contact_id=contact_id
            )
        
        elif response.status_code == 400:
            # Contact might already exist
            error_detail = response.json()
            if "already exists" in str(error_detail).lower():
                print(f"⚠️ Contact already exists in Brevo: {body.email}")
                return AddContactResponse(
                    success=True,
                    message="Contact already exists in Brevo"
                )
            else:
                print(f"❌ Brevo API error (400): {error_detail}")
                raise HTTPException(status_code=400, detail=f"Brevo API error: {error_detail}")
        
        else:
            # Other error
            print(f"❌ Unhandled Brevo API error. Status: {response.status_code}. Body: {response.text}")
            raise HTTPException(
                status_code=response.status_code, 
                detail=f"Brevo API error: {response.text}"
            )
    
    except requests.exceptions.RequestException as e:
        print(f"❌ Network error calling Brevo API: {e}", flush=True)
        raise HTTPException(status_code=500, detail=f"Network error: {str(e)}")
    
    except Exception as e:
        print(f"❌ Unexpected error in add_contact_to_brevo: {e}", flush=True)
        raise HTTPException(status_code=500, detail=f"Unexpected error: {str(e)}")
