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
from bs4 import BeautifulSoup
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
        "Use ONLY the FAQ provided below to answer questions.\n"
        "Do NOT invent new times, dates, or information.\n\n"
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
            timeout=15,
        )

        try:
            print("[ai_fallback] raw:", res.text[:2000])
        except:
            pass

        result = res.json()
        content = result.get("choices", [{}])[0].get("message", {}).get("content")

        # ⚠ Ensure always string
        if not content or not content.strip():
            return "Xin lỗi, tôi không thể trả lời câu hỏi này."
        return content.strip()
    except Exception as e:
        print("AI error:", e)
        traceback.print_exc()
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
- Do NOT add greetings, spam, personal data, emoji if unnecessary or jokes.
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
    Extract the first valid JSON object from text.
    """
    import json

    start = text.find("{")
    if start == -1:
        return None

    # tìm dấu ngoặc đóng phù hợp
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                candidate = text[start:i+1]
                try:
                    return json.loads(candidate)
                except:
                    # thử sửa lỗi trích dẫn
                    try:
                        candidate_fixed = candidate.replace("'", "\"")
                        return json.loads(candidate_fixed)
                    except:
                        return None

    return None

# -------------------------
# Safe insert with check
# -------------------------
def auto_insert_faq(q, a):
    """
    Robust insert with verbose debug. Returns dict:
    { success: bool, data:..., error: "text" }
    """
    if not q or not a:
        print("[auto_insert_faq] Empty question or answer; skipping insert.")
        return {"success": False, "error": "empty"}

    try:
        # Attempt insert
        res = supabase.table("faq").insert({"question": q, "answer": a}).execute()

        # Many supabase python clients return an object with .data and .error
        data = getattr(res, "data", None)
        error = getattr(res, "error", None)

        # If res is a dict (some clients)
        if isinstance(res, dict):
            data = res.get("data", None)
            error = res.get("error", None)
            # Some libs return status
            status = res.get("status_code") or res.get("status")
        else:
            # Try extract status_code / text if available
            status = getattr(res, "status_code", None)
            text = getattr(res, "text", None)

        # Log verbose
        print("[auto_insert_faq] raw_response:", {"data": data, "error": error, "status": status})

        if error:
            return {"success": False, "error": str(error)}

        # If data empty or None, still consider printing for debugging
        return {"success": True, "data": data}
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
def clean_model_output(text):
    if not text:
        return text
    remove_list = ["<s>", "</s>", "[OUT]", "[OUT] ", "[INST]", "[/INST]"]
    for token in remove_list:
        text = text.replace(token, "")
    return text.strip()

def faq_exists(question):
    question_clean = question.strip().lower()
    try:
        rows = supabase.table("faq").select("question").execute().data
        for row in rows:
            if row["question"].strip().lower() == question_clean:
                return True
    except Exception as e:
        print("[faq_exists] error:", e)
    return False

def search_faq(query):
    query_clean = re.sub(r"[^\w\s]", "", query.lower()).strip()
    if not query_clean:
        return None

    rows = supabase.table("faq").select("*").execute().data
    if not rows:
        return None

    # ✅ Exact match ưu tiên
    for row in rows:
        if row["question"].strip().lower() == query_clean:
            return row["answer"]

    # Fallback overlap matching
    query_words = set(query_clean.split())
    best_match = None
    highest_overlap = 0

    for row in rows:
        faq_clean = re.sub(r"[^\w\s]", "", row["question"].lower())
        faq_words = set(faq_clean.split())
        total_words = len(faq_words)
        if total_words == 0:
            continue
        overlap = len(query_words & faq_words)
        ratio = overlap / total_words
        if ratio > highest_overlap and ratio >= 0.7:
            highest_overlap = ratio
            best_match = row["answer"]

    return best_match

# -------------------------
# API: CHAT (updated)
# -------------------------
def fetch_website_text(url="https://libraryweb-ntw0ixtoo-shikuzenas-projects.vercel.app/"):
    """
    Fetch and extract visible text from the website.
    """
    try:
        res = requests.get(url, timeout=10)
        if res.status_code != 200:
            return None

        soup = BeautifulSoup(res.text, "lxml")

        # remove scripts, styles
        for s in soup(["script", "style", "noscript"]):
            s.decompose()

        text = soup.get_text(separator="\n", strip=True)
        # optional: limit length
        return text[:3500]  # prevent huge text
    except Exception as e:
        print("[fetch_website_text] error:", e)
        return None


def is_website_related(msg):
    """
    Detect if the user query is related to the website content.
    You can expand keywords here.
    """
    keywords = [
        "thư viện", "library", "mượn sách", "giờ mở cửa", 
        "sách", "tài liệu", "trang web", "website"
    ]
    msg_lower = msg.lower()
    return any(k in msg_lower for k in keywords)


@app.route("/api/chat", methods=["POST"])
def chat():
    data = request.json
    msg = data.get("message")
    session_id = data.get("session_id")

    if not msg:
        return jsonify({"error": "message is required"}), 400

    answer = None

    # 1️⃣ Try search FAQ first
    answer = search_faq(msg)
    answer = clean_model_output(answer) if answer else None

    # 2️⃣ If FAQ empty, check website
    if not answer and is_website_related(msg):
        website_text = fetch_website_text()
        if website_text:
            # Ask AI to generate answer based on website content
            answer = ai_fallback(
                f"Use ONLY the following website content to answer the question:\n\n{website_text}\n\nQuestion: {msg}"
            )
            answer = clean_model_output(answer)

    # 3️⃣ If still no answer, AI fallback without website
    if not answer or not answer.strip():
        answer = ai_fallback(msg)
        answer = clean_model_output(answer) or "Xin lỗi, tôi không thể trả lời câu hỏi này."

    # 4️⃣ Save chat history
    save_history(session_id, msg, answer)

    # 5️⃣ SELF-LEARNING — generate new FAQ
    try:
        gen = ai_generate_new_faq(msg, answer)
        print("[auto-learning raw]", gen)

        if isinstance(gen, dict):
            required = ["is_new_faq", "question", "answer"]
            if all(k in gen for k in required):
                question = clean_model_output(gen.get("question", "").strip())
                faq_answer = clean_model_output(gen.get("answer", "").strip())
                if gen.get("is_new_faq") and question and faq_answer:
                    similar = search_faq(question)
                    if not similar:
                        insert_result = auto_insert_faq(question, faq_answer)
                        print("[FAQ INSERT RESULT]", insert_result)

    except Exception as e:
        print("[auto-learning-error]", e)
        traceback.print_exc()

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
