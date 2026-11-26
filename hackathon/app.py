import base64
import hashlib
import hmac
import json
import logging
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request, url_for

load_dotenv(override=True)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ====================== 設定（都可以用 .env 覆蓋） ======================
DB_PATH = Path(os.getenv("DATABASE_PATH", "data.db"))
CHANNEL_SECRET = os.getenv("CHANNEL_SECRET", "")
CHANNEL_ACCESS_TOKEN = os.getenv("CHANNEL_ACCESS_TOKEN", "")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://localhost:5000").rstrip("/")
AI_API_KEY = os.getenv("CGU_API_KEY") or os.getenv("OPENAI_API_KEY", "")
AI_BASE_URL = os.getenv("AI_BASE_URL", "https://air.cgu.edu.tw/cgullmapi/v1").rstrip("/")
AI_MODEL = os.getenv("AI_MODEL", "gpt-oss-120b")

MIN_CONFIDENCE = float(os.getenv("MIN_CONFIDENCE", "0.7"))      # AI 信心低於這個值就不記錄
REPLY_IN_GROUP = os.getenv("REPLY_IN_GROUP", "0") == "1"        # 群組裡要不要回覆（預設安靜）
PHOTO_WINDOW = int(os.getenv("PHOTO_WINDOW_SECONDS", "30"))     # 文字和照片需在 30 秒內配對
MAX_PHOTOS = 5                                                  # 每筆失物最多幾張照片
MAX_PHOTO_BYTES = 10 * 1024 * 1024
UPLOAD_DIR = Path(app.root_path) / "static" / "uploads"

# 這些網址可以直接換成學校或你們自己的網站。
LINKS = {
    "圖書館": os.getenv("LIBRARY_URL", "http://163.25.115.140:8089/find/index.php"),
    "學務處": os.getenv("STUDENT_AFFAIRS_URL", "https://www4.is.cgu.edu.tw/lostfound/std/lostpost.aspx"),
    # 沒有另外指定時，使用目前 Bot 的公開網站根網址。
    "我們的網頁": os.getenv("OUR_SITE_URL", PUBLIC_BASE_URL),
}
LINK_ICONS = {"圖書館": "📚", "學務處": "🏫", "我們的網頁": "🌐"}
MENU_TITLE = "校園資訊 Link Tree"

# 這些指令直接回覆 Link Tree 卡片。
MENU_COMMANDS = {"/失物招領", "失物招領", "/連結", "連結", "/選單", "選單"}

# 粗篩：沒有包含這些字的訊息，連 AI 都不會呼叫，也不會被記錄
PREFILTER = (
    "掉", "落在", "掉落", "撿", "拾", "遺", "不見", "找不到", "找不著",
    "丟", "失物", "忘在", "忘了拿", "忘記拿", "遺留", "少了", "認領",
)
LOST_KEYWORDS = (
    "遺失", "遺落", "遺留", "不見", "找不到", "找不著", "掉了", "掉在", "掉到",
    "掉落", "落在", "丟了", "丟失", "忘在", "忘了拿", "忘記拿", "少了",
)
FOUND_KEYWORDS = (
    "拾獲", "拾到", "撿到", "撿拾", "撿獲", "發現失物", "發現有人遺失",
    "看到失物", "看到有人掉",
)

# 物品或描述含這些字，照片不公開（證件、卡片有個資）
SENSITIVE_WORDS = ("證", "卡", "護照", "存摺")

