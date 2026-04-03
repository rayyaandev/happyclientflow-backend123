"""
Google Business Profile OAuth (refresh token) and posting replies to reviews via My Business API v4.

Requires Google Cloud OAuth client (web) with redirect URI pointing to this API's callback.
Enable APIs: My Business Account Management, My Business Business Information, My Business API (v4).
"""
from __future__ import annotations

import base64
import json
import os
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Optional

import databutton as db
import requests
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from supabase import Client, create_client

router = APIRouter(prefix="/google-business", tags=["google_business"])

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
ACCOUNT_MGMT = "https://mybusinessaccountmanagement.googleapis.com/v1"
BUSINESS_INFO = "https://mybusinessbusinessinformation.googleapis.com/v1"
MYBUSINESS_V4 = "https://mybusiness.googleapis.com/v4"

# business.manage covers listing accounts/locations and posting replies (v4)
SCOPES = "https://www.googleapis.com/auth/business.manage"


def _read_secret(*keys: str) -> Optional[str]:
    """
    Read config: os.environ first (local .env via dotenv in main.py), then Databutton vault.
    db.secrets.get raises KeyError when a name is not registered in Databutton.
    """
    for key in keys:
        env_val = os.environ.get(key)
        if env_val and str(env_val).strip():
            return str(env_val).strip()
        try:
            val = db.secrets.get(key)
            if val and str(val).strip():
                return str(val).strip()
        except KeyError:
            pass
        except Exception:
            pass
    return None


def get_supabase() -> Client:
    url = _read_secret("SUPABASE_URL")
    key = _read_secret("SUPABASE_SERVICE_KEY")
    if not url or not key:
        raise HTTPException(status_code=500, detail="Supabase not configured")
    return create_client(url, key)


def _google_oauth_config() -> tuple[str, str, str]:
    client_id = _read_secret("GOOGLE_BUSINESS_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_ID")
    client_secret = _read_secret("GOOGLE_BUSINESS_CLIENT_SECRET", "GOOGLE_OAUTH_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise HTTPException(
            status_code=500,
            detail="Google OAuth not configured (GOOGLE_BUSINESS_CLIENT_ID / GOOGLE_BUSINESS_CLIENT_SECRET).",
        )
    backend_base = (
        _read_secret("BACKEND_PUBLIC_URL", "API_PUBLIC_URL")
        or "http://localhost:8000"
    )
    redirect_uri = f"{backend_base.rstrip('/')}/routes/google-business/oauth/callback"
    return client_id, client_secret, redirect_uri


def _refresh_access_token(refresh_token: str) -> dict[str, Any]:
    client_id, client_secret, _ = _google_oauth_config()
    r = requests.post(
        GOOGLE_TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=30,
    )
    if not r.ok:
        raise HTTPException(status_code=502, detail=f"Google token refresh failed: {r.text}")
    return r.json()


