"""
This API module is responsible for processing and sending scheduled reminders.
It is designed to be triggered by a cron job. External review CTAs are recorded via
clients.clicked_google_link (any platform) through create_feedback routes.
"""
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
import databutton as db
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail, From
from supabase import create_client, Client
from app.env import mode, Mode
import datetime
from typing import List, Optional, Dict, Any
from twilio.rest import Client as TwilioClient
import json

from app.libs.reminder_scheduling import (
    GOOGLE_REVIEW_FOLLOWUP_KIND,
    feedback_high_satisfaction_min,
    is_scheduled_followup_template,
    prefetch_latest_feedback_satisfaction,
)

router = APIRouter(prefix="/v1/reminders", tags=["reminders"])


def build_sender_display_name(company_name: Optional[str]) -> str:
    company = (company_name or "").strip()
    return f"{company} via Happy Client Flow" if company else "Happy Client Flow"


def build_rich_link(url: Optional[str], title: str) -> str:
    if not url:
        return ""
    safe_url = str(url).strip()
    if not safe_url:
        return ""
    return (
        f'<a href="{safe_url}" '
        'style="color:#2563eb;text-decoration:underline;font-weight:600">'
        f"{title}</a>"
    )


def get_link_title(key: str, language: str) -> str:
    labels = {
        "review_link": {
            "de": "Feedback abgeben",
            "en": "Leave feedback",
        },
        "google_review_link": {
            "de": "Google-Bewertung abgeben",
            "en": "Leave a Google review",
        },
    }
    return labels.get(key, {}).get(language, labels.get(key, {}).get("en", key))

def get_supabase_client() -> Client:
    supabase_url = db.secrets.get("SUPABASE_URL")
    supabase_key = db.secrets.get("SUPABASE_SERVICE_KEY")
    if not supabase_url or not supabase_key:
        raise HTTPException(status_code=500, detail="Supabase connection details not configured.")
    return create_client(supabase_url, supabase_key)

