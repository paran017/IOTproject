import json
import math
import time

import RPi.GPIO as GPIO
import smbus2 as smbus
import paho.mqtt.client as mqtt


# =========================================================
# 기본 설정
# =========================================================
PASSWORD = "1234"

OTP_VALID_SECONDS = 180  # 3분

LOCK_ANGLE = 90
UNLOCK_ANGLE = 0
UNLOCK_TIMEOUT = 20

TILT_THRESHOLD = 15.0

MQTT_BROKER = "localhost"
MQTT_PORT = 1883

# =========================================================
# 진동 감지 필터 설정
# SW-420은 진동 세기값이 아니라 ON/OFF 신호이므로
# 짧은 흔들림 1회는 무시하고 일정 시간 안에 여러 번 감지될 때만 위험으로 처리
# =========================================================
VIB_WINDOW_SECONDS = 1.5
VIB_HIT_THRESHOLD = 4
VIB_COOLDOWN_SECONDS = 5

# =========================================================
# GPIO 설정 BCM 기준
# =========================================================
SERVO_PIN = 18
REED_PIN = 21
VIB_PIN = 26

ROWS = [5, 6, 13, 19]
COLS = [12, 16, 20]

KEYS = [
    ["1", "2", "3"],
    ["4", "5", "6"],
    ["7", "8", "9"],
    ["*", "0", "#"]
]

# =========================================================
# I2C 설정
# =========================================================
LCD_ADDR = 0x27
MPU6050_ADDR = 0x68

PWR_MGMT_1 = 0x6B
ACCEL_XOUT_H = 0x3B
ACCEL_YOUT_H = 0x3D
ACCEL_ZOUT_H = 0x3F

bus = smbus.SMBus(1)

# =========================================================
# LCD 설정
# =========================================================
LCD_WIDTH = 16
LCD_CHR = 1
LCD_CMD = 0

LCD_LINE_1 = 0x80
LCD_LINE_2 = 0xC0

LCD_BACKLIGHT = 0x08
ENABLE = 0b00000100


# =========================================================
# 상태 변수
# =========================================================
risk_score = 0

current_otp = None
otp_expires_at = 0
otp_used = False
last_otp = None

safe_locked = True
authorized_open = False
unlock_time = 0
opened_once = False

base_roll = 0
base_pitch = 0

vib_normal_state = 0
last_vib_state = None
vib_hits = []

last_status_publish = 0
last_vibration_time = 0
last_tilt_time = 0

forced_open_active = False
tilt_alert_active = False

input_buffer = ""


# =========================================================
# MQTT 생성
# =========================================================
def create_mqtt_client(client_id):
    try:
        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
    except Exception:
        return mqtt.Client(client_id=client_id)


mqtt_client = create_mqtt_client("iot_safe_device")


# =========================================================
# LCD 함수
# =========================================================
def lcd_toggle_enable(bits):
    time.sleep(0.0005)
    bus.write_byte(LCD_ADDR, bits | ENABLE)
    time.sleep(0.0005)
    bus.write_byte(LCD_ADDR, bits & ~ENABLE)
    time.sleep(0.0005)


def lcd_byte(bits, mode):
    high_bits = mode | (bits & 0xF0) | LCD_BACKLIGHT
    low_bits = mode | ((bits << 4) & 0xF0) | LCD_BACKLIGHT

    bus.write_byte(LCD_ADDR, high_bits)
    lcd_toggle_enable(high_bits)

    bus.write_byte(LCD_ADDR, low_bits)
    lcd_toggle_enable(low_bits)


def lcd_init():
    lcd_byte(0x33, LCD_CMD)
    lcd_byte(0x32, LCD_CMD)
    lcd_byte(0x06, LCD_CMD)
    lcd_byte(0x0C, LCD_CMD)
    lcd_byte(0x28, LCD_CMD)
    lcd_byte(0x01, LCD_CMD)
    time.sleep(0.005)


