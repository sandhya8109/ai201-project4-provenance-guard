import string
import os
import uuid
import sqlite3
import json
from datetime import datetime, timezone
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from groq import Groq
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

# Load environment variables
load_dotenv()

app = Flask(__name__)

limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=[],           # No blanket defaults — limits are set per-route
    storage_uri="memory://"
)

groq_client = Groq()

# ---------------------------------------------------------------------------
# DATABASE
# ---------------------------------------------------------------------------
DB_FILE = "audit_log.db"

def init_db():
    """Creates the audit_log table if it doesn't already exist."""
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS audit_log (
                content_id       TEXT PRIMARY KEY,
                creator_id       TEXT,
                timestamp        TEXT,
                attribution      TEXT,
                confidence       REAL,
                groq_score       REAL,
                ttr_raw          REAL,   -- raw vocabulary diversity (higher = more diverse)
                ttr_inverted     REAL,   -- 1 - ttr_raw, used in confidence formula
                status           TEXT,
                appeal_reasoning TEXT
            )
        ''')
        conn.commit()

init_db()

# ---------------------------------------------------------------------------
# SIGNAL 1 — Groq LLM (semantic / holistic)
# ---------------------------------------------------------------------------
def analyze_with_groq(text: str) -> float:
    """
    Asks Llama-3 to assess whether the text reads as human- or AI-generated.
    Returns a float 0.0 (definitely human) → 1.0 (definitely AI).
    Falls back to 0.5 on any API error so the system degrades gracefully.
    """
    prompt = (
        "You are an expert stylometric AI detector. Analyze the following text and "
        "decide whether it reads as human-written or AI-generated.\n\n"
        "AI text tends to have: perfectly balanced sentence structures, predictable "
        "transitions (e.g. 'Furthermore', 'It is important to note'), and a lack of "
        "emotional quirks or organic tangents.\n"
        "Human text tends to have: varied pacing, idiosyncrasies, casual asides, "
        "and unexpected vocabulary choices.\n\n"
        "Return ONLY a JSON object with a single key \"ai_probability\" whose value "
        "is a float between 0.0 (Definitely Human) and 1.0 (Definitely AI).\n\n"
        f"Text to analyze:\n\"\"\"\n{text}\n\"\"\""
    )

    try:
        response = groq_client.chat.completions.create(
            messages=[
                {"role": "system", "content": "You output JSON only. No explanation."},
                {"role": "user",   "content": prompt}
            ],
            model="llama-3.3-70b-versatile",
            response_format={"type": "json_object"},
            temperature=0.0
        )
        result = json.loads(response.choices[0].message.content)
        score = float(result.get("ai_probability", 0.5))
        return max(0.0, min(1.0, score))   # clamp to [0, 1]

    except Exception as e:
        print(f"[Groq error] {e}")
        return 0.5


# ---------------------------------------------------------------------------
# SIGNAL 2 — Stylometrics: Type-Token Ratio (structural / statistical)
# ---------------------------------------------------------------------------
def calculate_ttr(text: str) -> float:
    """
    Type-Token Ratio = unique_words / total_words.
    Higher TTR → richer vocabulary → more likely human.
    Returns raw TTR (0.0–1.0). The caller inverts it for the confidence formula.

    Known limitation: very short texts (<20 tokens) produce unreliable TTR
    because the ratio is trivially high for any short sequence.
    """
    translator = str.maketrans('', '', string.punctuation)
    tokens = text.translate(translator).lower().split()

    if len(tokens) < 5:
        return 0.5   # not enough data — return neutral rather than skew score

    return len(set(tokens)) / len(tokens)


# ---------------------------------------------------------------------------
# CONFIDENCE SCORING
# ---------------------------------------------------------------------------
def compute_confidence(groq_score: float, ttr_raw: float) -> tuple[float, float]:
    """
    Combines the two signal scores into a single calibrated confidence value.

    Formula: confidence = (groq_score × 0.70) + (inverted_ttr × 0.30)

    LLM is weighted heavier because TTR degrades on short or vocabulary-dense
    texts regardless of authorship. The inverted TTR converts 'diversity'
    into 'AI-likeness' (low diversity → high AI probability).

    Returns (final_confidence, ttr_inverted).
    """
    ttr_inverted = 1.0 - ttr_raw
    confidence = (groq_score * 0.70) + (ttr_inverted * 0.30)
    return round(max(0.0, min(1.0, confidence)), 4), round(ttr_inverted, 4)


# ---------------------------------------------------------------------------
# TRANSPARENCY LABEL
# ---------------------------------------------------------------------------
def generate_label(confidence: float) -> tuple[str, str]:
    """
    Maps a confidence score to an attribution string and plain-language label.

    Thresholds (asymmetric — errs toward human to reduce false positives):
      0.00 – 0.35  →  likely_human
      0.36 – 0.75  →  uncertain
      0.76 – 1.00  →  likely_ai
    """
    if confidence <= 0.35:
        return (
            "likely_human",
            (
                "✅ Likely Human-Written. Our analysis signals indicate with high "
                "confidence that this content was organically written by a person. "
                "Vocabulary diversity and semantic style both align with human authorship."
            )
        )
    elif confidence <= 0.75:
        return (
            "uncertain",
            (
                "⚠️ Uncertain — Mixed Signals. Our system cannot confidently determine "
                "whether this content was written by a human or generated by AI. "
                "It shows characteristics of both. No flag has been applied. "
                "If you are the creator, no action is needed."
            )
        )
    else:
        return (
            "likely_ai",
            (
                "🤖 Likely AI-Generated. Our detection signals indicate with high "
                "confidence that this content was primarily generated by artificial "
                "intelligence. If you believe this classification is incorrect, "
                "you may submit an appeal below."
            )
        )


# ---------------------------------------------------------------------------
# ROUTES
# ---------------------------------------------------------------------------

@app.route("/", methods=["GET"])
def index():
    """Health check / API index — prevents 404 on root URL."""
    return jsonify({
        "service": "Provenance Guard",
        "version": "1.0.0",
        "status": "running",
        "endpoints": {
            "POST /submit": "Submit content for attribution analysis",
            "POST /appeal": "Contest a classification decision",
            "GET  /log":    "View the full audit log"
        }
    }), 200


@app.route("/submit", methods=["POST"])
@limiter.limit("5 per minute; 50 per day")
def submit_content():
    """
    Accepts a piece of text for attribution analysis.
    Runs both detection signals, computes confidence, selects label,
    writes to audit log, and returns a structured response.

    Body: { "text": "...", "creator_id": "..." }
    """
    data = request.get_json(silent=True)

    if not data or "text" not in data or "creator_id" not in data:
        return jsonify({"error": "Request body must include 'text' and 'creator_id'."}), 400

    text       = data["text"].strip()
    creator_id = data["creator_id"].strip()

    if not text:
        return jsonify({"error": "'text' cannot be empty."}), 400

    content_id = str(uuid.uuid4())
    timestamp  = datetime.now(timezone.utc).isoformat()

    # --- Run signals ---
    groq_score = analyze_with_groq(text)
    ttr_raw    = calculate_ttr(text)

    # --- Score & label ---
    confidence, ttr_inverted = compute_confidence(groq_score, ttr_raw)
    attribution, label       = generate_label(confidence)

    # --- Persist to audit log ---
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute('''
            INSERT INTO audit_log
              (content_id, creator_id, timestamp, attribution, confidence,
               groq_score, ttr_raw, ttr_inverted, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (content_id, creator_id, timestamp, attribution, confidence,
              groq_score, ttr_raw, ttr_inverted, "classified"))
        conn.commit()

    return jsonify({
        "content_id":  content_id,
        "attribution": attribution,
        "confidence":  confidence,
        "label":       label,
        "signals": {
            "groq_score":    round(groq_score, 4),
            "ttr_raw":       round(ttr_raw, 4),
            "ttr_inverted":  round(ttr_inverted, 4)
        }
    }), 200


