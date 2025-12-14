import json
import os
import random
import sqlite3
import uuid
from pathlib import Path

import streamlit as st
from streamlit.errors import StreamlitSecretNotFoundError

# Cloud DB (Postgres) optional
try:
    import psycopg2
except Exception:
    psycopg2 = None


# --- 路徑 ---
APP_DIR = Path(__file__).parent
SQLITE_DB_PATH = APP_DIR / "quiz.db"
QUESTIONS_PATH = APP_DIR / "questions.json"


# -------------------------
# DB URL 讀取（修法 A：不讓 st.secrets 在本機爆炸）
# -------------------------
def get_db_url() -> str | None:
    # 1) 先看環境變數（本機 / CI / 部署都好用）
    env_url = os.getenv("DB_URL")
    if env_url:
        return env_url

    # 2) 再看 Streamlit secrets（沒有 secrets.toml 不要炸）
    try:
        return st.secrets.get("DB_URL", None)
    except StreamlitSecretNotFoundError:
        return None


def is_postgres_enabled() -> bool:
    return (get_db_url() is not None) and (psycopg2 is not None)


def get_conn():
    """
    回傳 (conn, db_type)
    db_type: "postgres" or "sqlite"
    """
    db_url = get_db_url()
    if db_url and psycopg2:
        conn = psycopg2.connect(db_url)
        return conn, "postgres"

    conn = sqlite3.connect(SQLITE_DB_PATH)
    return conn, "sqlite"


# -------------------------
# 使用者識別：每個人有自己的錯題本/進度
# -------------------------
def get_user_id() -> str:
    # 每個瀏覽器 session 一個 user_id
    if "user_id" not in st.session_state:
        st.session_state["user_id"] = str(uuid.uuid4())
    return st.session_state["user_id"]