def lcd_string(message, line):
    message = str(message)
    message = message[:LCD_WIDTH].ljust(LCD_WIDTH, " ")
    lcd_byte(line, LCD_CMD)

    for char in message:
        lcd_byte(ord(char), LCD_CHR)


def lcd_clear():
    lcd_byte(0x01, LCD_CMD)
    time.sleep(0.005)


def show_ready():
    lcd_string("Input Password", LCD_LINE_1)
    lcd_string("+ OTP then #", LCD_LINE_2)


def show_alert(line1, line2):
    lcd_string(line1, LCD_LINE_1)
    lcd_string(line2, LCD_LINE_2)


# =========================================================
# MQTT Publish 함수
# =========================================================
def publish(topic, payload):
    try:
        mqtt_client.publish(topic, json.dumps(payload, ensure_ascii=False))
    except Exception as e:
        print("[MQTT Publish Error]", e)


def get_risk_level():
    if risk_score >= 100:
        return "INTRUSION"
    elif risk_score >= 80:
        return "DANGER"
    elif risk_score >= 50:
        return "WARNING"
    elif risk_score >= 30:
        return "CAUTION"
    return "NORMAL"


def get_otp_status():
    now = time.time()

    if current_otp is not None:
        if now <= otp_expires_at:
            return "ACTIVE"
        return "EXPIRED"

    if otp_used:
        return "USED"

    return "NONE"


def publish_status():
    payload = {
        "safe_locked": safe_locked,
        "door_closed": is_door_closed(),
        "risk_score": risk_score,
        "risk_level": get_risk_level(),
        "otp_status": get_otp_status(),
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
    }
    publish("safe/status", payload)


def publish_event(event_type, message, risk_delta=0):
    payload = {
        "event_type": event_type,
        "message": message,
        "risk_delta": risk_delta,
        "risk_score": risk_score,
        "risk_level": get_risk_level(),
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
    }
    publish("safe/event", payload)
    publish_status()


def publish_auth_result(success, reason):
    payload = {
        "success": success,
        "reason": reason,
        "risk_score": risk_score,
        "risk_level": get_risk_level(),
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
    }
    publish("safe/auth/result", payload)


def add_risk(score, event_type, message):
    global risk_score

    risk_score += score
    print(f"[RISK] {event_type} / +{score} / 현재 위험도 {risk_score}")

    publish_event(event_type, message, score)

    if risk_score >= 100:
        show_alert("INTRUSION", f"Risk: {risk_score}")
    elif risk_score >= 80:
        show_alert("DANGER", f"Risk: {risk_score}")
    elif risk_score >= 50:
        show_alert("WARNING", f"Risk: {risk_score}")
    elif risk_score >= 30:
        show_alert("CAUTION", f"Risk: {risk_score}")


# =========================================================
# 서보모터 함수
# =========================================================
def set_servo_angle(angle):
    duty = 2.5 + (angle / 18.0)
    servo.ChangeDutyCycle(duty)
    time.sleep(0.6)
    servo.ChangeDutyCycle(0)


def lock_safe():
    global safe_locked, authorized_open, opened_once

    set_servo_angle(LOCK_ANGLE)

    safe_locked = True
    authorized_open = False
    opened_once = False

    lcd_string("Safe Locked", LCD_LINE_1)
    lcd_string("Input Ready", LCD_LINE_2)

    print("[LOCK] 금고 잠금")
    publish_event("lock", "금고가 잠금 상태가 되었습니다.", 0)


def unlock_safe():
    global safe_locked, authorized_open, unlock_time, opened_once

    set_servo_angle(UNLOCK_ANGLE)

    safe_locked = False
    authorized_open = True
    unlock_time = time.time()
    opened_once = False

    lcd_string("Auth Success", LCD_LINE_1)
    lcd_string("Safe Unlocked", LCD_LINE_2)

    print("[UNLOCK] 금고 열림")
    publish_event("unlock", "인증 성공으로 금고가 열렸습니다.", 0)


# =========================================================
# 센서 함수
# =========================================================
def is_door_closed():
    # 단품 리드 스위치 기준
    # 자석 가까움 = LOW = 문 닫힘
    return GPIO.input(REED_PIN) == GPIO.LOW


