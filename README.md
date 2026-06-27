# Provenance Guard

A Flask API that classifies submitted text as human-written or AI-generated, surfaces a plain-language transparency label to readers, and gives creators a formal path to appeal. Built for creative platforms where a false positive — labeling a human's work as AI — is worse than a false negative.

---

## Architecture Overview

A submission enters via `POST /submit` and travels through the following components before returning a structured response:

```
[Creator] ──POST /submit──▶ [Input Validation]
                                    │
              ┌─────────────────────┴──────────────────────┐
              ▼                                             ▼
   [Signal 1: Groq LLM]                     [Signal 2: Stylometrics (TTR)]
   Semantic + holistic style                Vocabulary diversity math
   → float 0.0–1.0                          → float 0.0–1.0 (inverted for score)
              │                                             │
              └─────────────────────┬──────────────────────┘
                                    ▼
                       [Confidence Scoring Logic]
                       confidence = (groq × 0.70) + (inv_ttr × 0.30)
                                    │
                                    ▼
                       [Transparency Label Selection]
                       ≤0.35 → likely_human
                       0.36–0.75 → uncertain
                       ≥0.76 → likely_ai
                                    │
                          ┌─────────┴─────────┐
                          ▼                   ▼
                    [Audit Log]        [JSON Response]
                    (SQLite)           returned to caller
```

**Appeal flow:**

```
[Creator] ──POST /appeal──▶ [Lookup content_id]
                                    │
                       [Update status → under_review]
                                    │
                       [Write reasoning to Audit Log]
                                    │
                       [Return confirmation JSON]
```

---

## Detection Signals

### Signal 1 — Groq LLM (Llama-3.3-70b-versatile)

**What it measures:** Semantic coherence, narrative structure, and holistic prose style. The model reads the text the way a human editor would and assesses whether transitions, sentence rhythm, and vocabulary feel organically human or mechanically balanced.

**Output:** A float between `0.0` (definitely human) and `1.0` (definitely AI), returned as `ai_probability` in a JSON response.

**Why this signal:** LLMs are the only tools that can catch the "AI voice" — that particular blend of confident hedging, parallel list structure, and transitional phrases like "It is important to note" — that stylometric math can't detect.

**Blind spot:** Formal human writing (legal briefs, academic abstracts, grant proposals) naturally mimics AI polish. A PhD student's paper may score higher than a casual AI-generated blog post.

---

### Signal 2 — Stylometrics: Type-Token Ratio (TTR)

**What it measures:** Vocabulary diversity — the ratio of unique words to total words in a passage. AI models optimize for likely next tokens, which causes them to recycle common words more than a human naturally would.

**Formula:** `TTR = unique_word_count / total_word_count`

**Output:** A raw float `0.0–1.0` (higher = more diverse vocabulary = more human-like). This is **inverted** (`1.0 - TTR`) before entering the confidence formula so that "low diversity" → "higher AI probability."

**Why this signal:** It is fully deterministic, explainable, and structurally independent from the LLM signal. One signal is semantic; the other is mathematical. That independence makes their combination more informative than either alone.

**Blind spot:** Very short texts (fewer than ~20 tokens) have artificially high TTR regardless of authorship, because every word in a 10-word sentence is trivially unique. The code returns a neutral `0.5` for texts under 5 tokens to prevent skewing.

---

## Confidence Scoring

### Formula

```
confidence = (groq_score × 0.70) + (inverted_ttr × 0.30)
```

The LLM is weighted at 70% because TTR degrades on short texts and vocabulary-dense poetry regardless of authorship. The inverted TTR contributes 30% — enough to move borderline LLM scores in the right direction without dominating the result.

### What the score means

A score of `0.5` does **not** flip a binary flag. It signals genuine uncertainty and falls inside the wide "Uncertain" band. The system was designed to be conservative: the uncertain zone spans `0.36–0.75`, meaning a full **39-point range** produces no flag. This reflects the core design principle that a false positive on a human writer is worse than a false negative on AI content.

### Thresholds

| Range | Attribution | Meaning |
|-------|-------------|---------|
| 0.00 – 0.35 | `likely_human` | High confidence it's human-written |
| 0.36 – 0.75 | `uncertain` | Mixed signals; no flag applied |
| 0.76 – 1.00 | `likely_ai` | High confidence it's AI-generated |

### Example submissions with real scores

**Formal AI-sounding text — confidence: 0.6823**
```
Input: "Artificial intelligence represents a transformative paradigm shift in 
modern society. It is important to note that while the benefits of AI are 
numerous, it is equally essential to consider the ethical implications. 
Furthermore, stakeholders across various sectors must collaborate to ensure 
responsible deployment of these emerging technologies."

groq_score:   0.92
ttr_raw:      0.8723  (high vocabulary diversity)
ttr_inverted: 0.1277
confidence:   (0.92 × 0.70) + (0.1277 × 0.30) = 0.6823
attribution:  uncertain
```

