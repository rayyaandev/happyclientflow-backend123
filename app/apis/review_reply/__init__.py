"""
AI-generated draft replies for public reviews (owner perspective).
Used by the dashboard Review reply dialog (MVP: draft + edit + copy).
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from typing import Literal, Optional
import databutton as db
from openai import OpenAI

router = APIRouter(prefix="/review-reply", tags=["review_reply"])

try:
    _openai = OpenAI(api_key=db.secrets.get("OPENAI_API_KEY"))
except Exception as e:
    print(f"[review_reply] OpenAI init error: {e}")
    _openai = None


class ReviewReplyDraftRequest(BaseModel):
    review_text: str = Field(..., description="The customer's public review text")
    rating: int = Field(..., ge=1, le=5)
    source: str = Field(
        ...,
        description="Platform key e.g. google, proven_expert, anwalt",
    )
    language: Literal["de", "en"] = "de"
    company_name: Optional[str] = None
    author_name: Optional[str] = None


class ReviewReplyDraftResponse(BaseModel):
    draft: str


@router.post("/draft", response_model=ReviewReplyDraftResponse)
def create_review_reply_draft(body: ReviewReplyDraftRequest):
    """
    Generate a short, professional, friendly owner reply suitable for public posting.
    """
    if not _openai:
        raise HTTPException(status_code=500, detail="AI is not configured (OPENAI_API_KEY).")

    review_excerpt = (body.review_text or "").strip()

    lang = body.language
    company = (body.company_name or "our business").strip()
    author = (body.author_name or "").strip()
    source_label = body.source.replace("_", " ")

    if lang == "de":
        system = (
            "Du schreibst öffentliche Antworten eines Unternehmens auf Kundenbewertungen. "
            "Ton: professionell, freundlich, dankbar. Keine Rechtfertigung bei Kritik; "
            "kurz anerkennen und Lösung oder Gespräch anbieten. Keine erfundenen Fakten. "
            "Maximal etwa 1200 Zeichen. Verwende Sie-Form wo üblich. Keine Platzhalter wie [Name]."
        )
        user = (
            f"Unternehmen: {company}\n"
            f"Bewertungsplattform: {source_label}\n"
            f"Sterne (1–5): {body.rating}\n"
        )
        if author:
            user += f"Bewerter-Name (nur Kontext, nicht zwangsläufig ansprechen): {author}\n"
        if review_excerpt:
            user += f"Text der Bewertung:\n{review_excerpt}\n"
        else:
            user += (
                "Hinweis: Der Kunde hat keinen schriftlichen Kommentar hinterlassen (nur Sternebewertung).\n"
            )
        user += "\nSchreibe NUR den Antworttext, ohne Überschrift."
    else:
        system = (
            "You write public business replies to customer reviews. "
            "Tone: professional, friendly, grateful. For criticism, acknowledge briefly and offer to resolve "
            "or continue offline—no invented facts. About 800 characters max. No placeholders like [name]."
        )
        user = (
            f"Business: {company}\n"
            f"Platform: {source_label}\n"
            f"Star rating (1–5): {body.rating}\n"
        )
        if author:
            user += f"Reviewer name (context only): {author}\n"
        if review_excerpt:
            user += f"Review text:\n{review_excerpt}\n"
        else:
            user += "Note: The customer left no written comment (star rating only).\n"
        user += "\nReply with the response body only, no heading."

    try:
        completion = _openai.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.55,
            max_tokens=500,
        )
        text = (completion.choices[0].message.content or "").strip()
        if not text:
            raise HTTPException(status_code=500, detail="Empty AI response.")
        return ReviewReplyDraftResponse(draft=text)
    except HTTPException:
        raise
    except Exception as e:
        print(f"[review_reply] OpenAI error: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to generate draft: {e!s}") from e