def _exchange_code_for_tokens(code: str) -> dict[str, Any]:
    client_id, client_secret, redirect_uri = _google_oauth_config()
    r = requests.post(
        GOOGLE_TOKEN_URL,
        data={
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
        timeout=30,
    )
    if not r.ok:
        raise HTTPException(status_code=502, detail=f"Google token exchange failed: {r.text}")
    return r.json()


def _star_enum_to_int(star: Optional[str]) -> int:
    if not star:
        return 0
    m = {"ONE": 1, "TWO": 2, "THREE": 3, "FOUR": 4, "FIVE": 5}
    return m.get(star.upper(), 0)


def _parse_rfc3339(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s.replace("Z", "+00:00")
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _norm(s: str) -> str:
    return " ".join(s.lower().split())


def _find_matching_review(
    reviews_payload: dict,
    rating: int,
    author_name: str,
    review_text: str,
    target_ts: Optional[float],
) -> Optional[str]:
    """Return review resource name if found."""
    items = reviews_payload.get("reviews") or []
    author_norm = _norm(author_name or "")
    text_prefix = (review_text or "").strip()[:80]
    best_name: Optional[str] = None
    best_score = 0

    for rev in items:
        name = rev.get("name") or ""
        st = _star_enum_to_int(rev.get("starRating"))
        reviewer = (rev.get("reviewer") or {}).get("displayName") or ""
        comment = (rev.get("comment") or "").strip()
        ct = _parse_rfc3339(rev.get("createTime"))
        ct_ts = ct.timestamp() if ct else None

        score = 0
        if st == rating:
            score += 5
        if text_prefix and comment.startswith(text_prefix[: min(len(comment), len(text_prefix))]):
            score += 4
        elif text_prefix and text_prefix[:40] in comment:
            score += 3
        if author_norm and _norm(reviewer) == author_norm:
            score += 3
        elif author_norm and author_norm[:5] in _norm(reviewer):
            score += 1
        if target_ts is not None and ct_ts is not None:
            if abs(ct_ts - target_ts) < 86400 * 3:
                score += 2

        if score > best_score:
            best_score = score
            best_name = name

    if best_score >= 8 and best_name:
        return best_name
    if best_score >= 6 and best_name:
        return best_name
    return None


class CreateGoogleOAuthLinkRequest(BaseModel):
    company_id: str
    return_url: str = Field(..., description="Frontend URL to redirect after success/failure")


class CreateGoogleOAuthLinkResponse(BaseModel):
    oauth_url: str


@router.post("/oauth/create-link", response_model=CreateGoogleOAuthLinkResponse)
def create_google_oauth_link(body: CreateGoogleOAuthLinkRequest):
    supabase = get_supabase()
    c = supabase.table("companies").select("id").eq("id", body.company_id).single().execute()
    if c is None or not getattr(c, "data", None):
        raise HTTPException(status_code=404, detail="Company not found")

    client_id, _, redirect_uri = _google_oauth_config()
    state_raw = f"{body.company_id}:{base64.urlsafe_b64encode(body.return_url.encode()).decode()}"
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPES,
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state_raw,
    }
    url = GOOGLE_AUTH_URL + "?" + urllib.parse.urlencode(params)
    return CreateGoogleOAuthLinkResponse(oauth_url=url)


@router.get("/oauth/callback")
async def google_oauth_callback(request: Request):
    params = dict(request.query_params)
    code = params.get("code")
    err = params.get("error")
    state = params.get("state") or ""

    def _fail_redirect(msg: str) -> RedirectResponse:
        if ":" in state:
            try:
                _cid, b64u = state.split(":", 1)
                ru = base64.urlsafe_b64decode(b64u.encode()).decode()
                sep = "&" if "?" in ru else "?"
                return RedirectResponse(url=f"{ru}{sep}google_business=error&reason={urllib.parse.quote(msg)}")
            except Exception:
                pass
        raise HTTPException(status_code=400, detail=msg)

    if err:
        desc = params.get("error_description") or err
        return _fail_redirect(desc)

    if not code or ":" not in state:
        raise HTTPException(status_code=400, detail="Missing code or state")

    company_id, b64_url = state.split(":", 1)
    try:
        return_url = base64.urlsafe_b64decode(b64_url.encode()).decode()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid state")

    try:
        tokens = _exchange_code_for_tokens(code)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e)) from e

    refresh = tokens.get("refresh_token")
    if not refresh:
        sep = "&" if "?" in return_url else "?"
        fail = f"{return_url}{sep}google_business=missing_refresh"
        return RedirectResponse(url=fail)

    supabase = get_supabase()
    supabase.table("company_google_business_oauth").upsert(
        {
            "company_id": company_id,
            "refresh_token": refresh,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
    ).execute()

    sep = "&" if "?" in return_url else "?"
    ok = f"{return_url}{sep}google_business=connected"
    return RedirectResponse(url=ok)


class GoogleBusinessStatusResponse(BaseModel):
    connected: bool


@router.get("/status/{company_id}", response_model=GoogleBusinessStatusResponse)
def google_business_status(company_id: str):
    supabase = get_supabase()
    resp = (
        supabase.table("company_google_business_oauth")
        .select("company_id")
        .eq("company_id", company_id)
        .maybe_single()
        .execute()
    )
    if resp is None:
        return GoogleBusinessStatusResponse(connected=False)
    data = getattr(resp, "data", None)
    return GoogleBusinessStatusResponse(connected=bool(data))


class DisconnectRequest(BaseModel):
    company_id: str


@router.post("/disconnect")
def disconnect_google_business(body: DisconnectRequest):
    supabase = get_supabase()
    supabase.table("company_google_business_oauth").delete().eq("company_id", body.company_id).execute()
    return {"ok": True}


class PostGoogleReviewReplyRequest(BaseModel):
    company_id: str
    profile_id: str
    rating: int = Field(ge=1, le=5)
    author_name: str = ""
    review_text: str = ""
    """Customer review body as shown in HCF (for matching)."""
    review_unix_ts: Optional[int] = None
    """Seconds since epoch for the review (Google Places `time`)."""
    reply_text: str = Field(..., min_length=1)


def _list_accounts(access_token: str) -> list[dict]:
    r = requests.get(
        f"{ACCOUNT_MGMT}/accounts",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=60,
    )
    if not r.ok:
        raise HTTPException(status_code=502, detail=f"List accounts failed: {r.text}")
    return r.json().get("accounts") or []


def _list_locations(access_token: str, account_name: str) -> list[dict]:
    """account_name like accounts/123456789"""
    url = f"{BUSINESS_INFO}/{account_name}/locations"
    r = requests.get(
        url,
        headers={"Authorization": f"Bearer {access_token}"},
        params={"readMask": "name,title,metadata,storefrontAddress"},
        timeout=60,
    )
    if not r.ok:
        raise HTTPException(status_code=502, detail=f"List locations failed: {r.text}")
    return r.json().get("locations") or []


def _list_reviews_v4(access_token: str, parent_accounts_locations: str) -> dict:
    """parent_accounts_locations: accounts/{aid}/locations/{lid} — paginated."""
    url = f"{MYBUSINESS_V4}/{parent_accounts_locations}/reviews"
    all_reviews: list = []
    page_token: Optional[str] = None
    for _ in range(20):
        params: dict[str, Any] = {"pageSize": 100}
        if page_token:
            params["pageToken"] = page_token
        r = requests.get(
            url,
            headers={"Authorization": f"Bearer {access_token}"},
            params=params,
            timeout=60,
        )
        if not r.ok:
            return {"reviews": [], "_error": r.text}
        data = r.json()
        all_reviews.extend(data.get("reviews") or [])
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return {"reviews": all_reviews}


def _put_reply_v4(access_token: str, review_name: str, comment: str) -> None:
    url = f"{MYBUSINESS_V4}/{review_name}/reply"
    r = requests.put(
        url,
        headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
        json={"comment": comment},
        timeout=60,
    )
    if not r.ok:
        raise HTTPException(status_code=502, detail=f"Post reply failed: {r.text}")


@router.post("/post-reply")
def post_google_review_reply(body: PostGoogleReviewReplyRequest):
    """
    Match the review in Google Business Profile and post the reply.
    Matching uses rating, author, text prefix, and optional unix timestamp.
    """
    if len(body.reply_text) > 4000:
        raise HTTPException(status_code=400, detail="Reply too long.")

    supabase = get_supabase()
    oauth = (
        supabase.table("company_google_business_oauth")
        .select("refresh_token")
        .eq("company_id", body.company_id)
        .maybe_single()
        .execute()
    )
    oauth_data = getattr(oauth, "data", None) if oauth is not None else None
    if not oauth_data:
        raise HTTPException(status_code=400, detail="Google Business not connected for this company.")

    prof = (
        supabase.table("profiles")
        .select("id, company_id, profile_type, google_place_id")
        .eq("id", body.profile_id)
        .single()
        .execute()
    )
    prof_data = getattr(prof, "data", None) if prof is not None else None
    if not prof_data or prof_data.get("company_id") != body.company_id:
        raise HTTPException(status_code=404, detail="Profile not found for company.")
    if prof_data.get("profile_type") != "google":
        raise HTTPException(status_code=400, detail="In-app reply is only supported for Google profiles in v2.")

    place_hint = (prof_data.get("google_place_id") or "").strip()

    tok = _refresh_access_token(oauth_data["refresh_token"])
    access = tok["access_token"]

    accounts = _list_accounts(access)
    if not accounts:
        raise HTTPException(status_code=400, detail="No Google Business accounts returned for this login.")

    target_ts = float(body.review_unix_ts) if body.review_unix_ts else None

    for acc in accounts:
        acc_name = acc.get("name")
        if not acc_name:
            continue
        locations = _list_locations(access, acc_name)
        if not locations:
            continue

        # Prefer a single-location account; else try place id substring match in JSON blob
        loc_candidates = locations
        if place_hint and len(locations) > 1:
            filtered = []
            for loc in locations:
                blob = json.dumps(loc).lower()
                if place_hint.lower() in blob:
                    filtered.append(loc)
            if filtered:
                loc_candidates = filtered

        for loc in loc_candidates:
            loc_name = loc.get("name")
            if not loc_name:
                continue
            payload = _list_reviews_v4(access, loc_name)
            if payload.get("_error"):
                continue
            match = _find_matching_review(
                payload,
                body.rating,
                body.author_name,
                body.review_text,
                target_ts,
            )
            if match:
                _put_reply_v4(access, match, body.reply_text.strip())
                return {"ok": True, "review_name": match}

    raise HTTPException(
        status_code=404,
        detail="Could not match this review in Google Business Profile. Check account, location, or reply manually.",
    )


class PostPlatformReplyRequest(BaseModel):
    """v3+ placeholder: extend per platform when APIs exist."""
    source: str
    company_id: str


@router.post("/post-reply-platform")
def post_platform_reply_placeholder(body: PostPlatformReplyRequest):
    if body.source == "google":
        raise HTTPException(status_code=400, detail="Use /post-reply for Google.")
    raise HTTPException(
        status_code=501,
        detail=f"In-app posting for source '{body.source}' is not available yet.",
    )