def read_raw_data(addr):
    high = bus.read_byte_data(MPU6050_ADDR, addr)
    low = bus.read_byte_data(MPU6050_ADDR, addr + 1)

    value = (high << 8) | low

    if value > 32768:
        value -= 65536

    return value


def get_roll_pitch():
    acc_x = read_raw_data(ACCEL_XOUT_H)
    acc_y = read_raw_data(ACCEL_YOUT_H)
    acc_z = read_raw_data(ACCEL_ZOUT_H)

    ax = acc_x / 16384.0
    ay = acc_y / 16384.0
    az = acc_z / 16384.0

    roll = math.atan2(ay, az) * 180 / math.pi
    pitch = math.atan2(-ax, math.sqrt(ay * ay + az * az)) * 180 / math.pi

    return roll, pitch


def calibrate_mpu():
    global base_roll, base_pitch

    lcd_string("Calibrating", LCD_LINE_1)
    lcd_string("Keep Still", LCD_LINE_2)

    print("MPU6050 기준값 설정 중...")
    time.sleep(2)

    base_roll, base_pitch = get_roll_pitch()

    print(f"기준 Roll : {base_roll:.2f}")
    print(f"기준 Pitch: {base_pitch:.2f}")

    publish_event("calibrate", "MPU6050 기준값을 설정했습니다.", 0)


# =========================================================
# 키패드 함수
# =========================================================
def scan_keypad():
    for r_idx, row in enumerate(ROWS):
        GPIO.output(row, GPIO.LOW)

        for c_idx, col in enumerate(COLS):
            if GPIO.input(col) == GPIO.LOW:
                key = KEYS[r_idx][c_idx]
                GPIO.output(row, GPIO.HIGH)
                return key

        GPIO.output(row, GPIO.HIGH)

    return None


def read_keypad():
    key1 = scan_keypad()

    if key1 is None:
        return None

    time.sleep(0.05)

    key2 = scan_keypad()

    if key1 != key2:
        return None

    while scan_keypad() == key1:
        time.sleep(0.02)

    return key1


# =========================================================
# OTP 및 인증 처리
# =========================================================
def set_new_otp(otp, expires_at):
    global current_otp, otp_expires_at, otp_used, last_otp

    current_otp = str(otp)
    last_otp = str(otp)
    otp_expires_at = float(expires_at)
    otp_used = False

    remain = int(otp_expires_at - time.time())

    lcd_string("OTP Received", LCD_LINE_1)
    lcd_string(f"Valid {remain}s", LCD_LINE_2)

    print(f"[OTP] 새 OTP 수신: {current_otp}, 남은 시간 {remain}초")
    publish_event("otp_issued", "새 OTP가 발급되었습니다.", 0)


def clear_otp_after_success():
    global current_otp, otp_expires_at, otp_used

    current_otp = None
    otp_expires_at = 0
    otp_used = True


def handle_auth_attempt(value):
    global current_otp, otp_expires_at, otp_used

    now = time.time()

    print("[AUTH] 입력값:", value)

    # 이미 사용된 OTP 재사용 시도
    if otp_used and last_otp is not None and value == PASSWORD + last_otp:
        lcd_string("OTP Used", LCD_LINE_1)
        lcd_string("Issue Again", LCD_LINE_2)
        add_risk(20, "otp_reuse", "이미 사용된 OTP 재사용 시도")
        publish_auth_result(False, "otp_reuse")
        return

    # OTP 미발급 상태
    if current_otp is None:
        lcd_string("No OTP Issued", LCD_LINE_1)
        lcd_string("Check Web", LCD_LINE_2)
        add_risk(15, "no_otp", "OTP 미발급 상태에서 입력 시도")
        publish_auth_result(False, "no_otp")
        return

    # OTP 만료
    if now > otp_expires_at:
        lcd_string("OTP Expired", LCD_LINE_1)
        lcd_string("Issue Again", LCD_LINE_2)
        add_risk(15, "otp_expired", "만료된 OTP 입력 시도")
        publish_auth_result(False, "otp_expired")

        current_otp = None
        otp_expires_at = 0
        otp_used = False
        return

    # 인증 성공
    if value == PASSWORD + current_otp:
        lcd_string("Auth Success", LCD_LINE_1)
        lcd_string("Opening...", LCD_LINE_2)

        publish_auth_result(True, "auth_success")
        publish_event("auth_success", "비밀번호와 OTP 인증에 성공했습니다.", 0)

        clear_otp_after_success()
        unlock_safe()
        return

    # 일반 인증 실패
    lcd_string("Auth Failed", LCD_LINE_1)
    lcd_string("Try Again", LCD_LINE_2)

    add_risk(10, "auth_fail", "비밀번호 또는 OTP 인증 실패")
    publish_auth_result(False, "auth_fail")


