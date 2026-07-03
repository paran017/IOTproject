# 🔒 IoT Smart Safe

Raspberry Pi 4 기반의 IoT 스마트 금고 프로젝트입니다.

웹에서 로그인 후 OTP를 발급받아 금고를 열 수 있으며, 진동·기울기·강제 개방을 감지하여 실시간으로 웹 대시보드에서 모니터링할 수 있습니다.

---

## 📌 Features

- 웹 로그인 (MySQL)
- OTP 기반 2단계 인증
- 3x4 키패드를 이용한 비밀번호 입력
- LCD를 통한 입력 상태 및 인증 결과 출력
- 서보모터를 이용한 금고 잠금/해제
- 리드 스위치를 이용한 문 상태 감지
- SW-420 진동 감지
- MPU6050 기울기 감지
- 위험도(Risk Score) 기반 침입 감지
- MQTT 기반 실시간 통신
- 자동 잠금 기능

---

## 🛠 Tech Stack

- Raspberry Pi 4
- Python
- Flask
- MySQL
- MQTT (Mosquitto)
- HTML / CSS
- GPIO
- I2C

---

## 📦 Hardware

- Raspberry Pi 4
- 3x4 Matrix Keypad
- I2C LCD (0x27)
- MPU6050 (GY-521)
- SW-420 Vibration Sensor
- Reed Switch
- MG90S Servo Motor

---

## 🏗 System Architecture

```
Browser
    │
HTTP
    │
Flask Web Server
    │
MySQL
    │
MQTT Broker
    │
Raspberry Pi 4
    ├── Keypad
    ├── LCD
    ├── MPU6050
    ├── SW-420
    ├── Reed Switch
    └── Servo Motor
```

---

## 📂 Project Structure

```
iot/
├── safe_device.py
├── web_server.py
├── requirements.txt
├── login.html
├── index.html
└── README.md
```

---

## 🚀 Installation

```bash
git clone https://github.com/your-id/iot-smart-safe.git
cd iot-smart-safe

pip install -r requirements.txt
```

필요한 서비스

- Mosquitto MQTT Broker
- MySQL
- Raspberry Pi OS

---

## 📷 Demo

- OTP 발급
- 키패드 인증
- 자동 잠금
- 위험도 계산
- 실시간 웹 모니터링

---

## 👨‍💻 Author

**윤종찬**

Computer Science Student
