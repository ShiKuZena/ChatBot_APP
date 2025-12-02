# ---------------------------------------------------
# IMPORTS
# ---------------------------------------------------
from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
import re
import json
import time
import traceback

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
    
# -------------------------
# Robust FAQ generator
# -------------------------
def ai_generate_new_faq(user_msg, bot_answer, max_tries=2):
    """
    Ask OpenRouter to decide whether to make a new FAQ.
    This function is robust to model text (tries to extract JSON),
    logs raw responses for debugging, and returns a dict with keys:
    { "is_new_faq": bool, "question": str, "answer": str, "raw": str }
    """
    prompt = f"""
User asked: {user_msg}
Bot answered: {bot_answer}

Decide if this should be added as a new FAQ entry.

RULES:
- Only add if the question is useful for many users.
- Do NOT add greetings, spam, personal data, or jokes.
- Keep the answer short (1-2 sentences).
- Output JSON only (no extra commentary).

Return JSON exactly like:
{{"is_new_faq": true/false, "question": "clean question", "answer": "short answer"}}
"""

    for attempt in range(1, max_tries + 1):
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
                        {"role": "system", "content": "You generate structured JSON only."},
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": 0.0,
                    "max_tokens": 400,
                },
                timeout=15,
            )
        except Exception as e:
            print(f"[ai_generate_new_faq] Request error (attempt {attempt}):", e)
            traceback.print_exc()
            time.sleep(1)
            continue

        # Log status & raw text for debugging
        status = getattr(res, "status_code", None)
        text = ""
        try:
            text = res.text
        except:
            text = "<no text>"
        print(f"[ai_generate_new_faq] status={status} attempt={attempt} raw={text[:1000]}")

        # Try to parse JSON from the response in multiple ways
        parsed = None
        # 1) If API returned JSON structure
        try:
            j = res.json()
            # common path: choices[0].message.content
            content = j.get("choices", [{}])[0].get("message", {}).get("content")
            if content:
                parsed = try_load_json_from_text(content)
        except Exception as e:
            # not fatal; continue attempts to extract
            pass

        # 2) Fallback: try to extract JSON substring from raw text
        if parsed is None:
            parsed = try_load_json_from_text(text)

        if parsed is None:
            print("[ai_generate_new_faq] Failed to parse JSON from model response.")
            # If last attempt, return safe false with raw for inspection
            if attempt == max_tries:
                return {"is_new_faq": False, "question": "", "answer": "", "raw": text}
            time.sleep(0.7)
            continue

        # Ensure keys exist and clean strings
        is_new = bool(parsed.get("is_new_faq") or parsed.get("is_new"))
        q = parsed.get("question") or parsed.get("q") or ""
        a = parsed.get("answer") or parsed.get("a") or ""

        # sanitize
        q = q.strip()
        a = a.strip()

        return {"is_new_faq": is_new, "question": q, "answer": a, "raw": text}

    # fallback
    return {"is_new_faq": False, "question": "", "answer": "", "raw": ""}


def try_load_json_from_text(text):
    """
    Try to find and load a JSON object inside `text`.
    Returns dict or None.
    """
    import re
    # find first {...} that looks like JSON
    matches = re.findall(r"\{(?:[^{}]|(?R))*\}", text, flags=re.DOTALL)
    for m in matches:
        try:
            return json.loads(m)
        except Exception:
            # try to fix common mistakes (single quotes -> double quotes)
            try:
                fixed = m.replace("'", "\"")
                return json.loads(fixed)
            except Exception:
                continue
    # final attempt: direct json.loads
    try:
        return json.loads(text)
    except Exception:
        return None


# -------------------------
# Safe insert with check
# -------------------------
def auto_insert_faq(q, a):
    """
    Insert but return the Supabase response (and print error if any).
    Make sure you're using a SUPABASE service_role key on the server.
    """
    if not q or not a:
        print("[auto_insert_faq] Empty question or answer; skipping insert.")
        return {"success": False, "error": "empty"}

    try:
        res = supabase.table("faq").insert({"question": q, "answer": a}).execute()
        # Some supabase client libs return a dict-like res with .data and .error
        # Print both for debugging.
        print("[auto_insert_faq] insert response:", getattr(res, "data", None), getattr(res, "error", None))
        # Interpret success
        if hasattr(res, "error") and res.error:
            return {"success": False, "error": res.error}
        # If using a dict return:
        if isinstance(res, dict):
            if res.get("error"):
                return {"success": False, "error": res.get("error")}
            return {"success": True, "data": res.get("data")}
        return {"success": True, "data": getattr(res, "data", None)}
    except Exception as e:
        print("[auto_insert_faq] exception:", e)
        traceback.print_exc()
        return {"success": False, "error": str(e)}
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