# =========================================================
# MQTT Subscribe 처리
# =========================================================
def on_connect(client, userdata, flags, reason_code, properties=None):
    print("[MQTT] 연결 완료")

    client.subscribe("safe/auth/otp")
    client.subscribe("safe/control/lock")
    client.subscribe("safe/control/unlock")
    client.subscribe("safe/control/reset_risk")
    client.subscribe("safe/control/calibrate")

    publish_event("device_online", "IoT 금고 장치가 MQTT에 연결되었습니다.", 0)


def on_message(client, userdata, msg):
    global risk_score

    topic = msg.topic
    payload_text = msg.payload.decode("utf-8")

    print(f"[MQTT] {topic}: {payload_text}")

    try:
        data = json.loads(payload_text)
    except Exception:
        data = {}

    if topic == "safe/auth/otp":
        otp = data.get("otp")
        expires_at = data.get("expires_at")

        if otp and expires_at:
            set_new_otp(otp, expires_at)

    elif topic == "safe/control/lock":
        lock_safe()

    elif topic == "safe/control/unlock":
        unlock_safe()

    elif topic == "safe/control/reset_risk":
        risk_score = 0
        lcd_string("Risk Reset", LCD_LINE_1)
        lcd_string("Normal", LCD_LINE_2)
        publish_event("risk_reset", "위험도가 초기화되었습니다.", 0)
        time.sleep(1)
        show_ready()

    elif topic == "safe/control/calibrate":
        calibrate_mpu()
        time.sleep(1)
        show_ready()


# =========================================================
# GPIO 초기화
# =========================================================
GPIO.setwarnings(False)
GPIO.setmode(GPIO.BCM)

# 키패드
for row in ROWS:
    GPIO.setup(row, GPIO.OUT)
    GPIO.output(row, GPIO.HIGH)

for col in COLS:
    GPIO.setup(col, GPIO.IN, pull_up_down=GPIO.PUD_UP)

# 센서
GPIO.setup(REED_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)
GPIO.setup(VIB_PIN, GPIO.IN)

# 서보
GPIO.setup(SERVO_PIN, GPIO.OUT)
servo = GPIO.PWM(SERVO_PIN, 50)
servo.start(0)