# -------------------------
# Database
# -------------------------
def init_db():
    conn, db_type = get_conn()
    cur = conn.cursor()

    if db_type == "postgres":
        cur.execute("""
            CREATE TABLE IF NOT EXISTS attempts (
                user_id TEXT NOT NULL,
                qid TEXT NOT NULL,
                is_correct INTEGER NOT NULL,
                last_answer TEXT,
                correct_answer TEXT,
                updated_at TIMESTAMPTZ DEFAULT NOW(),
                PRIMARY KEY (user_id, qid)
            );
        """)
    else:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS attempts (
                user_id TEXT NOT NULL,
                qid TEXT NOT NULL,
                is_correct INTEGER NOT NULL,
                last_answer TEXT,
                correct_answer TEXT,
                updated_at TEXT,
                PRIMARY KEY (user_id, qid)
            )
        """)

    conn.commit()
    conn.close()


def load_attempts(user_id: str):
    init_db()
    conn, db_type = get_conn()
    cur = conn.cursor()

    if db_type == "postgres":
        cur.execute(
            "SELECT qid, is_correct, last_answer, correct_answer FROM attempts WHERE user_id=%s",
            (user_id,),
        )
        rows = cur.fetchall()
    else:
        rows = conn.execute(
            "SELECT qid, is_correct, last_answer, correct_answer FROM attempts WHERE user_id=?",
            (user_id,),
        ).fetchall()

    conn.close()
    return {r[0]: {"is_correct": int(r[1]), "last_answer": r[2], "correct_answer": r[3]} for r in rows}


def save_attempts_batch(user_id: str, results: list[dict]):
    """
    results: list of dict:
      {"qid": str, "is_correct": bool, "user_ans": str|None, "correct_ans": str}
    """
    if not results:
        return

    init_db()
    conn, db_type = get_conn()
    cur = conn.cursor()

    payload = [
        (user_id, r["qid"], int(bool(r["is_correct"])), r.get("user_ans"), r.get("correct_ans"))
        for r in results
    ]

    if db_type == "postgres":
        cur.executemany("""
            INSERT INTO attempts(user_id, qid, is_correct, last_answer, correct_answer)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (user_id, qid)
            DO UPDATE SET
                is_correct = EXCLUDED.is_correct,
                last_answer = EXCLUDED.last_answer,
                correct_answer = EXCLUDED.correct_answer,
                updated_at = NOW();
        """, payload)
    else:
        cur.executemany("""
            INSERT INTO attempts(user_id, qid, is_correct, last_answer, correct_answer, updated_at)
            VALUES(?,?,?,?,?, datetime('now'))
            ON CONFLICT(user_id, qid) DO UPDATE SET
                is_correct=excluded.is_correct,
                last_answer=excluded.last_answer,
                correct_answer=excluded.correct_answer,
                updated_at=datetime('now');
        """, payload)

    conn.commit()
    conn.close()


def reset_progress(user_id: str):
    init_db()
    conn, db_type = get_conn()
    cur = conn.cursor()

    if db_type == "postgres":
        cur.execute("DELETE FROM attempts WHERE user_id=%s", (user_id,))
    else:
        cur.execute("DELETE FROM attempts WHERE user_id=?", (user_id,))

    conn.commit()
    conn.close()


# -------------------------
# 題目載入（cache）
# -------------------------
@st.cache_data
def load_questions():
    if not QUESTIONS_PATH.exists():
        st.error(f"找不到檔案：{QUESTIONS_PATH}。請確認 questions.json 位於同一目錄。")
        return []

    with open(QUESTIONS_PATH, "r", encoding="utf-8") as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError:
            st.error("JSON 格式錯誤，無法解析。")
            return []

    if not isinstance(data, list) or len(data) == 0:
        st.error("JSON 必須是一個非空的列表 (List)。")
        return []

    normalized = []
    seen_ids = set()

    for i, q in enumerate(data):
        required_keys = ["id", "question", "options", "answer"]
        if not all(k in q for k in required_keys):
            st.warning(f"第 {i+1} 題資料不完整，跳過。")
            continue

        try:
            raw_id = int(q["id"])
        except Exception:
            st.warning(f"第 {i+1} 題 id 不是整數，跳過。")
            continue

        if raw_id in seen_ids:
            continue
        seen_ids.add(raw_id)

        options = q["options"]
        ans_idx = q["answer"]

        if not isinstance(options, list) or len(options) < 2:
            continue
        if not isinstance(ans_idx, int) or not (0 <= ans_idx < len(options)):
            continue

        normalized.append({
            "id": f"Q{raw_id:04d}",
            "question": str(q["question"]).strip(),
            "choices": [str(x).strip() for x in options],
            "answer": str(options[ans_idx]).strip(),
            # 兼容：你題庫以前常用 explain
            "explanation": str(q.get("explain", q.get("explanation", ""))).strip()
        })

    return normalized


# -------------------------
# 抽題邏輯
# -------------------------
def pick_questions(all_questions, attempts, n, avoid_seen=True, use_wrong_only=False):
    seen_ids = set(attempts.keys())
    wrong_ids = {qid for qid, v in attempts.items() if v["is_correct"] == 0}

    if use_wrong_only:
        pool = [q for q in all_questions if q["id"] in wrong_ids]
        if not pool:
            st.toast("太棒了！錯題本目前是空的 🎉")
    elif avoid_seen:
        pool = [q for q in all_questions if q["id"] not in seen_ids]
        if not pool:
            st.toast("所有題目都做完囉！可以考慮重置進度。")
    else:
        pool = list(all_questions)

    if not pool:
        return []

    n = min(int(n), len(pool))
    return random.sample(pool, n)


# -------------------------
# UI
# -------------------------
st.set_page_config(page_title="刷題神器", layout="centered")

user_id = get_user_id()

st.title("🔥 考試刷題神器")
st.caption("隨機抽題 ｜ 錯題本 ｜ 自動記錄進度 ｜（雲端 DB 可選）")

if is_postgres_enabled():
    st.success("✅ 已使用雲端資料庫（Postgres），進度不會因重啟而消失")
else:
    st.info("ℹ️ 目前使用本機 SQLite（quiz.db）。上線到雲端後可設定 DB_URL 以啟用 Postgres")

questions = load_questions()
if not questions:
    st.stop()

attempts = load_attempts(user_id)

total_q = len(questions)
done_q = len(attempts)
correct_q = sum(1 for v in attempts.values() if v["is_correct"] == 1)
accuracy = (correct_q / done_q * 100) if done_q > 0 else 0.0

with st.sidebar:
    st.header("📊 刷題狀態")
    st.write(f"總題庫：{total_q} 題")
    st.write(f"已完成：{done_q} 題")
    st.write(f"正確率：{accuracy:.1f}%")
    st.progress(min(done_q / total_q, 1.0))

    st.divider()
    st.header("⚙️ 抽題設定")

    max_n = min(100, total_q)
    n_input = st.number_input("本次題數", min_value=1, max_value=max_n, value=min(10, max_n), step=1)

    avoid_seen = st.checkbox("只出「沒做過」的題", value=True)
    wrong_only = st.checkbox("只出「錯題本」的題", value=False)

    if st.button("🚀 開始/重新抽題", use_container_width=True):
        picked = pick_questions(questions, attempts, n_input, avoid_seen, wrong_only)
        st.session_state["picked"] = picked
        st.rerun()

    st.divider()
    if st.button("🗑️ 重置我的進度", type="primary", use_container_width=True):
        reset_progress(user_id)
        st.session_state.pop("picked", None)
        # 同時把所有作答選擇清掉（避免殘留）
        keys_to_remove = [k for k in st.session_state.keys() if str(k).startswith("ans_")]
        for k in keys_to_remove:
            st.session_state.pop(k, None)
        st.rerun()


picked_qs = st.session_state.get("picked", [])
if not picked_qs:
    st.info("👈 請在左側點擊「開始/重新抽題」")
    st.stop()

with st.form("quiz_form"):
    st.subheader(f"本次練習：{len(picked_qs)} 題")

    for i, q in enumerate(picked_qs, start=1):
        st.markdown(f"**{i}. {q['question']}**")
        qid = q["id"]

        st.radio(
            "請選擇：",
            q["choices"],
            key=f"ans_{qid}",
            index=None,
            label_visibility="collapsed"
        )
        st.markdown("---")

    submitted = st.form_submit_button("📝 交卷", use_container_width=True)

if submitted:
    results_to_save = []
    score = 0
    wrong_list = []

    for q in picked_qs:
        qid = q["id"]
        user_ans = st.session_state.get(f"ans_{qid}")  # 可能是 None
        correct_ans = q["answer"]

        is_correct = (user_ans == correct_ans)
        if is_correct:
            score += 1
        else:
            wrong_list.append({"q": q, "user_ans": user_ans})

        results_to_save.append({
            "qid": qid,
            "is_correct": is_correct,
            "user_ans": user_ans,
            "correct_ans": correct_ans
        })

    save_attempts_batch(user_id, results_to_save)

    final_score = int(score / len(picked_qs) * 100)
    if final_score == 100:
        st.balloons()
        st.success(f"太強了！全對！得分：{final_score}")
    else:
        st.error(f"作答結束！得分：{final_score}（對 {score}/{len(picked_qs)} 題）")

    if wrong_list:
        st.subheader("❌ 錯題檢討")
        for item in wrong_list:
            q = item["q"]
            with st.expander(f"題目：{q['question']}", expanded=False):
                st.error(f"你的答案：{item['user_ans']}")
                st.success(f"正確答案：{q['answer']}")
                if q.get("explanation"):
                    st.info(f"💡 解析：{q['explanation']}")