Note: This text lands in "uncertain" rather than "likely_ai" because the high 
vocabulary diversity (ttr_raw 0.87) partially offsets the strong LLM signal. 
This is the system working as designed — it errs toward caution rather than 
confidently flagging borderline content.

**Casual human-written text — confidence: 0.07**
```
Input: "ok so i finally tried that ramen place downtown and honestly? 
underwhelming. way too much sodium."

groq_score:   0.10
ttr_raw:      1.0  (every word unique — short text)
ttr_inverted: 0.0
confidence:   (0.10 × 0.70) + (0.0 × 0.30) = 0.07
attribution:  likely_human
```

The two examples produce a confidence gap of **0.61**, confirming the scoring 
function produces meaningful variation across the range rather than clustering 
near 0.5.

---

## Transparency Labels

The label returned by the API changes based on the confidence score. All three variants are shown below with their exact text.

**High-confidence human (confidence ≤ 0.35)**
> ✅ Likely Human-Written. Our analysis signals indicate with high confidence that this content was organically written by a person. Vocabulary diversity and semantic style both align with human authorship.

**Uncertain (confidence 0.36 – 0.75)**
> ⚠️ Uncertain — Mixed Signals. Our system cannot confidently determine whether this content was written by a human or generated by AI. It shows characteristics of both. No flag has been applied. If you are the creator, no action is needed.

**High-confidence AI (confidence ≥ 0.76)**
> 🤖 Likely AI-Generated. Our detection signals indicate with high confidence that this content was primarily generated by artificial intelligence. If you believe this classification is incorrect, you may submit an appeal below.

---

## Appeals Workflow

Any creator can appeal a classification they believe is wrong. The system does **not** automatically reclassify — appeals are queued for a human reviewer.

**Endpoint:** `POST /appeal`

**What the creator provides:**
- `content_id` — the UUID returned by `/submit`
- `creator_reasoning` — a plain-text explanation of why they believe the flag is wrong

**What happens on appeal:**
1. The system looks up the `content_id` in the audit log.
2. The status is updated from `classified` → `under_review`.
3. The creator's reasoning is written into the `appeal_reasoning` column of the audit log.
4. A confirmation JSON is returned.

**What a human reviewer sees:**
When a moderator queries `GET /log`, they see the full entry: original text attribution, both signal scores, the combined confidence score, and the creator's written explanation — everything needed to make a manual decision.

---

## Rate Limiting

**Limit on `POST /submit`:** 5 requests per minute; 50 per day per IP address.

**Reasoning:**
- A real writer submitting their own work might post once or twice in a session — 5/minute is generous for legitimate use while stopping any scripted flood.
- The 50/day ceiling prevents an adversary from cycling through proxies across a full day to probe the model's decision boundaries.
- `/appeal` and `/log` are not rate-limited because they are read/update operations that a legitimate user shouldn't need to repeat rapidly.

**Evidence of rate limiting working** (12 rapid requests sent; first 5 return 200, remainder return 429 — confirming the 5/minute limit):

```
200
200
200
200
200
429
429
429
429
429
429
429
```

---

## Audit Log

Every classification decision and every appeal is written to a SQLite database (`audit_log.db`). The log is queryable via `GET /log`.

**Schema:**

| Column | Type | Description |
|--------|------|-------------|
| `content_id` | TEXT | UUID for this submission |
| `creator_id` | TEXT | Identifier provided by the creator |
| `timestamp` | TEXT | UTC ISO-8601 timestamp |
| `attribution` | TEXT | `likely_human`, `uncertain`, or `likely_ai` |
| `confidence` | REAL | Combined confidence score (0.0–1.0) |
| `groq_score` | REAL | Raw LLM signal output |
| `ttr_raw` | REAL | Raw vocabulary diversity ratio |
| `ttr_inverted` | REAL | `1.0 - ttr_raw` — value used in formula |
| `status` | TEXT | `classified` or `under_review` |
| `appeal_reasoning` | TEXT | Creator's appeal text (null if no appeal) |

**Sample log output (3 entries):**

```json
{
  "entries": [
    {
      "content_id": "c799287d-bf81-4cd1-bf23-3773319c8192",
      "creator_id": "demo-ai-user",
      "timestamp": "2026-06-26T15:49:30.000000+00:00",
      "attribution": "uncertain",
      "confidence": 0.6823,
      "groq_score": 0.92,
      "ttr_raw": 0.8723,
      "ttr_inverted": 0.1277,
      "status": "classified",
      "appeal_reasoning": null
    },
    {
      "content_id": "428e2e34-bbac-4c34-b8d2-cb4788fdfdcb",
      "creator_id": "demo-human-user",
      "timestamp": "2026-06-26T15:49:06.434907+00:00",
      "attribution": "likely_human",
      "confidence": 0.07,
      "groq_score": 0.1,
      "ttr_raw": 1.0,
      "ttr_inverted": 0.0,
      "status": "classified",
      "appeal_reasoning": null
    },
    {
      "content_id": "f4fcc817-575e-44e6-8fb4-3ce3e4289d9f",
      "creator_id": "ratelimit-test",
      "timestamp": "2026-06-26T15:47:54.040203+00:00",
      "attribution": "uncertain",
      "confidence": 0.56,
      "groq_score": 0.8,
      "ttr_raw": 1.0,
      "ttr_inverted": 0.0,
      "status": "under_review",
      "appeal_reasoning": "I wrote this myself. I am a non-native English speaker and my writing may appear formal."
    }
  ]
}
```

