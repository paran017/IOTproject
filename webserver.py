import json
import random
import time
import threading
import hashlib
import os
import smtplib
from email.mime.text import MIMEText
from functools import wraps

from flask import Flask, render_template, jsonify, request, redirect, url_for, session
import pymysql
import paho.mqtt.client as mqtt


# =========================================================
# 기본 설정
# =========================================================
MQTT_BROKER = "localhost"
MQTT_PORT = 1883
OTP_VALID_SECONDS = 180

# /var/www/html 안의 index.html, login.html을 사용
app = Flask(__name__, template_folder="/var/www/html")
app.secret_key = os.getenv("SAFE_SECRET_KEY", "iot-safe-secret-key")


# =========================================================
# MySQL 설정
# =========================================================
DB_CONFIG = {
    "host": "192.168.137.1",
    "user": "root",
    "password": "1234",
    "database": "iot_safe",
    "charset": "utf8mb4"
}


# =========================================================
# 이메일 알림 설정, 추후 추가 예정.
# 환경변수를 설정하지 않으면 이메일은 자동으로 비활성처럼 동작
# =========================================================
EMAIL_ENABLED = True
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587

EMAIL_USER = os.getenv("SAFE_EMAIL_USER")
EMAIL_PASS = os.getenv("SAFE_EMAIL_PASS")
EMAIL_TO = os.getenv("SAFE_ALERT_TO")

last_email_time = 0
EMAIL_COOLDOWN_SECONDS = 60


# =========================================================
# 웹 상태 저장
# =========================================================
state = {
    "safe_locked": True,
    "door_closed": True,
    "risk_score": 0,
    "risk_level": "NORMAL",
    "otp": None,
    "otp_expires_at": 0,
    "otp_remaining": 0,
    "otp_status": "NONE",
    "device_otp_status": "NONE",
    "last_auth": None,
    "last_status_time": "",
    "events": []
}


# =========================================================
# DB 로그인 함수(mysql)
# =========================================================
def get_db_connection():
    return pymysql.connect(
        host=DB_CONFIG["host"],
        user=DB_CONFIG["user"],
        password=DB_CONFIG["password"],
        database=DB_CONFIG["database"],
        charset=DB_CONFIG["charset"],
        cursorclass=pymysql.cursors.DictCursor
    )


def check_user_login(username, password):
    password_hash = hashlib.sha256(password.encode("utf-8")).hexdigest()

    conn = get_db_connection()

    try:
        with conn.cursor() as cursor:
            sql = """
                SELECT id, username
                FROM users
                WHERE username = %s AND password_hash = %s
            """
            cursor.execute(sql, (username, password_hash))
            user = cursor.fetchone()
            return user

    finally:
        conn.close()


