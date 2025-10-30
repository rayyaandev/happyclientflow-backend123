import csv
import io
from fastapi import APIRouter, File, UploadFile, HTTPException, Depends
from pydantic import BaseModel
from typing import List, Dict, Any
from app.libs.auth import require_auth

router = APIRouter(prefix="/csv-utils", tags=["CSV Utils"])

REQUIRED_HEADERS = ["First Name", "Last Name", "Email", "Contact Channel", "Start Date"]

# German translations for CSV headers
GERMAN_HEADER_MAPPING = {
    "first name": ["first name", "vorname"],
    "last name": ["last name", "nachname"],
    "email": ["email", "e-mail"],
    "contact channel": ["contact channel", "kontaktkanal"],
    "start date": ["start date", "startdatum"],
    "title": ["title", "anrede"],
    "phone": ["phone", "telefon"],
    "product used": ["product used", "verwendetes produkt"]
}

class CSVParsedData(BaseModel):
    headers: List[str]
    data: List[Dict[str, Any]]

class CSVParseResponse(BaseModel):
    success: bool
    data: CSVParsedData | None = None
    error: str | None = None
    missing_headers: List[str] | None = None # To inform frontend about missing specific headers

@router.post("/parse-csv", response_model=CSVParseResponse)
async def parse_csv_file(
    file: UploadFile = File(...),
    current_user: str = Depends(require_auth)
):
    """
    Parses an uploaded CSV file and returns its headers and data.
    Checks for required headers: First Name, Last Name, Email, Contact Channel, Start Date.
    Expects UTF-8 encoded CSV.
    
    Args:
        file: The uploaded CSV file
        current_user: The authenticated user's ID from JWT token
    """
    print(f"[AUTH] Parsing CSV file for user: {current_user}")
    
    if not file.filename.endswith('.csv'):
        return CSVParseResponse(success=False, error="Invalid file type. Please upload a .csv file.")

    try:
        contents = await file.read()
        try:
            text_content = contents.decode('utf-8')
        except UnicodeDecodeError:
            text_content = contents.decode('utf-8-sig')

        string_io = io.StringIO(text_content)
        
        try:
            dialect = csv.Sniffer().sniff(string_io.read(2048)) # Increased sniff buffer
            string_io.seek(0)
        except csv.Error:
            dialect = 'excel' 

        reader = csv.DictReader(string_io, dialect=dialect)
        
        actual_headers = reader.fieldnames
        if not actual_headers:
            return CSVParseResponse(success=False, error="CSV file is empty or headers are missing.")

        # Normalize actual headers for comparison (e.g., strip whitespace, lower case)
        # This makes the check more robust to slight variations in header naming.
        # Support bilingual headers (English/German) and individual language headers
        normalized_actual_headers = {}
        for h in actual_headers:
            # Handle bilingual headers like "First Name/Vorname"
            if '/' in h:
                # Split bilingual header and add both parts
                english_part, german_part = h.split('/', 1)
                english_normalized = english_part.strip().lower()
                german_normalized = german_part.strip().lower()
                normalized_actual_headers[english_normalized] = h
                normalized_actual_headers[german_normalized] = h
            else:
                # Single language header
                normalized_key = h.strip().lower()
                normalized_actual_headers[normalized_key] = h
        
        normalized_required_headers = [rh.lower() for rh in REQUIRED_HEADERS]

        missing_headers = []
        for req_header_lower in normalized_required_headers:
            original_req_header = next((rh for rh in REQUIRED_HEADERS if rh.lower() == req_header_lower), req_header_lower)
            # Check if this required header exists in any form (English, German, or bilingual)
            found = False
            if req_header_lower in normalized_actual_headers:
                found = True
            else:
                # Check German equivalents
                if req_header_lower in GERMAN_HEADER_MAPPING:
                    for variant in GERMAN_HEADER_MAPPING[req_header_lower]:
                        if variant in normalized_actual_headers:
                            found = True
                            break
            
            if not found:
                missing_headers.append(original_req_header) # Report with original capitalization
        
        if missing_headers:
            error_message = f"Missing required columns: {', '.join(missing_headers)}. Please ensure your CSV contains these columns."
            # Also return the actual headers found so the user can compare, along with missing_headers list
            return CSVParseResponse(success=False, error=error_message, missing_headers=missing_headers, data=CSVParsedData(headers=actual_headers, data=[]))

        data = [row for row in reader]

        if not data and actual_headers: # Check if there are headers but no data
             return CSVParseResponse(success=False, error="CSV file contains headers but no data rows.", data=CSVParsedData(headers=actual_headers, data=[]))

        return CSVParseResponse(success=True, data=CSVParsedData(headers=actual_headers, data=data))

    except UnicodeDecodeError:
        return CSVParseResponse(success=False, error="Failed to decode CSV file. Please ensure it is UTF-8 encoded.")
    except csv.Error as e:
        return CSVParseResponse(success=False, error=f"Error parsing CSV file: {str(e)}. Please check the file format.")
    except Exception as e:
        print(f"Unexpected error parsing CSV: {e}")
        return CSVParseResponse(success=False, error=f"An unexpected error occurred: {str(e)}")
    finally:
        await file.close()

