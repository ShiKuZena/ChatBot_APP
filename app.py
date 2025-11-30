from flask import Flask, request, jsonify
from flask_cors import CORS
import mysql.connector
import requests
from config import DB_CONFIG, OPENROUTER_API_KEY, MODEL

app = Flask(__name__)
CORS(app)
# ------------------------
# DB Connect
# ------------------------
def db_connect():
    return mysql.connector.connect(**DB_CONFIG)


# ------------------------
# Load FAQ for AI training
# ------------------------
def load_faq_for_ai():
    conn = db_connect()
    cur = conn.cursor(dictionary=True)

    cur.execute("SELECT question, answer FROM faq")
    rows = cur.fetchall()

    conn.close()

    faq_text = ""
    for row in rows:
        faq_text += f"Q: {row['question']}\nA: {row['answer']}\n\n"

    return faq_text.strip()


# ------------------------
# FAQ Search
# ------------------------
def search_faq(query):
    import re

    # Normalize user query
    query_clean = re.sub(r"[^\w\s]", "", query.lower())
    query_words = set(query_clean.split())

    if not query_words:
        return f"Bạn muốn tìm cách nào cho: {query}?"

    conn = db_connect()
    cur = conn.cursor(dictionary=True)
    cur.execute("SELECT question, answer FROM faq")
    rows = cur.fetchall()
    conn.close()

    best_match = None
    highest_overlap = 0

    for row in rows:
        # Normalize FAQ question
        faq_clean = re.sub(r"[^\w\s]", "", row["question"].lower())
        faq_words = set(faq_clean.split())

        # Count overlapping words
        overlap = len(query_words & faq_words)
        total_words = len(faq_words)

        # Calculate overlap ratio
        ratio = overlap / total_words

        # Only consider as match if ratio >= threshold (strict)
        if ratio > highest_overlap and ratio >= 0.7:
            highest_overlap = ratio
            best_match = row["answer"]

    # Return FAQ answer if found, otherwise a friendly fallback
    if best_match:
        return best_match
    else:
        return f"Không gì tôi chưa hiểu câu hỏi: {query}?"


# ------------------------
# AI Fallback (with FAQ context)
# ------------------------
def ai_fallback(user_message):
    faq_data = load_faq_for_ai()
    system_text = (
        "You are a helpful Library Assistant.\n"
        "When answering, use the FAQ below when possible.\n\n"
        f"{faq_data}"
    )

    try:
        res = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": MODEL,
                "messages": [
                    {"role": "system", "content": system_text},
                    {"role": "user", "content": user_message},
                ]
            }
        )
        data = res.json()
        return data["choices"][0]["message"]["content"]

    except Exception:
        return "I'm not sure about that."


# ------------------------
# Save chat history
# ------------------------
def save_history(session_id, user_msg, bot_reply):
    conn = db_connect()
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO chat_history (session_id, user_message, bot_reply)
        VALUES (%s, %s, %s)
    """, (session_id, user_msg, bot_reply))

    conn.commit()
    conn.close()


# ------------------------
# API: Chat
# ------------------------
@app.route("/api/chat", methods=["POST"]) 
def chat(): 
    data = request.json 
    msg = data.get("message") 
    session_id = data.get("session_id") 
    
    answer = search_faq(msg) 
    if not answer: 
        answer = ai_fallback(msg)
         
    save_history(session_id, msg, answer) 
    return jsonify({"reply": answer})


# ------------------------
# Admin: Add FAQ
# ------------------------
@app.route("/api/admin/add_faq", methods=["POST"])
def add_faq():
    data = request.json
    q = data["question"]
    a = data["answer"]

    conn = db_connect()
    cur = conn.cursor()

    cur.execute("INSERT INTO faq (question, answer) VALUES (%s, %s)", (q, a))
    conn.commit()
    conn.close()

    return jsonify({"status": "success"})

# ------------------------
# Update FAQ
# ------------------------
@app.route("/api/admin/update_faq/<int:id>", methods=["PUT"])
def update_faq(id):
    data = request.json
    q = data.get("question")
    a = data.get("answer")

    conn = db_connect()
    cur = conn.cursor()
    cur.execute("UPDATE faq SET question=%s, answer=%s WHERE id=%s", (q, a, id))
    conn.commit()
    conn.close()

    return jsonify({"status": "success"})

# ------------------------
# Delete FAQ
# ------------------------
@app.route("/api/admin/delete_faq/<int:id>", methods=["DELETE"])
def delete_faq(id):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("DELETE FROM faq WHERE id=%s", (id,))
    conn.commit()
    conn.close()

    return jsonify({"status": "success"})

# ------------------------
# Admin: Get FAQ
# ------------------------
@app.route("/api/admin/faq")
def admin_get_faq():
    conn = db_connect()
    cur = conn.cursor(dictionary=True)

    cur.execute("SELECT * FROM faq ORDER BY id DESC")
    rows = cur.fetchall()
    conn.close()

    return jsonify(rows)


# ------------------------
# Admin: Chat History
# ------------------------
@app.route("/api/admin/history")
def admin_history():
    conn = db_connect()
    cur = conn.cursor(dictionary=True)

    cur.execute("SELECT * FROM chat_history ORDER BY id DESC")
    rows = cur.fetchall()

    conn.close()
    return jsonify(rows)


if __name__ == "__main__":
    app.run(debug=True)