# ============================ 資料庫 ============================
def db():
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    with db() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                group_id TEXT,
                photo TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS lost_found_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id INTEGER,
                post_id INTEGER,
                report_type TEXT NOT NULL,
                content TEXT NOT NULL,
                details_json TEXT,
                group_id TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL
            );
            """
        )
        # 舊版資料庫補欄位
        for statement in (
            "ALTER TABLE posts ADD COLUMN photo TEXT",
            "ALTER TABLE lost_found_reports ADD COLUMN post_id INTEGER",
            "ALTER TABLE lost_found_reports ADD COLUMN details_json TEXT",
        ):
            try:
                connection.execute(statement)
            except sqlite3.OperationalError:
                pass


# 啟動時印出設定檢查（只印長度，不印內容）：secret 應為 32、token 通常 150 以上。
logger.info(
    "設定檢查：CHANNEL_SECRET 長度=%d（應為 32）、前3碼=%s 後3碼=%s、CHANNEL_ACCESS_TOKEN 長度=%d、CGU_API_KEY %s",
    len(CHANNEL_SECRET), CHANNEL_SECRET[:3], CHANNEL_SECRET[-3:] if CHANNEL_SECRET else "",
    len(CHANNEL_ACCESS_TOKEN), "已設定" if AI_API_KEY else "未設定（改用關鍵字判斷）",
)
if len(CHANNEL_SECRET) != 32:
    logger.warning("CHANNEL_SECRET 長度不是 32：請檢查 .env 是否存檔、是否貼對（Basic settings 的 Channel secret）")

# 讓 gunicorn/waitress 等 WSGI server 匯入 app 時也會建立資料表。
init_db()


# ============================ 個資遮罩 ============================
PII_PATTERNS = (
    (re.compile(r"09\d{2}[- ]?\d{3}[- ]?\d{3}"), "[電話已隱藏]"),
    (re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+"), "[Email已隱藏]"),
    # 學號、身分證、卡號等：英文字母 + 6 位以上數字
    (re.compile(r"(?<![A-Za-z0-9])[A-Za-z]?\d{6,}(?![A-Za-z0-9])"), "[號碼已隱藏]"),
)


def mask_pii(text: str) -> str:
    for pattern, replacement in PII_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def is_sensitive(analysis: dict, text: str) -> bool:
    haystack = f"{analysis.get('item', '')}{analysis.get('features', '')}{text}"
    return any(word in haystack for word in SENSITIVE_WORDS)


# ============================ LINE API ============================
def verify_signature(body: bytes, signature: str) -> bool:
    if not CHANNEL_SECRET or not signature:
        return False
    digest = hmac.new(CHANNEL_SECRET.encode(), body, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode()
    return hmac.compare_digest(expected, signature)


def reply_messages(reply_token: str, messages: list):
    """呼叫 LINE Messaging API 回覆。失敗只記 log，不讓整個 webhook 掛掉。"""
    if not reply_token:
        return
    try:
        response = requests.post(
            "https://api.line.me/v2/bot/message/reply",
            headers={
                "Authorization": f"Bearer {CHANNEL_ACCESS_TOKEN}",
                "Content-Type": "application/json",
            },
            json={"replyToken": reply_token, "messages": messages},
            timeout=10,
        )
        if response.status_code != 200:
            logger.warning("LINE 回覆失敗 %s：%s", response.status_code, response.text)
    except requests.RequestException as exc:
        logger.warning("LINE 回覆失敗：%s", exc)


def reply_text(reply_token: str, text: str):
    reply_messages(reply_token, [{"type": "text", "text": text}])


def build_menu_flex(only: str | None = None) -> dict:
    """Link Tree：一張卡片，裡面有多顆連結按鈕。"""
    names = [only] if only in LINKS else list(LINKS)
    buttons = [
        {
            "type": "button",
            "style": "secondary",
            "margin": "sm",
            "action": {
                "type": "uri",
                "label": f"{LINK_ICONS.get(name, '🔗')} {name}"[:20],
                "uri": LINKS[name],
            },
        }
        for name in names
        if LINKS.get(name)
    ]
    return {
        "type": "flex",
        "altText": MENU_TITLE,
        "contents": {
            "type": "bubble",
            "header": {
                "type": "box",
                "layout": "vertical",
                "contents": [
                    {"type": "text", "text": MENU_TITLE, "weight": "bold", "size": "lg", "align": "center"}
                ],
            },
            "body": {"type": "box", "layout": "vertical", "spacing": "sm", "contents": buttons},
        },
    }


def download_line_image(message_id: str):
    """向 LINE 下載使用者傳的圖片，回傳 (bytes, 副檔名)；失敗回傳 (None, None)。"""
    try:
        response = requests.get(
            f"https://api-data.line.me/v2/bot/message/{message_id}/content",
            headers={"Authorization": f"Bearer {CHANNEL_ACCESS_TOKEN}"},
            timeout=15,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("下載圖片失敗：%s", exc)
        return None, None
    content_type = response.headers.get("Content-Type", "").split(";")[0].strip().lower()
    extensions = {"image/jpeg": "jpg", "image/png": "png", "image/gif": "gif", "image/webp": "webp"}
    if content_type not in extensions or len(response.content) > MAX_PHOTO_BYTES:
        logger.warning("圖片格式或大小不符：%s，%s bytes", content_type, len(response.content))
        return None, None
    return response.content, extensions[content_type]


# ============================ AI 分析 ============================
def keyword_analyze_message(text: str) -> dict:
    """AI 尚未設定或暫時失敗時的備援分類。"""
    base = {"item": "", "color": "", "location": "", "date": "", "features": "",
            "confidence": 0.55, "source": "keyword"}
    # 「有人掉了／有人遺失」通常是旁人回報，表示說話者拾獲或發現物品。
    someone_lost = "有人" in text and any(keyword in text for keyword in LOST_KEYWORDS)
    if any(keyword in text for keyword in FOUND_KEYWORDS) or someone_lost:
        return {"type": "found", **base}
    if any(keyword in text for keyword in LOST_KEYWORDS):
        return {"type": "lost", **base}
    return {"type": "general", **base}


def ai_analyze_message(text: str) -> dict | None:
    """使用 CGU LLM Gateway 將自然語言整理成固定 JSON。"""
    if not AI_API_KEY:
        return None
    system_prompt = """你是校園失物系統的訊息分類器。只輸出一個合法 JSON 物件，不要 Markdown，不要解釋。
