"""
This API module is responsible for generating AI-powered Google reviews based on client feedback.

It provides an endpoint that receives feedback details, constructs a sophisticated prompt for the OpenAI API,
and returns a generated review text. This is used in the final step of the positive
feedback flow on the frontend to assist clients in writing their reviews.

The system uses a two-part approach:
1. Prompt (rules and logic) - defines how reviews must be written
2. Prompt Components (variation library) - provides randomized text pools and style presets
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
import databutton as db
from openai import OpenAI
import os
import random
import re

# Initialize OpenAI client
try:
    client = OpenAI(api_key=db.secrets.get("OPENAI_API_KEY"))
except Exception as e:
    print(f"Error initializing OpenAI client: {e}")
    client = None

router = APIRouter()

# --- Sophisticated Prompt Components ---
PROMPT_COMPONENTS = {
    "lengthDefaults": {
        "kurz": [1, 2],
        "mittel": [3, 6],
        "lang": [6, 9]
    },

    "stylePresets": {
        "praegnant_sachlich": {
            "transition_prob": 0.25,
            "emoji_prob": 0.00,
            "imperfection_prob": 0.05,
            "softcritique_prob": 0.10,
            "anchor_weights": {
                "contact_person": 0.30, "highlight": 0.30, "reason": 0.20, "feeling": 0.10, "neutral": 0.10
            }
        },
        "herzlich_persoenlich": {
            "transition_prob": 0.45,
            "emoji_prob": 0.05,
            "imperfection_prob": 0.08,
            "softcritique_prob": 0.15,
            "anchor_weights": {
                "contact_person": 0.30, "highlight": 0.25, "reason": 0.15, "feeling": 0.25, "neutral": 0.05
            }
        },
        "kompetent_vertrauensvoll": {
            "transition_prob": 0.40,
            "emoji_prob": 0.02,
            "imperfection_prob": 0.05,
            "softcritique_prob": 0.10,
            "anchor_weights": {
                "contact_person": 0.25, "highlight": 0.30, "reason": 0.25, "feeling": 0.10, "neutral": 0.10
            }
        },
        "begeistert_lebendig": {
            "transition_prob": 0.50,
            "emoji_prob": 0.10,
            "imperfection_prob": 0.10,
            "softcritique_prob": 0.10,
            "anchor_weights": {
                "contact_person": 0.20, "highlight": 0.40, "reason": 0.15, "feeling": 0.20, "neutral": 0.05
            }
        },
        "ruhig_klar": {
            "transition_prob": 0.30,
            "emoji_prob": 0.00,
            "imperfection_prob": 0.04,
            "softcritique_prob": 0.08,
            "anchor_weights": {
                "contact_person": 0.30, "highlight": 0.25, "reason": 0.25, "feeling": 0.10, "neutral": 0.10
            }
        },
        "strukturiert_pointiert": {
            "transition_prob": 0.35,
            "emoji_prob": 0.01,
            "imperfection_prob": 0.03,
            "softcritique_prob": 0.08,
            "anchor_weights": {
                "contact_person": 0.30, "highlight": 0.30, "reason": 0.20, "feeling": 0.10, "neutral": 0.10
            }
        }
    },

    "openingExamples": {
        "contact_person": [
            "Herr {name} hat mich von Anfang an hervorragend begleitet",
            "Frau {name} war mein Ansprechpartner und hat schnell Klarheit geschaffen"
        ],
        "highlight": [
            "Besonders geholfen hat mir {highlight}",
            "Was mich überzeugt hat: {highlight}"
        ],
        "reason": [
            "Ich habe mich wegen {reason} an die Kanzlei gewandt",
            "Anlass war {reason}"
        ],
        "feeling": [
            "Ich fühlte mich durchweg {feeling}",
            "Von Anfang an {feeling}"
        ],
        "neutral": [
            "Sehr gute Erfahrung",
            "Alles in allem eine runde Sache"
        ]
    },

    "transitions": ["Außerdem", "Zudem", "Besonders", "Darüber hinaus", "Nicht zuletzt", "Zusätzlich"],
    "closings": ["kann ich nur empfehlen", "jederzeit wieder", "war die richtige Wahl", "bin sehr zufrieden", "würde ich jederzeit weiterempfehlen"],
    "softCritique": [
        "Die Antwort hätte stellenweise etwas schneller sein können, insgesamt aber top",
        "Kleine Rückfragen wurden zügig geklärt – unterm Strich sehr positiv"
    ],
    "emojiSet": ["😊", "😉", "👍"]
}


class GenerateReviewRequest(BaseModel):
    """
    Defines the expected input for the AI review generation endpoint.
    All fields from the feedback form are included to provide context to the AI.
    """
    collaboration_reason: str
    contact_person: str = ""
    collaboration_feeling: str
    highlight: str
    satisfaction: int
    recommendation: str # 'ja', 'nein', 'vielleicht'
    customer_uuid: str
    length: str = "mittel"  # 'kurz', 'mittel', 'lang'
    existing_reviews: list[str] = []  # List of existing review texts for uniqueness check

class GenerateReviewResponse(BaseModel):
    """
    Defines the output of the AI review generation endpoint.
    """
    generated_review: str

def get_style_preset_from_uuid(uuid: str) -> str:
    """Map UUID last digit to style preset"""
    last_digit = int(uuid[-1]) if uuid and uuid[-1].isdigit() else 0
    style_map = {
        0: "praegnant_sachlich", 1: "praegnant_sachlich",
        2: "herzlich_persoenlich", 3: "herzlich_persoenlich", 
        4: "kompetent_vertrauensvoll", 5: "kompetent_vertrauensvoll",
        6: "begeistert_lebendig", 7: "begeistert_lebendig",
        8: "ruhig_klar",
        9: "strukturiert_pointiert"
    }
    return style_map[last_digit]

def resolve_contact_person_display(contact_person: str) -> str:
    """Resolve contact person to proper display format"""
    if not contact_person or contact_person.lower() in ["jemand anderes", "weiß nicht", "someone else", "don't know"]:
        return ""
    
    # Simple gender inference by common German first names
    male_names = ["alexander", "andreas", "christian", "daniel", "david", "frank", "jan", "jens", "jörg", "kai", "klaus", "lars", "marc", "marco", "markus", "martin", "matthias", "michael", "oliver", "patrick", "peter", "ralf", "robert", "stefan", "stephan", "thomas", "thorsten", "tim", "tobias", "uwe", "wolfgang"]
    female_names = ["alexandra", "andrea", "angela", "anke", "anna", "antje", "barbara", "birgit", "brigitte", "christina", "christine", "claudia", "daniela", "doris", "eva", "gabriele", "heike", "ines", "jana", "julia", "karin", "katja", "katrin", "kerstin", "kirsten", "manuela", "maria", "marion", "martina", "melanie", "monika", "nadine", "nicole", "petra", "sabine", "sandra", "silke", "simone", "stefanie", "susanne", "tanja", "ute"]
    
    parts = contact_person.strip().split()
    if len(parts) >= 2:
        first_name = parts[0].lower()
        last_name = " ".join(parts[1:])
        
        if first_name in male_names:
            return f"Herr {last_name}"
        elif first_name in female_names:
            return f"Frau {last_name}"
        else:
            return contact_person  # Full name if gender unclear
    
    return contact_person

def weighted_random_choice(weights: dict) -> str:
    """Choose randomly based on weights"""
    choices = list(weights.keys())
    probabilities = list(weights.values())
    return random.choices(choices, weights=probabilities)[0]

def format_existing_reviews(reviews: list[str]) -> str:
    """Format existing reviews for the prompt"""
    if not reviews:
        return ""
    
    # Filter out empty reviews and limit to avoid token overflow
    valid_reviews = [r.strip() for r in reviews if r and r.strip()]
    if not valid_reviews:
        return ""
    
    # Format each review with a number
    formatted = []
    for i, review in enumerate(valid_reviews, 1):
        # Truncate very long reviews to save tokens
        truncated = review[:500] + "..." if len(review) > 500 else review
        formatted.append(f"{i}. \"{truncated}\"")
    
    return "\n".join(formatted)

@router.post(
    "/generate-review",
    response_model=GenerateReviewResponse,
    summary="Generate AI-powered Google Review",
    description="Receives client feedback and uses sophisticated AI prompt system to generate natural German reviews."
)
def generate_ai_review(
    request: GenerateReviewRequest,
):
    """
    This endpoint takes structured client feedback and uses the sophisticated AI prompt system
    to generate natural German Google reviews with style variation and randomization.

    The system uses UUID-based style selection and weighted randomization to ensure
    each review feels unique and natural while following strict quality rules.

    Args:
        request: A GenerateReviewRequest object containing all feedback details.

    Returns:
        A GenerateReviewResponse object with the generated review text.
        
    Raises:
        HTTPException: If the OpenAI client is not available or if the API call fails.
    """
    if not client:
        raise HTTPException(status_code=500, detail="OpenAI client is not configured. Please check API key.")

    # --- Step 1: Pick style preset from UUID ---
    style_preset_name = get_style_preset_from_uuid(request.customer_uuid)
    style_preset = PROMPT_COMPONENTS["stylePresets"][style_preset_name]
    
    # --- Step 2: Resolve contact person display ---
    display_contact = resolve_contact_person_display(request.contact_person)
    
    # --- Step 3: Apply randomization based on style preset ---
    use_transition = random.random() < style_preset["transition_prob"]
    use_emoji = random.random() < style_preset["emoji_prob"]
    use_imperfection = random.random() < style_preset["imperfection_prob"]
    use_soft_critique = random.random() < style_preset["softcritique_prob"]
    
    # Choose opening anchor
    anchor = weighted_random_choice(style_preset["anchor_weights"])
    
    # Select random elements if needed
    transition = random.choice(PROMPT_COMPONENTS["transitions"]) if use_transition else ""
    emoji = random.choice(PROMPT_COMPONENTS["emojiSet"]) if use_emoji else ""
    soft_critique_text = random.choice(PROMPT_COMPONENTS["softCritique"]) if use_soft_critique else ""
    
    # --- Step 4: Format existing reviews for uniqueness check ---
    existing_reviews_text = format_existing_reviews(request.existing_reviews)
    has_existing_reviews = bool(existing_reviews_text)
    
    # --- Step 5: Construct the sophisticated German prompt ---
    prompt = f"""AUFGABE