@app.route("/appeal", methods=["POST"])
def submit_appeal():
    """
    Allows a creator to contest a classification.
    Updates status to 'under_review' and logs the creator's reasoning.

    Body: { "content_id": "...", "creator_reasoning": "..." }
    """
    data = request.get_json(silent=True)

    if not data or "content_id" not in data or "creator_reasoning" not in data:
        return jsonify({"error": "Request body must include 'content_id' and 'creator_reasoning'."}), 400

    content_id = data["content_id"].strip()
    reasoning  = data["creator_reasoning"].strip()

    if not reasoning:
        return jsonify({"error": "'creator_reasoning' cannot be empty."}), 400

    with sqlite3.connect(DB_FILE) as conn:
        row = conn.execute(
            'SELECT status FROM audit_log WHERE content_id = ?', (content_id,)
        ).fetchone()

        if not row:
            return jsonify({"error": f"No submission found for content_id '{content_id}'."}), 404

        if row[0] == "under_review":
            return jsonify({"message": "An appeal for this content is already under review.", "content_id": content_id}), 200

        conn.execute('''
            UPDATE audit_log
            SET status = 'under_review', appeal_reasoning = ?
            WHERE content_id = ?
        ''', (reasoning, content_id))
        conn.commit()

    return jsonify({
        "message":    "Appeal submitted successfully. A human reviewer will assess your case.",
        "content_id": content_id,
        "status":     "under_review"
    }), 200


@app.route("/log", methods=["GET"])
def get_log():
    """Returns all audit log entries, most recent first."""
    with sqlite3.connect(DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            'SELECT * FROM audit_log ORDER BY timestamp DESC'
        ).fetchall()

    return jsonify({"entries": [dict(r) for r in rows]}), 200


# ---------------------------------------------------------------------------
# RATE LIMIT ERROR HANDLER
# ---------------------------------------------------------------------------
@app.errorhandler(429)
def ratelimit_handler(e):
    return jsonify({
        "error": "Rate limit exceeded.",
        "detail": str(e.description),
        "retry_after": "Please wait before submitting again."
    }), 429


if __name__ == "__main__":
    app.run(debug=True)