type 只能是 lost、found 或 general：lost 是使用者遺失物，found 是使用者拾獲物，general 是一般聊天。
請理解同義說法，例如不見了、掉了、找不到、遺失、有人撿到、撿到、拾獲。
「忘在某處」、「遺留在某處」、「落在某處」、「掉在某處」都算 lost；
「看到有人掉」、「發現某處有遺失物」都算 found。
欄位固定為：type、item、color、location、date、features、confidence。
找不到的欄位填空字串，confidence 是 0 到 1 的數字。"""
    try:
        response = requests.post(
            f"{AI_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {AI_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": AI_MODEL,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": text},
                ],
                "temperature": 0,
                "max_completion_tokens": 256,
            },
            timeout=10,
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"].strip()
        if content.startswith("```"):
            content = content.strip("`").replace("json", "", 1).strip()
        result = json.loads(content)
        if result.get("type") not in {"lost", "found", "general"}:
            return None
        return {
            "type": result["type"],
            "item": str(result.get("item", "")),
            "color": str(result.get("color", "")),
            "location": str(result.get("location", "")),
            "date": str(result.get("date", "")),
            "features": str(result.get("features", "")),
            "confidence": float(result.get("confidence", 0)),
            "source": "ai",
        }
    except (requests.RequestException, KeyError, IndexError, TypeError, ValueError) as exc:
        logger.warning("AI 分析失敗，改用關鍵字備援：%s", exc)
        return None


def analyze_message(text: str) -> dict:
    keyword_result = keyword_analyze_message(text)
    ai_result = ai_analyze_message(text)
    # AI 偶爾會把「有人水壺掉在教室」判成一般聊天；
    # 只要關鍵字已明確表示遺失或拾獲，就採用關鍵字備援結果。
    if (
        keyword_result["type"] == "found"
        or
        ai_result is None
        or (
            keyword_result["type"] != "general"
            and (
                ai_result["type"] == "general"
                or float(ai_result.get("confidence", 0)) < MIN_CONFIDENCE
            )
        )
    ):
        return keyword_result
    return ai_result


def classify_message(text: str) -> str:
    """保留簡單介面供其他程式使用。"""
    return analyze_message(text)["type"]


# ============================ 記錄失物 ============================
def build_content(analysis: dict, text: str) -> str:
    """網站上顯示的內容：優先用 AI 抽出的欄位；沒有的話才用遮罩後的原文。不會公開完整原文。"""
    details = "、".join(
        f"{label}：{mask_pii(analysis[key])}"
        for label, key in (("物品", "item"), ("顏色", "color"), ("地點", "location"),
                           ("日期", "date"), ("特徵", "features"))
        if analysis.get(key)
    )
    return details or mask_pii(text)[:120]


def record_report(analysis: dict, text: str) -> int:
    """寫入資料庫（不存發話者 ID、不存群組 ID），回傳貼文編號。"""
    title = "拾獲物（待確認）" if analysis["type"] == "found" else "遺失物（待確認）"
    content = build_content(analysis, text)
    safe_analysis = {k: (mask_pii(v) if isinstance(v, str) else v) for k, v in analysis.items()}
    created_at = now_iso()
    with db() as connection:
        cursor = connection.execute(
            "INSERT INTO posts(title, content, group_id, photo, created_at) VALUES (?, ?, NULL, NULL, ?)",
            (title, content, created_at),
        )
        post_id = cursor.lastrowid
        connection.execute(
            "INSERT INTO lost_found_reports(post_id, report_type, content, details_json, group_id, status, created_at) "
            "VALUES (?, ?, ?, ?, NULL, 'pending', ?)",
            (post_id, analysis["type"], content, json.dumps(safe_analysis, ensure_ascii=False), created_at),
        )
    return post_id


# ============================ 照片配對 ============================
# 只放在記憶體：文字和照片在 PHOTO_WINDOW 秒內互相配對。
# 因此支援「先文字後照片」和「先照片後文字」兩種順序。
# 不寫進資料庫。（若用多個 worker 部署，這份暫存不會共用，請只開一個 worker。）
PENDING: dict = {}
PENDING_LOCK = threading.Lock()


def source_key(event: dict) -> str:
    source = event.get("source", {})
    place = source.get("groupId") or source.get("roomId") or ""
    return f"{place}:{source.get('userId', '')}"


def remember_for_photo(key: str, post_id: int, sensitive: bool):
    now = time.time()
    with PENDING_LOCK:
        for expired in [k for k, v in PENDING.items() if v["expires"] < now]:
            for filename in PENDING[expired].get("photos", []):
                (UPLOAD_DIR / filename).unlink(missing_ok=True)
            del PENDING[expired]
        previous = PENDING.get(key, {})
        PENDING[key] = {
            "post_id": post_id,
            "sensitive": sensitive,
            "expires": now + PHOTO_WINDOW,
            "count": previous.get("count", 0),
            "noted": previous.get("noted", False),
            "photos": previous.get("photos", []),
            "photo_prompted": previous.get("photo_prompted", False),
        }


def attach_pending_photos(key: str, post_id: int, sensitive: bool):
    """把先傳的照片掛到剛建立的貼文；敏感物品則刪除暫存照片。"""
    with PENDING_LOCK:
        pending = PENDING.get(key)
        filenames = list(pending.get("photos", [])) if pending else []
        if pending:
            pending["photos"] = []

    if not filenames:
        return
    if sensitive:
        for filename in filenames:
            (UPLOAD_DIR / filename).unlink(missing_ok=True)
        with db() as connection:
            connection.execute(
                "UPDATE posts SET content = content || ? WHERE id = ?",
                ("\n（附有照片，因可能含個資不公開，請洽失物處理單位）", post_id),
            )
        return

    final_names = []
    for filename in filenames:
        source = UPLOAD_DIR / filename
        if not source.exists():
            continue
        final_name = f"{post_id}_{filename.removeprefix('pending_')}"
        source.rename(UPLOAD_DIR / final_name)
        final_names.append(final_name)
    if final_names:
        photo_value = ",".join(final_names)
        with db() as connection:
            connection.execute(
                "UPDATE posts SET photo = CASE WHEN photo IS NULL OR photo = '' THEN ? ELSE photo || ',' || ? END WHERE id = ?",
                (photo_value, photo_value, post_id),
            )


def should_reply(event: dict) -> bool:
    return event.get("source", {}).get("type") == "user" or REPLY_IN_GROUP


def has_pending_photos(key: str) -> bool:
    with PENDING_LOCK:
        pending = PENDING.get(key)
        return bool(pending and pending.get("photos") and pending["expires"] >= time.time())


# ============================ 事件處理 ============================
def handle_text(event: dict, text: str):
    reply_token = event.get("replyToken")

    # 1) Link Tree 指令（完全相符）
    if text in MENU_COMMANDS:
        reply_messages(reply_token, [build_menu_flex()])
        return
    if text in LINKS:
        reply_messages(reply_token, [build_menu_flex(only=text)])
        return

    # 2) 粗篩：與失物無關的聊天直接略過，不呼叫 AI、不記錄。
    # 若前面已先傳照片，提示使用者補充「遺失」或「拾獲」，避免照片無法配對。
    if not any(keyword in text for keyword in PREFILTER):
        if has_pending_photos(source_key(event)) and should_reply(event):
            reply_text(reply_token, "已收到照片，請補充這是遺失物還是拾獲物，以及物品和地點。")
        return

    # 3) 判斷是遺失還是拾獲
    analysis = analyze_message(text)
    if analysis["type"] == "general":
        return
    if analysis["source"] == "ai" and analysis["confidence"] < MIN_CONFIDENCE:
        return

    post_id = record_report(analysis, text)
    key = source_key(event)
    sensitive = is_sensitive(analysis, text)
    remember_for_photo(key, post_id, sensitive)
    attach_pending_photos(key, post_id, sensitive)

    if should_reply(event):
        if analysis["type"] == "lost":
            reply_text(reply_token, f"已記錄遺失資訊，可到 {PUBLIC_BASE_URL}/lost-found 查看。")
        else:
            reply_text(reply_token, "已記錄拾獲物，資料會以待確認狀態更新到網站。")


def handle_image(event: dict, message: dict):
    # 只處理 LINE 自己託管的圖片
    if message.get("contentProvider", {}).get("type", "line") != "line":
        return
    key = source_key(event)
    now = time.time()
    with PENDING_LOCK:
        pending = PENDING.get(key)
        if pending and pending["expires"] < now:
            for filename in pending.get("photos", []):
                (UPLOAD_DIR / filename).unlink(missing_ok=True)
            del PENDING[key]
            pending = None
        if not pending:
            # 先收到照片時先暫存，等 PHOTO_WINDOW 秒內的文字描述再配對。
            pending = {
                "post_id": None, "sensitive": False, "expires": now + PHOTO_WINDOW,
                "count": 0, "noted": False, "photos": [], "photo_prompted": False,
            }
            PENDING[key] = pending
        post_id = pending.get("post_id")
        if pending["sensitive"]:
            first_note = not pending["noted"]
            pending["noted"] = True
            download = False
        else:
            if len(pending.get("photos", [])) >= MAX_PHOTOS:
                return
            pending["count"] += 1
            first_note = False
            download = True

    if not download:
        if first_note:
            with db() as connection:
                connection.execute(
                    "UPDATE posts SET content = content || ? WHERE id = ?",
                    ("\n（附有照片，因可能含個資不公開，請洽失物處理單位）", post_id),
                )
        return

    data, extension = download_line_image(message["id"])
    if not data:
        return
    if post_id is None:
        key_hash = hashlib.sha256(key.encode()).hexdigest()[:12]
        filename = f"pending_{key_hash}_{message['id']}.{extension}"
    else:
        filename = f"{post_id}_{message['id']}.{extension}"
    (UPLOAD_DIR / filename).write_bytes(data)
    if post_id is None:
        with PENDING_LOCK:
            pending = PENDING.get(key)
            prompt = bool(pending and not pending.get("photo_prompted", False))
            if pending:
                pending["photos"].append(filename)
                pending["photo_prompted"] = True
        # 群組裡可能有大量一般照片，先不回覆以免洗版；私訊只提醒一次。
        if prompt and event.get("source", {}).get("type") == "user":
            reply_text(event.get("replyToken"), "已收到照片，請再傳送遺失或拾獲的文字描述。")
    else:
        with db() as connection:
            connection.execute(
                "UPDATE posts SET photo = CASE WHEN photo IS NULL OR photo = '' THEN ? ELSE photo || ',' || ? END WHERE id = ?",
                (filename, filename, post_id),
            )
        if event.get("source", {}).get("type") == "user":
            reply_text(event.get("replyToken"), "照片已加到網站。")


def handle_event(event: dict):
    if event.get("deliveryContext", {}).get("isRedelivery"):
        return                                  # LINE 重送的事件，避免重複記錄
    if event.get("type") != "message":
        return
    message = event.get("message", {})
    if message.get("type") == "text":
        handle_text(event, message.get("text", "").strip())
    elif message.get("type") == "image":
        handle_image(event, message)


def process_events(events: list):
    for event in events:
        try:
            handle_event(event)
        except Exception:
            logger.exception("處理事件失敗")


# ============================ 網頁與 API ============================
@app.get("/health")
def health():
    """不洩漏金鑰的啟動檢查端點，方便確認公開網址與部署狀態。"""
    return jsonify({
        "ok": True,
        "channel_secret_configured": bool(CHANNEL_SECRET),
        "channel_access_token_configured": bool(CHANNEL_ACCESS_TOKEN),
        "ai_configured": bool(AI_API_KEY),
    })


def post_to_dict(row, absolute: bool = False) -> dict:
    data = dict(row)
    created_at = data.get("created_at", "")
    try:
        parsed = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        data["display_time"] = parsed.astimezone().strftime("%Y/%m/%d %H:%M")
    except (TypeError, ValueError):
        data["display_time"] = created_at
    data["kind"] = data.get("report_type") or ("found" if "拾獲" in data.get("title", "") else "lost")
    names = [n for n in (data.get("photo") or "").split(",") if n]
    data["photos"] = [
        url_for("static", filename=f"uploads/{n}", _external=False) if not absolute
        else f"{PUBLIC_BASE_URL}/static/uploads/{n}"
        for n in names
    ]
    return data


@app.get("/")
def home():
    with db() as connection:
        posts = [
            post_to_dict(row)
            for row in connection.execute(
                "SELECT p.id, p.title, p.content, p.photo, p.created_at, "
                "COALESCE(r.report_type, CASE WHEN p.title LIKE '%拾獲%' THEN 'found' ELSE 'lost' END) AS report_type, "
                "COALESCE(r.status, 'pending') AS status "
                "FROM posts p LEFT JOIN lost_found_reports r ON r.post_id = p.id "
                "ORDER BY p.id DESC LIMIT 50"
            ).fetchall()
        ]
        reports = connection.execute(
            "SELECT report_type, content, status, created_at FROM lost_found_reports ORDER BY id DESC LIMIT 50"
        ).fetchall()
    return render_template("index.html", links=LINKS, posts=posts, reports=reports)


@app.get("/lost-found")
def lost_found_landing_page():
    """/失物招領 回傳的入口頁，概念上類似 Linktree。"""
    return render_template("lost_found.html", links=LINKS)


@app.get("/api/posts")
def api_posts():
    with db() as connection:
        rows = connection.execute(
            "SELECT id, title, content, photo, created_at FROM posts ORDER BY id DESC LIMIT 100"
        ).fetchall()
    return jsonify([post_to_dict(row, absolute=True) for row in rows])


@app.get("/api/reports")
def api_reports():
    with db() as connection:
        rows = connection.execute(
            "SELECT id, post_id, report_type, content, details_json, status, created_at "
            "FROM lost_found_reports ORDER BY id DESC LIMIT 100"
        ).fetchall()
    return jsonify([dict(row) for row in rows])


@app.post("/callback")
def callback():
    body = request.get_data()
    signature = request.headers.get("X-Line-Signature", "")
    if not verify_signature(body, signature):
        logger.warning(
            "簽章驗證失敗：程式載入的 CHANNEL_SECRET 長度=%d（應為 32），收到簽章=%s",
            len(CHANNEL_SECRET), "有" if signature else "沒有",
        )
        return "invalid signature", 400
    try:
        payload = json.loads(body.decode("utf-8"))
    except ValueError:
        return "bad request", 400
    events = payload.get("events", [])
    if events:
        # 先立刻回 200 給 LINE，AI 與下載照片在背景處理，避免 LINE 逾時重送
        threading.Thread(target=process_events, args=(events,), daemon=True).start()
    return "OK", 200


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