Erstelle eine natürliche Google-Bewertung auf Deutsch in ausschließlicher Ich-Perspektive.
Gib NUR den Bewertungstext zurück – keine Einleitung, keine Labels.

EINGABEN
grund_der_zusammenarbeit: {request.collaboration_reason}
ansprechpartner: {display_contact}
gefuehl_waehrend_der_zusammenarbeit: {request.collaboration_feeling}
highlight: {request.highlight}
zufriedenheit_von_5: {request.satisfaction}
wuerde_empfehlen: {request.recommendation}
uuid: {request.customer_uuid}
length: {request.length}

AUSGABE-BEDINGUNGEN
"wuerde_empfehlen" darf NUR erwähnt werden, wenn Wert = "ja". Bei "nein" oder "vielleicht": ignorieren.
"ansprechpartner" darf NUR verwendet werden, wenn ein konkreter Name angegeben ist.
Negative Details nur dezent und direkt positiv entkräften.

STILVARIATION (UUID-basiert: {style_preset_name})
Anchor-Fokus: {anchor}
Übergänge verwenden: {use_transition}
Emoji erlaubt: {use_emoji}
Kleine Unregelmäßigkeit: {use_imperfection}
Sanfte Kritik: {use_soft_critique}

TEXTREGELN
WICHTIG - PERSPEKTIVE: Die Bewertung wird von MIR als Kunde geschrieben. Ausschließlich Ich-Form verwenden!
❌ FALSCH: "Sie haben sich gewandt", "Man wurde beraten", "Der Kunde war zufrieden", "Es wurde geholfen"
✅ RICHTIG: "Ich habe mich gewandt", "Ich wurde beraten", "Ich war zufrieden", "Mir wurde geholfen"
Niemals dritte Person oder passive Formulierungen mit "man/sie/es" verwenden.
Eröffnung auf {anchor} fokussieren.
Eingaben integrieren: grund_der_zusammenarbeit optional, highlight konkret hervorheben, gefuehl_waehrend_der_zusammenarbeit subtil einbauen wenn positiv/neutral, ansprechpartner nach Regel nennen.
Zufriedenheit implizit ausdrücken („rundum zufrieden", „sehr gute Erfahrung"), keine Sterne nennen.
Satzlängen mischen, Redundanzen vermeiden.

