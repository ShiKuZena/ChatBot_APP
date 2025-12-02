# ---------------------------------------------------
# IMPORTS
# ---------------------------------------------------
from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
import re
import json

from supabase import create_client
from config import SUPABASE_URL, SUPABASE_KEY, OPENROUTER_API_KEY, MODEL

# ---------------------------------------------------
# INIT SUPABASE
# ---------------------------------------------------
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

app = Flask(__name__)
CORS(app)


# ---------------------------------------------------
# LOAD FAQ FOR AI PROMPT
# ---------------------------------------------------
def load_faq_for_ai():
    try:
        res = supabase.table("faq").select("question, answer").execute()
        rows = res.data
    except Exception as e:
        return ""

    faq_text = ""

    for row in rows:
        faq_text += f"Q: {row['question']}\nA: {row['answer']}\n\n"

    return faq_text.strip()


# ---------------------------------------------------
# SEARCH FAQ
# ---------------------------------------------------
def search_faq(query):
    query_clean = re.sub(r"[^\w\s]", "", query.lower())
    query_words = set(query_clean.split())

    if not query_words:
        return None

    rows = supabase.table("faq").select("*").execute().data

    best_match = None
    highest_overlap = 0

    for row in rows:
        faq_clean = re.sub(r"[^\w\s]", "", row["question"].lower())
        faq_words = set(faq_clean.split())

        overlap = len(query_words & faq_words)
        total_words = len(faq_words)
        if total_words == 0:
            continue

        ratio = overlap / total_words

        if ratio > highest_overlap and ratio >= 0.7:
            highest_overlap = ratio
            best_match = row["answer"]

    return best_match


# ---------------------------------------------------
# AI FALLBACK (OpenRouter)
# ---------------------------------------------------
def ai_fallback(user_message):
    faq_data = load_faq_for_ai()

    system_text = (
        "You are a helpful Library Assistant.\n"
        "When answering, USE the FAQ when possible.\n\n"
        f"{faq_data}"
    )

    try:
        res = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": MODEL,
                "messages": [
                    {"role": "system", "content": system_text},
                    {"role": "user", "content": user_message},
                ],
            },
        )

        result = res.json()
        return result["choices"][0]["message"]["content"]

    except Exception as e:
        print("AI error:", e)
        return "Xin lỗi, tôi không thể trả lời câu hỏi này."
    
# ---------------------------------------------------
# SELF-LEARNING: Generate new FAQ
# ---------------------------------------------------
def ai_generate_new_faq(user_msg, bot_answer):
    try:
        prompt = f"""
User asked: {user_msg}
Bot answered: {bot_answer}

Decide if this should be added as a new FAQ entry.

RULES:
- Only add if the question is useful for many users.
- No spam, personal info, greetings, jokes.
- Keep answer short and factual.
- Output ONLY JSON.

Return JSON exactly like:
{{
  "is_new_faq": true/false,
  "question": "cleaned question",
  "answer": "clean, short answer"
}}
"""

        res = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": MODEL,
                "messages": [
                    {"role": "system", "content": "You generate structured JSON only."},
                    {"role": "user", "content": prompt},
                ],
            },
        )

        raw = res.json()["choices"][0]["message"]["content"]

        return json.loads(raw)

    except Exception as e:
        print("FAQ generation error:", e)
        return {"is_new_faq": False}


# ---------------------------------------------------
# AUTO INSERT FAQ
# ---------------------------------------------------
def auto_insert_faq(q, a):
    try:
        supabase.table("faq").insert({
            "question": q,
            "answer": a
        }).execute()
        print("AUTO FAQ: Added ->", q)
    except Exception as e:
        print("Auto insert FAQ error:", e)


# ---------------------------------------------------
# SAVE CHAT HISTORY
# ---------------------------------------------------
def save_history(session_id, user_msg, bot_reply):
    supabase.table("chat_history").insert({
        "session_id": session_id,
        "user_message": user_msg,
        "bot_reply": bot_reply
    }).execute()


# ---------------------------------------------------
# API: CHAT
# ---------------------------------------------------
@app.route("/api/chat", methods=["POST"])
def chat():
    data = request.json
    msg = data.get("message")
    session_id = data.get("session_id")

    if not msg:
        return jsonify({"error": "message is required"}), 400

    answer = search_faq(msg)

    if not answer:
        answer = ai_fallback(msg)

    save_history(session_id, msg, answer)

    return jsonify({"reply": answer})


# ---------------------------------------------------
# ADMIN: ADD FAQ
# ---------------------------------------------------
@app.route("/api/admin/add_faq", methods=["POST"])
def add_faq():
    data = request.json
    q = data.get("question")
    a = data.get("answer")

    supabase.table("faq").insert({"question": q, "answer": a}).execute()

    return jsonify({"status": "success"})


# ---------------------------------------------------
# UPDATE FAQ
# ---------------------------------------------------
@app.route("/api/admin/update_faq/<int:id>", methods=["PUT"])
def update_faq(id):
    data = request.json

    supabase.table("faq").update({
        "question": data.get("question"),
        "answer": data.get("answer"),
    }).eq("id", id).execute()

    return jsonify({"status": "success"})


# ---------------------------------------------------
# DELETE FAQ
# ---------------------------------------------------
@app.route("/api/admin/delete_faq/<int:id>", methods=["DELETE"])
def delete_faq(id):
    supabase.table("faq").delete().eq("id", id).execute()

    return jsonify({"status": "success"})


# ---------------------------------------------------
# ADMIN: GET FAQ LIST
# ---------------------------------------------------
@app.route("/api/admin/faq")
def admin_get_faq():
    rows = supabase.table("faq") \
        .select("*") \
        .order("id", desc=True) \
        .execute().data

    return jsonify(rows)


# ---------------------------------------------------
# ADMIN: GET CHAT HISTORY
# ---------------------------------------------------
@app.route("/api/admin/history")
def admin_history():
    rows = supabase.table("chat_history") \
        .select("*") \
        .order("id", desc=True) \
        .execute().data

    return jsonify(rows)


# ---------------------------------------------------
# RUN APP
# ---------------------------------------------------
if __name__ == "__main__":
    app.run(debug=True)