def login_required(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        if not session.get("login_user"):
            if request.path.startswith("/api/"):
                return jsonify({"ok": False, "error": "login_required"}), 401
            return redirect(url_for("login"))
        return func(*args, **kwargs)

    return wrapper


# =========================================================
# 이메일 함수, 추후 사용예정. 이메일 만들고 쓸거같음. 아님말고
# =========================================================
def send_email_alert(subject, body):
    global last_email_time

    if not EMAIL_ENABLED:
        return

    if not EMAIL_USER or not EMAIL_PASS or not EMAIL_TO:
        print("[EMAIL] 이메일 환경변수가 설정되지 않아 발송하지 않습니다.")
        return

    now = time.time()

    if now - last_email_time < EMAIL_COOLDOWN_SECONDS:
        print("[EMAIL] 쿨다운 중이라 이메일을 보내지 않습니다.")
        return

    msg = MIMEText(body, _charset="utf-8")
    msg["Subject"] = subject
    msg["From"] = EMAIL_USER
    msg["To"] = EMAIL_TO

    try:
        server = smtplib.SMTP(SMTP_HOST, SMTP_PORT)
        server.starttls()
        server.login(EMAIL_USER, EMAIL_PASS)
        server.sendmail(EMAIL_USER, [EMAIL_TO], msg.as_string())
        server.quit()

        last_email_time = now
        print("[EMAIL] 경고 이메일 발송 완료")

    except Exception as e:
        print("[EMAIL ERROR]", e)


# =========================================================
# MQTT 함수
# =========================================================
def create_mqtt_client(client_id):
    try:
        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
    except Exception:
        return mqtt.Client(client_id=client_id)


mqtt_client = create_mqtt_client("iot_safe_web")


def add_event(event_type, message, risk_delta=0, risk_score=None, risk_level=None):
    event = {
        "event_type": event_type,
        "message": message,
        "risk_delta": risk_delta,
        "risk_score": risk_score if risk_score is not None else state.get("risk_score", 0),
        "risk_level": risk_level if risk_level is not None else state.get("risk_level", "NORMAL"),
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
    }

    state["events"].insert(0, event)
    state["events"] = state["events"][:30]


def publish(topic, payload):
    mqtt_client.publish(topic, json.dumps(payload, ensure_ascii=False))


def on_connect(client, userdata, flags, reason_code, properties=None):
    print("[MQTT] 웹 서버 연결 완료")

    client.subscribe("safe/status")
    client.subscribe("safe/event")
    client.subscribe("safe/auth/result")


def on_message(client, userdata, msg):
    topic = msg.topic
    payload_text = msg.payload.decode("utf-8")

    try:
        data = json.loads(payload_text)
    except Exception:
        data = {"raw": payload_text}

    print(f"[MQTT] {topic}: {data}")

    if topic == "safe/status":
        state["safe_locked"] = data.get("safe_locked", state["safe_locked"])
        state["door_closed"] = data.get("door_closed", state["door_closed"])
        state["risk_score"] = data.get("risk_score", state["risk_score"])
        state["risk_level"] = data.get("risk_level", state["risk_level"])
        state["device_otp_status"] = data.get("otp_status", "NONE")
        state["last_status_time"] = data.get("timestamp", "")

    elif topic == "safe/event":
        add_event(
            data.get("event_type", "event"),
            data.get("message", ""),
            data.get("risk_delta", 0),
            data.get("risk_score", state["risk_score"]),
            data.get("risk_level", state["risk_level"])
        )

        state["risk_score"] = data.get("risk_score", state["risk_score"])
        state["risk_level"] = data.get("risk_level", state["risk_level"])

        # 강제 개방 감지 시 이메일 알림
        if data.get("event_type") == "forced_open":
            send_email_alert(
                "[IoT 금고 경고] 강제 개방 감지",
                f"""IoT 스마트 금고에서 강제 개방이 감지되었습니다.

이벤트: {data.get("message")}
위험도: {data.get("risk_score")}
위험 단계: {data.get("risk_level")}
시간: {data.get("timestamp")}

즉시 금고 상태를 확인하세요.
"""
            )

    elif topic == "safe/auth/result":
        state["last_auth"] = data
        add_event(
            "auth_result",
            f"인증 결과: {data.get('reason', '')}",
            0,
            data.get("risk_score", state["risk_score"]),
            data.get("risk_level", state["risk_level"])
        )


mqtt_client.on_connect = on_connect
mqtt_client.on_message = on_message


# =========================================================
# 로그인 라우트
# =========================================================
@app.route("/login", methods=["GET", "POST"])
def login():
    error = None

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()

        try:
            user = check_user_login(username, password)
        except Exception as e:
            print("[DB ERROR]", e)
            user = None
            error = "DB 연결 오류가 발생했습니다. DB 설정을 확인하세요."

        if user:
            session["login_user"] = user["username"]
            return redirect(url_for("index"))

        if error is None:
            error = "아이디 또는 비밀번호가 올바르지 않습니다."

    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# =========================================================
# 웹 화면 / API
# =========================================================
@app.route("/")
@login_required
def index():
    return render_template("index.html")


@app.route("/api/state")
@login_required
def api_state():
    now = time.time()

    if state["otp"] is not None:
        remaining = int(state["otp_expires_at"] - now)

        if remaining > 0:
            state["otp_remaining"] = remaining
            state["otp_status"] = "ACTIVE"
        else:
            state["otp_remaining"] = 0
            state["otp_status"] = "EXPIRED"
    else:
        state["otp_remaining"] = 0
        if state.get("otp_status") not in ["EXPIRED"]:
            state["otp_status"] = "NONE"

    state["login_user"] = session.get("login_user")

    return jsonify(state)


@app.route("/api/otp", methods=["POST"])
@login_required
def api_generate_otp():
    otp = str(random.randint(100000, 999999))
    expires_at = time.time() + OTP_VALID_SECONDS

    state["otp"] = otp
    state["otp_expires_at"] = expires_at
    state["otp_remaining"] = OTP_VALID_SECONDS
    state["otp_status"] = "ACTIVE"

    payload = {
        "otp": otp,
        "expires_at": expires_at,
        "ttl": OTP_VALID_SECONDS
    }

    publish("safe/auth/otp", payload)

    add_event("otp_issued", f"OTP가 발급되었습니다. 유효시간 {OTP_VALID_SECONDS}초", 0)

    return jsonify({
        "ok": True,
        "otp": otp,
        "expires_at": expires_at,
        "ttl": OTP_VALID_SECONDS
    })


@app.route("/api/control/lock", methods=["POST"])
@login_required
def api_lock():
    publish("safe/control/lock", {"command": "lock"})
    add_event("web_lock", "웹에서 수동 잠금 명령을 전송했습니다.", 0)
    return jsonify({"ok": True})


@app.route("/api/control/unlock", methods=["POST"])
@login_required
def api_unlock():
    publish("safe/control/unlock", {"command": "unlock"})
    add_event("web_unlock", "웹에서 테스트용 잠금 해제 명령을 전송했습니다.", 0)
    return jsonify({"ok": True})


@app.route("/api/control/reset_risk", methods=["POST"])
@login_required
def api_reset_risk():
    publish("safe/control/reset_risk", {"command": "reset_risk"})
    state["risk_score"] = 0
    state["risk_level"] = "NORMAL"
    add_event("risk_reset", "웹에서 위험도 초기화 명령을 전송했습니다.", 0)
    return jsonify({"ok": True})


@app.route("/api/control/calibrate", methods=["POST"])
@login_required
def api_calibrate():
    publish("safe/control/calibrate", {"command": "calibrate"})
    add_event("calibrate", "웹에서 MPU6050 재보정 명령을 전송했습니다.", 0)
    return jsonify({"ok": True})


def start_mqtt():
    mqtt_client.connect(MQTT_BROKER, MQTT_PORT, 60)
    mqtt_client.loop_forever()


if __name__ == "__main__":
    mqtt_thread = threading.Thread(target=start_mqtt, daemon=True)
    mqtt_thread.start()

    print("웹 서버 시작")
    print("접속 주소: http://192.168.137.67:5000")
    app.run(host="0.0.0.0", port=5000, debug=False)