---

## Setup & Running Locally

**1. Install dependencies**
```bash
pip install -r requirements.txt
```

**2. Create a `.env` file** (never commit this)
```
GROQ_API_KEY=your_groq_api_key_here
```

**3. Run the server**
```bash
python app.py
```
The server runs at `http://127.0.0.1:5000`. Visit that URL in a browser or run `curl http://localhost:5000/` — you should see the API index JSON, confirming the server is up.

---

## API Reference

### `GET /`
Health check. Returns service name, version, and available endpoints.

---

### `POST /submit`
Submit content for attribution analysis.

**Rate limit:** 5 per minute / 50 per day

**Request body:**
```json
{
  "text": "The content to analyze...",
  "creator_id": "user-123"
}
```

**Response:**
```json
{
  "content_id": "uuid-here",
  "attribution": "likely_human",
  "confidence": 0.21,
  "label": "✅ Likely Human-Written. ...",
  "signals": {
    "groq_score": 0.23,
    "ttr_raw": 0.8276,
    "ttr_inverted": 0.1724
  }
}
```

**curl example:**
```bash
curl -s -X POST http://localhost:5000/submit \
  -H "Content-Type: application/json" \
  -d '{"text": "ok so i finally tried that ramen place downtown and honestly? underwhelming.", "creator_id": "user-1"}' \
  | python -m json.tool
```

---

### `POST /appeal`
Contest a classification.

**Request body:**
```json
{
  "content_id": "uuid-from-submit-response",
  "creator_reasoning": "I wrote this myself. I am a non-native English speaker."
}
```

**Response:**
```json
{
  "message": "Appeal submitted successfully. A human reviewer will assess your case.",
  "content_id": "uuid-here",
  "status": "under_review"
}
```

---

### `GET /log`
Returns all audit log entries, most recent first.

```bash
curl -s http://localhost:5000/log | python -m json.tool
```

---

## Known Limitations

**1. Short texts skew TTR artificially.** A haiku or three-sentence flash fiction piece has near-perfect vocabulary diversity by definition, because almost no word is repeated in so few tokens. This pulls the inverted TTR toward 0 (i.e., looks human) regardless of authorship. The code neutralizes this for texts under 5 tokens, but the issue persists for texts in the 5–30 word range. A proper production fix would use smoothed or log-corrected TTR variants like MATTR (Moving-Average TTR).

**2. Formal human writing gets misclassified by the LLM.** Grant applications, academic abstracts, legal motions, and structured reports score consistently high on the Groq signal because their intentional formality looks identical to the measured, hedged output of AI systems. This is the hardest false-positive case to solve because the distinguishing feature (emotional irregularity) is intentionally removed by the human author. The appeals workflow exists precisely for these creators.

---

## Spec Reflection

**One way the spec guided implementation:** Writing out the three label variants in `planning.md` before touching the code forced a concrete decision about thresholds. I had to choose "what does 0.76 feel like to a user?" before I could write any logic — which meant the thresholds in the code directly reflect a prior design decision rather than being reverse-engineered from a default `0.5` cutoff.

**One way implementation diverged from the spec:** The spec implied the TTR score logged to the audit table and the TTR value used in the confidence formula would naturally be the same field. During implementation I realized the formula uses the *inverted* TTR, not the raw value, so a moderator reading the log would see a number that is the opposite of what was actually calculated. The fix was to store both — `ttr_raw` and `ttr_inverted` — in the audit log, so the columns are self-documenting and the math is auditable.

---

## AI Usage

**Instance 1 — Flask skeleton + Groq signal function**
I provided the AI with my architecture diagram and the Signal 1 description from `planning.md` and asked it to generate the Flask app skeleton with a stubbed `/submit` route and the `analyze_with_groq` function. The generated function used `response.choices[0].message.content` without a length check, which would crash on an empty API response. I added the `try/except` fallback to `0.5` and the `max(0.0, min(1.0, score))` clamp to handle edge cases the generated code ignored.

**Instance 2 — Confidence scoring and label logic**
I provided my uncertainty representation section (thresholds + formula) and asked the AI to generate the `compute_confidence` and `generate_label` functions. The AI implemented the label thresholds correctly but used `>=0.5` as the uncertain/AI boundary instead of `>=0.76` as I had specified. I corrected the threshold and also split the label text into a separate function rather than inlining it in the route handler, which made the logic easier to test independently.