@router.post("/process", name="process_reminders")
async def process_reminders():
    """
    Processes all pending reminders that are due to be sent.
    """
    supabase = get_supabase_client()
    now = datetime.datetime.now(datetime.timezone.utc)

    # 1. Fetch due reminders
    try:
        reminders_res = supabase.table("reminders").select("*").eq("sent_status", "pending").lt("scheduled_at", now.isoformat()).execute()
        if not reminders_res.data:
            return {"message": "No due reminders to process."}
        due_reminders = reminders_res.data
    except Exception as e:
        print(f"Error fetching due reminders: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch due reminders.")

    client_ids = list({r["client_id"] for r in due_reminders if r.get("client_id")})
    latest_sat_by_client: Dict[str, Any] = prefetch_latest_feedback_satisfaction(
        supabase, client_ids
    )
    min_stars = feedback_high_satisfaction_min()

    sent_count = 0
    failed_ids = []

    for reminder in due_reminders:
        try:
            cid = reminder.get("client_id")
            raw_sat = latest_sat_by_client.get(cid) if cid else None
            if raw_sat is not None:
                try:
                    sat_val = int(raw_sat)
                except (TypeError, ValueError):
                    sat_val = None
                if sat_val is not None and sat_val < min_stars:
                    supabase.table("reminders").update({"sent_status": "cancelled"}).eq(
                        "id", reminder["id"]
                    ).execute()
                    print(
                        f"Skipped reminder {reminder['id']}: latest feedback satisfaction "
                        f"{sat_val} < {min_stars}"
                    )
                    continue

            # 2. Fetch message template
            template_res = (
                supabase.table("message_templates")
                .select("*")
                .eq("id", reminder["template_id"])
                .single()
                .execute()
            )
            if not template_res.data:
                raise Exception(f"Template not found for reminder {reminder['id']}")
            template = template_res.data

            if not is_scheduled_followup_template(template):
                supabase.table("reminders").update({"sent_status": "cancelled"}).eq(
                    "id", reminder["id"]
                ).execute()
                print(
                    f"Cancelled reminder {reminder['id']}: template is outreach / "
                    "not a scheduled follow-up (should not be sent as reminder)"
                )
                continue

            # Get client details (preferred channel and company_id) in one call
            client_res = supabase.table("clients").select(
                "preferred_contact_channel, company_id, phone, clicked_google_link, google_review_published"
            ).eq("id", reminder['client_id']).single().execute()
            if not client_res.data:
                raise Exception(f"Client not found for client_id {reminder['client_id']}")

            tmpl_kind = (template.get("template_kind") or "").strip().lower()
            is_google_review_followup = tmpl_kind == GOOGLE_REVIEW_FOLLOWUP_KIND

            if is_google_review_followup and client_res.data.get("google_review_published"):
                supabase.table("reminders").update({"sent_status": "cancelled"}).eq(
                    "id", reminder["id"]
                ).execute()
                continue

            if (
                not is_google_review_followup
                and client_res.data.get("clicked_google_link")
            ):
                supabase.table("reminders").update({"sent_status": "cancelled"}).eq(
                    "id", reminder["id"]
                ).execute()
                continue

            channel = client_res.data.get("preferred_contact_channel")
            client_phone = client_res.data.get("phone")
            if not channel:
                raise Exception(f"Preferred contact channel not found for client_id {reminder['client_id']}")
            
            company_id = client_res.data.get("company_id")

            # Fetch user language from the user associated with the client
            user_language = 'de' # Default to German
            if company_id:
                try:
                    # Find a user in that company to get the language
                    user_res = supabase.table("users").select("language").eq("company_id", company_id).limit(1).single().execute()
                    if user_res.data and user_res.data.get("language"):
                        user_language = user_res.data["language"]
                except Exception as e:
                    print(f"Could not determine user language for reminder {reminder['id']}, defaulting to 'de': {e}")

            # Translate title based on user's language
            original_title = reminder.get("title", "")
            translated_title = original_title
            if user_language == 'de':
                if original_title == 'Mr.' or original_title == 'Mr':
                    translated_title = 'Herr'
                elif original_title == 'Mrs.' or original_title == 'Mrs':
                    translated_title = 'Frau'

            # 3. Interpolate variables
            variables = {
                "{{title}}": translated_title,
                "{{first_name}}": reminder.get("first_name", ""),
                "{{last_name}}": reminder.get("last_name", ""),
                "{{company_name}}": reminder.get("company_name", ""),
                "{{product_name}}": reminder.get("product_name", ""),
                "{{review_link}}": reminder.get("review_link", ""),
                "{{google_review_link}}": reminder.get("google_review_link", ""),
            }
            subject = template['subject']
            body = template['body']

            for key, value in variables.items():
                replace_with = value if value is not None else ""
                if subject: subject = subject.replace(key, replace_with)
                if body: body = body.replace(key, replace_with)
            
            # Replace newlines with HTML line breaks for email rendering
            if body:
                body = body.replace('\n', '<br>')
                review_link = reminder.get("review_link", "")
                google_review_link = reminder.get("google_review_link", "")
                if review_link:
                    body = body.replace(
                        review_link,
                        build_rich_link(
                            review_link,
                            get_link_title("review_link", user_language),
                        ),
                    )
                if google_review_link:
                    body = body.replace(
                        google_review_link,
                        build_rich_link(
                            google_review_link,
                            get_link_title("google_review_link", user_language),
                        ),
                    )
            
            # 4. Send message by channel
            if channel == "Email":
                # 4. Send email
                sendgrid_api_key = db.secrets.get("SENDGRID_API_KEY")
                sendgrid_from_email = "noreply@happyclientflow.de"

                message = Mail(
                    from_email=From(
                        sendgrid_from_email,
                        build_sender_display_name(reminder.get("company_name")),
                    ),
                    to_emails=reminder['client_email'],
                    subject=subject,
                    html_content=body
                )
                sg = SendGridAPIClient(sendgrid_api_key)
                response = sg.send(message)

                if response.status_code >= 300:
                    raise Exception(f"SendGrid failed with status {response.status_code}")
            
            elif channel == "WhatsApp":
                twilio_account_sid = db.secrets.get("TWILIO_ACCOUNT_SID")
                twilio_auth_token = db.secrets.get("TWILIO_AUTH_TOKEN")
                twilio_from_number = db.secrets.get("TWILIO_FROM_NUMBER")

                if not all([twilio_account_sid, twilio_auth_token, twilio_from_number]):
                    raise Exception("Twilio WhatsApp configuration is missing.")

                if not client_phone:
                    raise Exception("Client phone number is required for WhatsApp channel.")

                template_name = template.get("name") or ""
                rule_type = template.get("rule_type")
                template_type = template.get("template_type")
                # Outreach ("Erste Nachricht") uses the same Twilio templates as review_requests WhatsApp.
                is_outreach = template_type == "Outreach" or "Erste Nachricht" in template_name

                content_sid = None
                if is_outreach and rule_type == "formal":
                    content_sid = "HX8991dce65c8ab28adea93518f63f3058"  # whatsapp_outreach_formal_v2
                elif is_outreach and rule_type == "informal":
                    content_sid = "HXe61bb8737035c5077500e6263d367afe"  # whatsapp_outreach_informal_v2
                elif "1. Erinnerung" in template_name and rule_type == "formal":
                    content_sid = "HX363218948b597c323bc628e54be1f9af" # whatsapp_reminder1_formal_v2
                elif "1. Erinnerung" in template_name and rule_type == "informal":
                    content_sid = "HXd8ffd916c5eddf9506e4f70a86d06fbe" # whatsapp_reminder1_informal_v2
                elif "2. Erinnerung" in template_name and rule_type == "formal":
                    content_sid = "HX3dfb020601addbcbed02fe683439cd9c" # whatsapp_reminder2_formal_v2
                elif "2. Erinnerung" in template_name and rule_type == "informal":
                    content_sid = "HX695b1182dfcb84dea5ece052e7e35614" # whatsapp_reminder2_informal_v2
                elif tmpl_kind == GOOGLE_REVIEW_FOLLOWUP_KIND and rule_type == "formal":
                    # Reuse survey reminder Twilio shells until dedicated GBP nudge templates exist.
                    if "2." in template_name or "2nd" in template_name.lower():
                        content_sid = "HX3dfb020601addbcbed02fe683439cd9c"
                    else:
                        content_sid = "HX363218948b597c323bc628e54be1f9af"

                if not content_sid:
                    raise Exception(f"Could not determine Content SID for template '{template_name}' with rule_type '{rule_type}'.")

                if rule_type == "formal":
                    content_variables = {
                        '1': reminder.get("client_id", ""),
                        '2': translated_title,
                        '3': reminder.get("last_name", ""),
                        '4': reminder.get("company_name", ""),
                    }
                else: # informal
                    content_variables = {
                        '1': reminder.get("client_id", ""),
                        '2': reminder.get("first_name", ""),
                        '3': reminder.get("company_name", ""),
                    }
                
                client = TwilioClient(twilio_account_sid, twilio_auth_token)
                message = client.messages.create(
                    content_sid=content_sid,
                    from_=f"whatsapp:{twilio_from_number}",
                    to=f"whatsapp:{client_phone}",
                    content_variables=json.dumps(content_variables)
                )
                print(f"WhatsApp reminder sent successfully, SID: {message.sid}")

            elif channel == "SMS":
                twilio_account_sid = db.secrets.get("TWILIO_ACCOUNT_SID")
                twilio_auth_token = db.secrets.get("TWILIO_AUTH_TOKEN")
                twilio_from_number = db.secrets.get("TWILIO_FROM_NUMBER")

                if not all([twilio_account_sid, twilio_auth_token, twilio_from_number]):
                    raise Exception("Twilio SMS configuration is missing.")

                if not client_phone:
                    raise Exception("Client phone number is required for SMS channel.")

                # The body for SMS should be plain text.
                sms_body = body.replace('<br>', '\n') if body else ''
                
                client = TwilioClient(twilio_account_sid, twilio_auth_token)
                message = client.messages.create(
                    to=client_phone,
                    from_=twilio_from_number,
                    body=sms_body,
                )
                print(f"SMS reminder sent successfully, SID: {message.sid}")

            # 5. Update reminder status
            supabase.table("reminders").update({"sent_status": "sent"}).eq("id", reminder['id']).execute()
            sent_count += 1

        except Exception as e:
            print(f"Failed to process reminder {reminder['id']}: {e}")
            failed_ids.append(reminder['id'])

    if failed_ids:
        return {"message": f"Processed reminders. Sent: {sent_count}. Failed: {len(failed_ids)}.", "failed_ids": failed_ids}
    
    return {"message": f"Successfully sent {sent_count} reminders."}