LÄNGE
{request.length}: {PROMPT_COMPONENTS["lengthDefaults"][request.length][0]}–{PROMPT_COMPONENTS["lengthDefaults"][request.length][1]} Sätze

SPEZIELLE ANWEISUNGEN
{f"Verwende Übergang: {transition}" if use_transition else ""}
{f"Füge Emoji hinzu: {emoji}" if use_emoji else ""}
{f"Sanfte Kritik einbauen: {soft_critique_text}" if use_soft_critique else ""}
{f"Kleine Unregelmäßigkeit erlaubt (fehlender Punkt, verkürzter Satz)" if use_imperfection else ""}

{"EINZIGARTIGKEIT - KRITISCH WICHTIG" if has_existing_reviews else ""}
{f'''Hier sind alle bestehenden Bewertungen dieses Unternehmens:
---
{existing_reviews_text}
---

STRENGE REGELN FÜR EINZIGARTIGKEIT:
1. NIEMALS gleiche oder ähnliche Eröffnungssätze wie in bestehenden Bewertungen verwenden
2. NIEMALS gleiche Phrasen, Redewendungen oder Formulierungen kopieren oder paraphrasieren
3. ANDERE Satzstrukturen und Satzmuster verwenden als in den bestehenden Bewertungen
4. ANDERE Wörter und Synonyme wählen - wenn bestehende Bewertungen "kompetent" sagen, nutze z.B. "fachkundig" oder "versiert"
5. ANDEREN Fokus setzen - wenn bestehende Bewertungen den Service loben, betone z.B. die Kommunikation oder Erreichbarkeit
6. Die generierte Bewertung muss sich VOLLSTÄNDIG von allen obigen Bewertungen unterscheiden
7. Bei Ähnlichkeit zu einer bestehenden Bewertung: komplett neu formulieren''' if has_existing_reviews else ""}