# =========================================================
# 메인
# =========================================================
try:
    lcd_init()

    lcd_string("IoT Smart Safe", LCD_LINE_1)
    lcd_string("Starting...", LCD_LINE_2)

    # MPU6050 깨우기
    bus.write_byte_data(MPU6050_ADDR, PWR_MGMT_1, 0)

    vib_normal_state = GPIO.input(VIB_PIN)
    last_vib_state = vib_normal_state

    calibrate_mpu()

    mqtt_client.on_connect = on_connect
    mqtt_client.on_message = on_message
    mqtt_client.connect(MQTT_BROKER, MQTT_PORT, 60)
    mqtt_client.loop_start()

    lock_safe()
    show_ready()

    print("================================")
    print("IoT 스마트 금고 장치 프로그램 시작")
    print("고정 비밀번호:", PASSWORD)
    print("OTP는 웹 대시보드에서 발급")
    print("입력 방식: 비밀번호 + OTP + #")
    print("예: 1234 + 6자리 OTP + #")
    print("================================")

    while True:
        now = time.time()

        # -------------------------------------------------
        # 문 상태 / 자동 잠금 / 강제 개방 감지
        # -------------------------------------------------
        door_closed = is_door_closed()

        if not safe_locked:
            if not door_closed:
                opened_once = True
                lcd_string("Door Opened", LCD_LINE_1)
                lcd_string("Close to Lock", LCD_LINE_2)

            if opened_once and door_closed:
                lcd_string("Door Closed", LCD_LINE_1)
                lcd_string("Auto Locking", LCD_LINE_2)
                time.sleep(1)
                lock_safe()
                show_ready()

            elif now - unlock_time > UNLOCK_TIMEOUT:
                lcd_string("Timeout", LCD_LINE_1)
                lcd_string("Auto Locking", LCD_LINE_2)
                time.sleep(1)
                lock_safe()
                show_ready()

        if safe_locked and not door_closed:
            if not forced_open_active:
                forced_open_active = True
                add_risk(100, "forced_open", "잠금 상태에서 문 열림 감지")
                lcd_string("Forced Open", LCD_LINE_1)
                lcd_string("INTRUSION", LCD_LINE_2)
        else:
            forced_open_active = False

        # -------------------------------------------------
        # 진동 감지
        # 약한 진동은 무시하고, 짧은 시간 안에 여러 번 감지될 때만 위험 처리
        # -------------------------------------------------
        vib_state = GPIO.input(VIB_PIN)

        if last_vib_state == vib_normal_state and vib_state != vib_normal_state:
            vib_hits.append(now)

        last_vib_state = vib_state

        vib_hits = [t for t in vib_hits if now - t <= VIB_WINDOW_SECONDS]

        if len(vib_hits) >= VIB_HIT_THRESHOLD:
            if now - last_vibration_time > VIB_COOLDOWN_SECONDS:
                add_risk(20, "vibration", "일정 수준 이상의 반복 진동 감지")
                last_vibration_time = now
                vib_hits.clear()

        # -------------------------------------------------
        # 기울기 감지
        # -------------------------------------------------
        try:
            roll, pitch = get_roll_pitch()
            roll_diff = abs(roll - base_roll)
            pitch_diff = abs(pitch - base_pitch)

            if roll_diff >= TILT_THRESHOLD or pitch_diff >= TILT_THRESHOLD:
                if not tilt_alert_active and now - last_tilt_time > 2:
                    tilt_alert_active = True
                    add_risk(30, "tilt", "기울기 또는 이동 감지")
                    last_tilt_time = now
            else:
                tilt_alert_active = False

        except OSError:
            print("[MPU6050] 읽기 오류")

        # -------------------------------------------------
        # 키패드 입력
        # -------------------------------------------------
        key = read_keypad()

        if key:
            if key == "*":
                input_buffer = ""
                lcd_string("Input Cleared", LCD_LINE_1)
                lcd_string("Try Again", LCD_LINE_2)
                time.sleep(0.5)
                show_ready()

            elif key == "#":
                if input_buffer:
                    handle_auth_attempt(input_buffer)
                    input_buffer = ""
                    time.sleep(1.2)
                    if safe_locked:
                        show_ready()
                else:
                    lcd_string("Empty Input", LCD_LINE_1)
                    lcd_string("Try Again", LCD_LINE_2)

            else:
                input_buffer += key
                masked = "*" * len(input_buffer)
                lcd_string("Input:", LCD_LINE_1)
                lcd_string(masked, LCD_LINE_2)

        # -------------------------------------------------
        # 상태 주기적 Publish
        # -------------------------------------------------
        if now - last_status_publish > 2:
            last_status_publish = now
            publish_status()

        time.sleep(0.05)

except KeyboardInterrupt:
    print("\n프로그램 종료")

finally:
    try:
        mqtt_client.loop_stop()
        mqtt_client.disconnect()
    except Exception:
        pass

    servo.stop()
    GPIO.cleanup()

    try:
        lcd_clear()
    except Exception:
        pass