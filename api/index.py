# 🚀 GigShield Final Backend (Submission Ready)

import os, random, hashlib, time
from datetime import datetime, timedelta

from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.responses import JSONResponse
from fastapi.requests import Request

from sqlalchemy import create_engine, Column, Integer, String, Float
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

import requests
import jwt

# ───────── CONFIG ─────────

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./gigshield.db")
JWT_SECRET = "gigshield_secret"

# ───────── DB SETUP ─────────

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()

# ───────── MODELS ─────────

class User(Base):
**tablename** = "users"
id = Column(Integer, primary_key=True)
phone = Column(String, unique=True)
name = Column(String)
city = Column(String)

class Claim(Base):
**tablename** = "claims"
id = Column(Integer, primary_key=True)
user_id = Column(Integer)
amount = Column(Float)
status = Column(String)

Base.metadata.create_all(bind=engine)

# ───────── APP ─────────

app = FastAPI(title="GigShield API")

app.add_middleware(
CORSMiddleware,
allow_origins=["*"],
allow_methods=["*"],
allow_headers=["*"],
)

security = HTTPBearer(auto_error=False)

# ───────── ERROR HANDLING ─────────

@app.exception_handler(Exception)
async def global_exception(request: Request, exc: Exception):
return JSONResponse(status_code=500, content={
"success": False,
"error": str(exc)
})

# ───────── OTP STORE ─────────

OTP_STORE = {}

def generate_otp(phone):
otp = str(random.randint(100000, 999999))
OTP_STORE[phone] = {"otp": otp, "exp": time.time() + 300}
return otp

def verify_otp(phone, otp):
rec = OTP_STORE.get(phone)
return rec and rec["otp"] == otp and time.time() < rec["exp"]

# ───────── JWT ─────────

def create_token(user_id):
payload = {
"user_id": user_id,
"exp": datetime.utcnow() + timedelta(days=7)
}
return jwt.encode(payload, JWT_SECRET, algorithm="HS256")

def get_user(creds: HTTPAuthorizationCredentials = Depends(security)):
if not creds:
raise HTTPException(401, "Unauthorized")

```
try:
    data = jwt.decode(creds.credentials, JWT_SECRET, algorithms=["HS256"])
    return data["user_id"]
except:
    raise HTTPException(401, "Invalid token")
```

# ───────── WEATHER (SAFE MOCK) ─────────

def get_weather(city):
try:
return {
"rain": random.randint(0, 120),
"temp": random.randint(25, 45)
}
except:
return {"rain": 50, "temp": 35}

# ───────── AI LOGIC (SIMPLE) ─────────

def predict_risk(weather):
if weather["rain"] > 80 or weather["temp"] > 42:
return 1
return 0

# ───────── ROUTES ─────────

@app.get("/api/health")
def health():
return {"success": True, "status": "ok"}

# 🔐 AUTH

@app.post("/api/auth/send-otp")
def send_otp(data: dict):
otp = generate_otp(data["phone"])
return {"success": True, "otp": otp}

@app.post("/api/auth/verify-otp")
def verify_otp_api(data: dict):
if not verify_otp(data["phone"], data["otp"]):
raise HTTPException(400, "Invalid OTP")

```
db = SessionLocal()
user = db.query(User).filter(User.phone == data["phone"]).first()

if user:
    token = create_token(user.id)
    return {"success": True, "token": token}

return {"success": True, "new_user": True}
```

@app.post("/api/auth/register")
def register(data: dict):
db = SessionLocal()

```
user = User(
    phone=data["phone"],
    name=data["name"],
    city=data["city"]
)

db.add(user)
db.commit()
db.refresh(user)

token = create_token(user.id)
return {"success": True, "token": token}
```

# ⚡ CLAIM ENGINE

@app.post("/api/trigger")
def trigger(user_id=Depends(get_user)):
db = SessionLocal()
user = db.query(User).get(user_id)

```
weather = get_weather(user.city)
risk = predict_risk(weather)

if risk == 0:
    return {"success": True, "message": "No claim triggered"}

amount = 400 if weather["rain"] > 80 else 300

claim = Claim(
    user_id=user_id,
    amount=amount,
    status="APPROVED"
)

db.add(claim)
db.commit()

return {"success": True, "amount": amount}
```

# 💰 PAYOUT

@app.post("/api/payout")
def payout(user_id=Depends(get_user)):
db = SessionLocal()

```
claims = db.query(Claim).filter(
    Claim.user_id == user_id,
    Claim.status == "APPROVED"
).all()

total = sum(c.amount for c in claims)

return {"success": True, "payout": total}
```

# 👤 PROFILE

@app.get("/api/me")
def me(user_id=Depends(get_user)):
db = SessionLocal()
user = db.query(User).get(user_id)

```
weather = get_weather(user.city)

return {
    "user": {
        "name": user.name,
        "city": user.city
    },
    "weather": weather
}
```