DATENFEHLER
Fehlende Eingaben weglassen, ohne Platzhalter oder Entschuldigung.

AUSGABE
Nur den finalen Bewertungstext zurückgeben, ohne Labels, Metadaten oder Anführungszeichen."""

    try:
        # Build system message - add uniqueness instruction if existing reviews provided
        system_message = "Du bist ein Experte für natürliche deutsche Google-Bewertungen. Du schreibst authentische Bewertungen IMMER aus der Ich-Perspektive, als ob DU der Kunde bist. NIEMALS dritte Person (sie/man/er/es) verwenden! Befolge die Regeln exakt und variiere den Stil basierend auf den Vorgaben."
        
        if has_existing_reviews:
            system_message += " WICHTIG: Dir werden bestehende Bewertungen gezeigt. Deine generierte Bewertung MUSS sich vollständig davon unterscheiden - andere Eröffnung, andere Formulierungen, andere Struktur, andere Wortwahl. Keine Ähnlichkeiten erlaubt!"
        
        completion = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_message},
                {"role": "user", "content": prompt}
            ],
            temperature=0.9 if has_existing_reviews else 0.8,  # Higher temperature for more variation when avoiding duplicates
            max_tokens=300,
        )
        generated_text = completion.choices[0].message.content
        if not generated_text:
             raise HTTPException(status_code=500, detail="OpenAI returned an empty response.")
             
        # Clean up the response to ensure it's just the review
        cleaned_review = generated_text.strip().strip('"').strip("'")

        return GenerateReviewResponse(generated_review=cleaned_review)
    except Exception as e:
        print(f"An error occurred while calling OpenAI API: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to generate review. Error: {str(e)}")
