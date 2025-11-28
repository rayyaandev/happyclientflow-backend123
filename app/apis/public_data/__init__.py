"""
This API module provides public-facing endpoints that do not require user authentication.
It is designed to expose necessary data for features like the anonymous feedback form,
where information related to a company (e.g., products, employees) needs to be accessible
via a non-sensitive identifier like a client_id.

This endpoint now uses the Supabase service client, following the correct pattern
established in other parts of the application to ensure stable database connections.
"""
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import List, Dict
import os
from supabase import Client, create_client
import databutton as db

router = APIRouter(prefix="/public", tags=["Public Data"])

# ===============================================================================
# Supabase Client Dependency
# ===============================================================================

def get_supabase_service_client():
    """Initializes and returns a Supabase client with the service role key."""
    supabase_url = db.secrets.get("SUPABASE_URL")
    service_key = db.secrets.get("SUPABASE_SERVICE_KEY")
    if not supabase_url or not service_key:
        raise HTTPException(status_code=500, detail="Supabase configuration missing.")
    return create_client(supabase_url, service_key)

# ===============================================================================
# Pydantic Models for API Request and Response
# ===============================================================================

class CompanyInfoRequest(BaseModel):
    client_id: str

class Product(BaseModel):
    id: str
    name: str

class Employee(BaseModel):
    id: str
    full_name: str

class Profile(BaseModel):
    id: str
    name: str
    profile_type: str
    link: str

class Company(BaseModel):
    id: str
    name: str
    logo_url: str
    donation_text: str
    is_donation_message_displayed: bool

class CompanyInfoResponse(BaseModel):
    products: List[Product]
    employees: List[Employee]
    employee_profiles: Dict[str, List[Profile]]
    company: Company = None

# ===============================================================================
# API Endpoint
# ===============================================================================

@router.post("/company-info", response_model=CompanyInfoResponse)
def get_company_info_for_feedback(
    request: CompanyInfoRequest,
    supabase: Client = Depends(get_supabase_service_client)
):
    """
    Fetches the products and employees associated with a company, identified
    by a client_id in the request body. This is for use in the public feedback form.
    """
    client_id = request.client_id
    if not client_id:
        raise HTTPException(status_code=400, detail="client_id is required.")

    try:
        # 1. Get company_id from client_id
        client_response = supabase.from_("clients").select("company_id").eq("id", client_id).execute()
        if not client_response.data:
            raise HTTPException(status_code=404, detail="Client not found or not associated with a company.")
        
        company_id = client_response.data[0]['company_id']

        # 2. Get company information including donation fields
        company_response = supabase.from_("companies").select("id, name, logo_url, donation_text, is_donation_message_displayed").eq("id", company_id).execute()
        company_data = None
        if company_response.data:
            row = company_response.data[0]
            company_data = Company(
                id=str(row['id']),
                name=row['name'],
                logo_url=row.get('logo_url'),
                donation_text=row.get('donation_text'),
                is_donation_message_displayed=row.get('is_donation_message_displayed', False)
            )

        # 3. Get all products for the company
        products_response = supabase.from_("products").select("id, name").eq("company_id", company_id).order("name").execute()
        products = [Product(id=str(row['id']), name=row['name']) for row in products_response.data]

        # 4. Get all employees for the company
        users_response = supabase.from_("users").select("id, first_name, last_name").eq("company_id", company_id).execute()
        
        employees = []
        employee_ids = []
        for row in users_response.data:
            full_name = f"{row.get('first_name') or ''} {row.get('last_name') or ''}".strip()
            if full_name:
                employee_id = str(row['id'])
                employees.append(Employee(id=employee_id, full_name=full_name))
                employee_ids.append(employee_id)

        # 5. Get all profiles for the company
        profiles_response = supabase.from_("profiles").select("id, name, profile_type, link").eq("company_id", company_id).execute()
        profiles_data = {str(row['id']): Profile(id=str(row['id']), name=row['name'], profile_type=row['profile_type'], link=row['link']) for row in profiles_response.data}

        # 6. Get employee-profile mappings
        employee_profiles_map: Dict[str, List[Profile]] = {}
        if employee_ids: # Only query if there are employees
            profile_employees_response = supabase.from_("profile_employees").select("profile_id, employee_id").in_("employee_id", employee_ids).execute()
        
            # 7. Construct the employee_profiles dictionary
            for mapping in profile_employees_response.data:
                profile_id = str(mapping['profile_id'])
                employee_id = str(mapping['employee_id'])
                if profile_id in profiles_data:
                    if employee_id not in employee_profiles_map:
                        employee_profiles_map[employee_id] = []
                    employee_profiles_map[employee_id].append(profiles_data[profile_id])

        print(f"DEBUG company_data hello is it saving or what: {company_data} {profiles_response} {employee_profiles_map}, {employee_ids}, {employees}")

        return CompanyInfoResponse(
            products=products, 
            employees=employees, 
            employee_profiles=employee_profiles_map,
            company=company_data
        )

    except HTTPException as e:
        raise e
    except Exception as e:
        print(f"An unexpected error occurred while fetching company info for client_id {client_id}: {e}")
        raise HTTPException(status_code=500, detail="An internal error occurred while fetching company information.")
