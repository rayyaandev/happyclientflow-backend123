"""
This API endpoint provides a secure method for creating feedback entries.
It is designed to be called from the frontend when an anonymous or unauthenticated user
submits feedback. This endpoint uses a service-level Supabase client to bypass
Row Level Security (RLS) policies, ensuring that feedback can be created by any user.

Reminder lifecycle is handled by /v1/reminders/process (low-star skip, external click,
non-follow-up templates) and by /mark-external-review-clicked (and legacy
/mark-google-review-clicked) — not by create-feedback.

4-star internal follow-up:
  POST /submit-internal-feedback  — saves optional callback note/request and
  sends a SendGrid notification e-mail to the company owner.
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import List, Optional
from supabase import Client, create_client
import databutton as db
import datetime

from app.libs.reminder_scheduling import cancel_pending_reminders_for_client

router = APIRouter()


class FeedbackContent(BaseModel):
    products: Optional[List[str]] = None
    employee: Optional[str] = None
    other_product: Optional[str] = None
    other_employee: Optional[str] = None
    collaboration_feeling: Optional[str] = None
    highlight: Optional[str] = None
    improvements: Optional[str] = None


class CreateFeedbackRequest(BaseModel):
    client_id: str
    satisfaction: int
    recommendation: str
    content: FeedbackContent


class MarkGoogleReviewClickedRequest(BaseModel):
    client_id: str


class MarkExternalReviewClickedRequest(BaseModel):
    client_id: str
    profile_type: Optional[str] = None


class SubmitInternalFeedbackRequest(BaseModel):
    """
    Payload for the 4-star internal follow-up screen.
    Both fields are optional — the client may submit a note, request a callback,
    or do neither (skip).  Even a skip is recorded so we have a clear paper-trail.
    """
    client_id: str
    feedback_id: str
    callback_requested: Optional[bool] = False
    callback_note: Optional[str] = None


class SubmitInternalFeedbackResponse(BaseModel):
    ok: bool
    message: str


def get_supabase_service_client():
    """Initializes and returns a Supabase client with the service role key."""
    supabase_url = db.secrets.get("SUPABASE_URL")
    service_key = db.secrets.get("SUPABASE_SERVICE_KEY")
    if not supabase_url or not service_key:
        raise HTTPException(status_code=500, detail="Supabase configuration missing.")
    return create_client(supabase_url, service_key)


def _record_external_review_platform_click(supabase: Client, client_id: str) -> dict:
    """
    Cancels pending reminders, sets clicked_google_link (any external review CTA),
    and sets review_status to ReviewComplete.
    """
    n = cancel_pending_reminders_for_client(supabase, client_id)
    supabase.from_("clients").update(
        {
            "clicked_google_link": True,
            "review_status": "ReviewComplete",
        }
    ).eq("id", client_id).execute()

    verify = (
        supabase.from_("clients")
        .select("id, clicked_google_link, review_status")
        .eq("id", client_id)
        .limit(1)
        .execute()
    )
    vdata = getattr(verify, "data", None) or []
    row = vdata[0] if vdata else None
    if not row:
        raise HTTPException(
            status_code=404,
            detail="Client not found after update.",
        )
    if not row.get("clicked_google_link") or row.get("review_status") != "ReviewComplete":
        print(
            f"mark_external_review_clicked: update did not persist for client_id={client_id!r} "
            f"row={row!r}"
        )
        raise HTTPException(
            status_code=404,
            detail="Could not save external review click (column missing or RLS?).",
        )

    return {
        "message": "OK",
        "reminders_cancelled": n,
        "clicked_google_link": row.get("clicked_google_link"),
        "review_status": row.get("review_status"),
    }


@router.post("/create-feedback")
def create_feedback(
    request: CreateFeedbackRequest,
    supabase: Client = Depends(get_supabase_service_client),
):
    """
    Creates a new feedback entry in the database.
    """
    try:
        feedback_insert_data = {
            "client_id": request.client_id,
            "satisfaction": request.satisfaction,
            "recommendation": request.recommendation,
            "content": request.content.model_dump(),
        }

        response = supabase.from_("feedback").insert(feedback_insert_data).execute()

        if not response.data:
            raise HTTPException(status_code=500, detail="Failed to create feedback entry.")

        # Anonymous feedback page cannot update clients via browser Supabase (RLS).
        # Service client sets survey-complete status here.
        cid = (request.client_id or "").strip()
        if cid:
            try:
                supabase.from_("clients").update(
                    {"review_status": "FeedbackSubmitted"}
                ).eq("id", cid).execute()
            except Exception as upd_err:
                # Do not fail the request: feedback row is already stored; client may retry and duplicate.
                print(
                    f"create_feedback: WARNING could not set FeedbackSubmitted for client_id={cid!r}: {upd_err}"
                )

        return response.data[0]

    except HTTPException:
        raise
    except Exception as e:
        print(f"Error creating feedback: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/mark-external-review-clicked")
def mark_external_review_clicked(
    body: MarkExternalReviewClickedRequest,
    supabase: Client = Depends(get_supabase_service_client),
):
    """
    Cancels pending reminders and sets clicked_google_link when the user opens any
    external review CTA (Google, Anwalt.de, Trustpilot, etc.).
    """
    try:
        client_id = (body.client_id or "").strip()
        if not client_id:
            raise HTTPException(status_code=400, detail="client_id is required.")

        if body.profile_type:
            print(
                f"mark_external_review_clicked: client_id={client_id!r} "
                f"profile_type={body.profile_type!r}"
            )

        return _record_external_review_platform_click(supabase, client_id)
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error in mark_external_review_clicked for {body.client_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/mark-google-review-clicked")
def mark_google_review_clicked(
    body: MarkGoogleReviewClickedRequest,
    supabase: Client = Depends(get_supabase_service_client),
):
    """
    Legacy alias for mark-external-review-clicked (Google-only clients).
    """
    try:
        client_id = (body.client_id or "").strip()
        if not client_id:
            raise HTTPException(status_code=400, detail="client_id is required.")

        return _record_external_review_platform_click(supabase, client_id)
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error in mark_google_review_clicked for {body.client_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Helpers for the 4-star internal follow-up
# ---------------------------------------------------------------------------

def _get_company_owner_email(supabase: Client, company_id: str) -> Optional[str]:
    """
    Returns the e-mail address of the company owner, or None on any error.
    Pattern mirrors client_consent API.
    """
    try:
        company_res = supabase.from_("companies").select("owner_id, name").eq("id", company_id).single().execute()
        if not company_res.data:
            return None
        owner_id = company_res.data.get("owner_id")
        if not owner_id:
            return None
        user_res = supabase.from_("users").select("email").eq("id", owner_id).single().execute()
        return (user_res.data or {}).get("email")
    except Exception as exc:
        print(f"_get_company_owner_email: could not look up owner for company {company_id}: {exc}")
        return None


def _send_callback_notification(
    owner_email: str,
    company_name: str,
    client_id: str,
    callback_note: Optional[str],
) -> bool:
    """
    Sends a SendGrid e-mail to the company owner when a client requests a callback.
    Non-critical: returns False on failure instead of raising.
    """
    try:
        from sendgrid import SendGridAPIClient
        from sendgrid.helpers.mail import Mail, From

        api_key = db.secrets.get("SENDGRID_API_KEY")
        if not api_key:
            print("_send_callback_notification: SENDGRID_API_KEY not configured.")
            return False

        note_html = (
            f"<blockquote style='border-left:4px solid #3b82f6;padding:8px 16px;color:#334155;background:#f8fafc'>"
            f"{callback_note.replace(chr(10), '<br>')}"
            f"</blockquote>"
            if callback_note
            else "<p style='color:#94a3b8'><em>(no additional note)</em></p>"
        )

        html_body = f"""
        <html><body style="font-family:Inter,sans-serif;color:#0f172a">
          <h2 style="color:#2563eb">📞 Callback Request from Client Feedback</h2>
          <p>A client has requested a callback after submitting their 4-star feedback on
             <strong>Happy Client Flow</strong>.</p>
          <table style="border-collapse:collapse;width:100%;max-width:500px">
            <tr><td style="padding:6px 12px;font-weight:600;width:140px">Company</td>
                <td style="padding:6px 12px">{company_name}</td></tr>
            <tr style="background:#f8fafc">
                <td style="padding:6px 12px;font-weight:600">Client ID</td>
                <td style="padding:6px 12px;font-family:monospace">{client_id}</td></tr>
            <tr><td style="padding:6px 12px;font-weight:600">Submitted</td>
                <td style="padding:6px 12px">{datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}</td></tr>
          </table>
          <h3 style="margin-top:20px">Client's additional note:</h3>
          {note_html}
          <hr style="border:none;border-top:1px solid #e2e8f0;margin:24px 0">
          <p style="color:#64748b;font-size:12px">
            This notification was sent automatically by Happy Client Flow.<br>
            Please reach out to the client within 24 hours as promised.
          </p>
        </body></html>
        """

        message = Mail(
            from_email=From("noreply@happyclientflow.de", "Happy Client Flow"),
            to_emails=owner_email,
            subject=f"[HCF] Callback requested — {company_name}",
            html_content=html_body,
        )
        sg = SendGridAPIClient(api_key)
        response = sg.send(message)
        print(
            f"_send_callback_notification: sent to {owner_email}, "
            f"status={response.status_code}"
        )
        return response.status_code in (200, 202)
    except Exception as exc:
        print(f"_send_callback_notification: failed → {exc}")
        return False


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@router.post("/submit-internal-feedback", response_model=SubmitInternalFeedbackResponse)
def submit_internal_feedback(
    body: SubmitInternalFeedbackRequest,
    supabase: Client = Depends(get_supabase_service_client),
):
    """
    Called by the 4-star internal follow-up screen after the main feedback was
    already submitted via /create-feedback.

    - Persists callback_requested and callback_note on the feedback row.
    - If callback_requested is true, sends a SendGrid notification e-mail to
      the company owner so they can follow up within 24 hours.

    All errors are non-fatal: the feedback row already exists; this is bonus data.
    """
    client_id = (body.client_id or "").strip()
    feedback_id = (body.feedback_id or "").strip()

    if not client_id or not feedback_id:
        raise HTTPException(status_code=400, detail="client_id and feedback_id are required.")

    # 1. Persist callback fields on the feedback row
    try:
        supabase.from_("feedback").update(
            {
                "callback_requested": bool(body.callback_requested),
                "callback_note": (body.callback_note or "").strip() or None,
            }
        ).eq("id", feedback_id).execute()
    except Exception as exc:
        print(f"submit_internal_feedback: could not update feedback row {feedback_id}: {exc}")
        # Non-fatal — continue to try notification

    # 2. Notify company owner if callback was requested
    if body.callback_requested:
        try:
            # Resolve company_id from client
            client_res = supabase.from_("clients").select("company_id").eq("id", client_id).single().execute()
            company_id = (client_res.data or {}).get("company_id")

            if company_id:
                # Get company name
                company_res = supabase.from_("companies").select("name").eq("id", company_id).single().execute()
                company_name = (company_res.data or {}).get("name", "Your company")

                # Get owner e-mail
                owner_email = _get_company_owner_email(supabase, company_id)
                if owner_email:
                    _send_callback_notification(
                        owner_email=owner_email,
                        company_name=company_name,
                        client_id=client_id,
                        callback_note=body.callback_note,
                    )
                else:
                    print(f"submit_internal_feedback: no owner email found for company {company_id}")
            else:
                print(f"submit_internal_feedback: no company_id on client {client_id}")
        except Exception as exc:
            print(f"submit_internal_feedback: notification error: {exc}")

    return SubmitInternalFeedbackResponse(ok=True, message="Internal feedback recorded.")
