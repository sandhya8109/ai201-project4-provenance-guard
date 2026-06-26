# ai201-project4-provenance-guard# Provenance Guard

Provenance Guard is a Flask-based API designed to detect AI-generated text while minimizing false positives against human creators. It utilizes a dual-signal detection architecture, combining the semantic analysis of a Large Language Model (Groq / Llama-3) with pure mathematical stylometrics (Type-Token Ratio). 

## Features
* **Dual-Signal Detection:** Balances LLM "vibes" with objective vocabulary diversity math.
* **Confidence Scoring:** Safely maps combined scores to Transparency Labels (Likely Human, Uncertain, Likely AI).
* **Audit Logging:** Automatically logs all submissions to a local SQLite database.
* **Appeals Workflow:** Allows creators to contest "Likely AI" flags, updating the database status for human moderation.
* **Rate Limiting:** Protects endpoints from spam (5 requests per minute for submissions).

## Setup Instructions

1. **Install Dependencies:**
   Ensure you have Python installed, then install the required packages:
   ```bash
   pip install -r requirements.txt
 **  Environment Variables:
Create a .env file in the root directory and add your Groq API key:

Code snippet
GROQ_API_KEY=your_api_key_here

** Run the Server:
Start the Flask development server:
python app.py
The server will run on http://127.0.0.1:5000.

** API Endpoints
1. Submit Content
URL: /submit

Method: POST

Body:
```json
{
"text": "Your content here...",
"creator_id": "user-123"
}

Returns: content_id, attribution (likely_human, uncertain, likely_ai), confidence score, and a detailed transparency label.

2. Submit Appeal
URL: /appeal

Method: POST

Body:

JSON
{
  "content_id": "the-uuid-from-submission",
  "creator_reasoning": "Explanation of why the flag is incorrect."
}
Returns: Confirmation message and updates the database status to under_review.

3. View Audit Log
URL: /log

Method: GET

Returns: A complete JSON array of all past submissions and their current statuses.