"""
GigShield API v3.0 — Production-Grade Insurtech Backend
Auth: JWT + OTP | KYC | Policies | Claims | Payouts | Admin
Vercel-ready: FastAPI + Mangum (in-memory store, swap DB_URL for Postgres)
"""

import os, random, string, hashlib, time
from datetime import datetime, timedelta
from typing import Optional
from fastapi import FastAPI, HTTPException, Depends, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from mangum import Mangum
from pydantic import BaseModel, field_validator
import httpx

try:
    from jose import JWTError, jwt
    JWT_AVAILABLE = True
except ImportError:
    JWT_AVAILABLE = False

# ── CONFIG ───────────────────────────────────────────────────────
SECRET_KEY  = os.getenv("JWT_SECRET", "gigshield-secret-2026-insurtech")
ALGORITHM   = "HS256"
TOKEN_EXP   = 60 * 24 * 7  # 7 days in minutes
OWM_KEY     = os.getenv("OWM_API_KEY", "")
ADMIN_TOKEN = os.getenv("ADMIN_SECRET", "gigshield-admin-2026")

# ── APP ──────────────────────────────────────────────────────────
app = FastAPI(title="GigShield API", version="3.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

security = HTTPBearer(auto_error=False)

# ── IN-MEMORY DATABASE ───────────────────────────────────────────
DB = {
    "users":     {},   # phone → user dict
    "workers":   {},   # id → worker profile
    "kyc":       {},   # worker_id → kyc record
    "policies":  {},   # worker_id → policy
    "shifts":    {},   # id → shift
    "claims":    {},   # id → claim
    "payouts":   {},   # id → payout
    "otp_store": {},   # phone → {otp, expires, attempts}
    "sessions":  {},   # token_hash → worker_id
    "audit_log": [],   # list of audit events
}
_seq = {k: 0 for k in ["user","shift","claim","payout","kyc"]}

def _next(k):
    _seq[k] += 1
    return _seq[k]

def _now():    return datetime.utcnow().isoformat()
def _week_id(): return datetime.utcnow().strftime("%Y-W%U")
def _upi():    return "UPI" + "".join(random.choices(string.digits, k=12))

def _audit(action, worker_id, detail=""):
    DB["audit_log"].append({
        "ts": _now(), "action": action,
        "worker_id": worker_id, "detail": detail
    })

# ── CONSTANTS ────────────────────────────────────────────────────
ZONE_RISK = {
    "Bandra":1.30,"Andheri":1.22,"Salt Lake":1.18,"T Nagar":1.12,
    "Koramangala":1.08,"Sector 18":1.05,"Connaught Place":1.04,"Jubilee Hills":1.02,
}
CITY_SEASONAL = {
    "Mumbai":1.20,"Kolkata":1.15,"Chennai":1.10,"Delhi":1.08,
    "Bangalore":1.05,"Hyderabad":1.04,"Noida":1.06,
}
BASE_PREMIUM = {"Basic":20.0,"Standard":30.0,"Premium":50.0}
WEEKLY_CAP   = {"Basic":150.0,"Standard":200.0,"Premium":300.0}
MAX_DISRUPT  = {"Basic":1,"Standard":2,"Premium":3}
VEHICLE_RISK = {"Bicycle":25,"Motorcycle":15,"Scooter":18,"Car":8}
PLATFORM_RISK= {"Zepto":20,"Blinkit":18,"Swiggy":15,"Zomato":15}
CITY_COORDS  = {
    "Mumbai":(19.076,72.8777),"Kolkata":(22.5726,88.3639),
    "Delhi":(28.6139,77.209),"Chennai":(13.0827,80.2707),
    "Bangalore":(12.9716,77.5946),"Hyderabad":(17.385,78.4867),"Noida":(28.5355,77.391),
}
MOCK_WEATHER = {
    "Mumbai":   {"rain_mm":12.0,"temp_celsius":29.5,"aqi":85, "condition":"Partly Cloudy"},
    "Kolkata":  {"rain_mm":8.0, "temp_celsius":31.0,"aqi":120,"condition":"Humid"},
    "Delhi":    {"rain_mm":0.0, "temp_celsius":38.0,"aqi":210,"condition":"Hazy"},
    "Chennai":  {"rain_mm":3.0, "temp_celsius":33.0,"aqi":90, "condition":"Sunny"},
    "Bangalore":{"rain_mm":5.0, "temp_celsius":26.0,"aqi":75, "condition":"Cloudy"},
    "Hyderabad":{"rain_mm":1.0, "temp_celsius":35.0,"aqi":95, "condition":"Clear"},
    "Noida":    {"rain_mm":0.0, "temp_celsius":37.0,"aqi":195,"condition":"Hazy"},
}

# ── JWT HELPERS ──────────────────────────────────────────────────
def _create_token(data: dict) -> str:
    if not JWT_AVAILABLE:
        # Fallback simple token
        raw = f"{data}:{time.time()}"
        return hashlib.sha256(raw.encode()).hexdigest()
    payload = {**data, "exp": datetime.utcnow() + timedelta(minutes=TOKEN_EXP)}
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)

def _decode_token(token: str) -> Optional[dict]:
    if not JWT_AVAILABLE:
        return DB["sessions"].get(token)
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except Exception:
        return None

def get_current_worker(creds: HTTPAuthorizationCredentials = Depends(security)):
    if not creds:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
    payload = _decode_token(creds.credentials)
    if not payload:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")
    wid = payload.get("worker_id")
    if not wid or wid not in DB["workers"]:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Worker not found")
    return DB["workers"][wid]

def get_current_worker_optional(creds: HTTPAuthorizationCredentials = Depends(security)):
    try:
        return get_current_worker(creds)
    except Exception:
        return None

def require_kyc(worker=Depends(get_current_worker)):
    kyc = DB["kyc"].get(worker["id"])
    if not kyc or kyc["status"] != "VERIFIED":
        raise HTTPException(403, "KYC verification required before accessing this feature")
    return worker

# ── OTP SERVICE ──────────────────────────────────────────────────
def _generate_otp(phone: str) -> str:
    otp = str(random.randint(100000, 999999))
    DB["otp_store"][phone] = {
        "otp": otp,
        "expires": time.time() + 300,  # 5 min
        "attempts": 0,
        "verified": False,
    }
    return otp

def _verify_otp(phone: str, otp: str) -> bool:
    record = DB["otp_store"].get(phone)
    if not record:
        return False
    if time.time() > record["expires"]:
        return False
    record["attempts"] += 1
    if record["attempts"] > 5:
        return False
    if record["otp"] == otp:
        record["verified"] = True
        return True
    return False

async def _send_otp(phone: str, otp: str):
    """Send OTP via Fast2SMS or simulate."""
    api_key = os.getenv("FAST2SMS_KEY", "")
    if api_key:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.post(
                    "https://www.fast2sms.com/dev/bulkV2",
                    headers={"authorization": api_key},
                    json={
                        "route": "otp",
                        "variables_values": otp,
                        "numbers": phone,
                    }
                )
        except Exception:
            pass
    # Always print for demo/dev
    print(f"📱 OTP for {phone}: {otp}")

# ── SERVICES ─────────────────────────────────────────────────────
def hash_password(pw: str) -> str:
    return hashlib.sha256(pw.encode()).hexdigest()

def compute_risk_score(zone, city, vehicle_type, platform,
                       rain_mm=0, temp_c=30, aqi=80):
    score = 0
    zr = ZONE_RISK.get(zone, 1.0)
    score += min(int((zr - 1.0) / 0.30 * 30), 30)
    cs = CITY_SEASONAL.get(city, 1.0)
    score += min(int((cs - 1.0) / 0.20 * 20), 20)
    ws = (20 if rain_mm > 80 else 12 if rain_mm > 40
          else 18 if temp_c > 42 else 10 if temp_c > 38
          else 15 if aqi > 300 else 8 if aqi > 200 else 0)
    score += min(ws, 20)
    score += int(VEHICLE_RISK.get(vehicle_type, 15) * 15 / 25)
    score += int(PLATFORM_RISK.get(platform, 15) * 10 / 20)
    score = max(0, min(100, score))
    return {"score": score, "level": "LOW" if score < 35 else "MEDIUM" if score < 65 else "HIGH"}

def calc_premium(plan, zone, city, weeks_clean=0, rain_mm=0, temp_c=30, aqi=80):
    base = BASE_PREMIUM.get(plan, 30.0)
    zr   = ZONE_RISK.get(zone, 1.0)
    sf   = CITY_SEASONAL.get(city, 1.0)
    if rain_mm > 80 or temp_c > 42 or aqi > 300:
        sf = min(sf * 1.1, 1.3)
    loyalty = min(weeks_clean * 0.5, 3.0) if weeks_clean >= 8 else 0.0
    return {
        "base_premium": base, "zone_risk": zr,
        "seasonal_factor": round(sf, 3), "loyalty_discount": loyalty,
        "dynamic_premium": round(base * zr * sf - loyalty, 2),
        "weekly_cap": WEEKLY_CAP.get(plan, 200.0),
        "max_disruptions": MAX_DISRUPT.get(plan, 2),
    }

def predict_earnings(city, zone):
    base = 3500.0
    cm   = {"Mumbai":1.15,"Bangalore":1.10,"Delhi":1.08,
            "Chennai":1.05,"Kolkata":1.03,"Hyderabad":1.02}
    zr   = ZONE_RISK.get(zone, 1.0)
    return round(base * cm.get(city, 1.0) * (0.9 + zr * 0.1), 0)

def compute_fraud_score(gps_points, active_minutes, avg_speed, duration_minutes):
    score, flags = 0, []
    coverage = gps_points / max(1, duration_minutes)
    if gps_points < 10:      score += 30; flags.append("LOW_GPS_COVERAGE")
    elif coverage < 0.3:     score += 20; flags.append("SPARSE_GPS")
    ratio = active_minutes / max(1, duration_minutes)
    if ratio < 0.10:         score += 30; flags.append("VERY_LOW_ACTIVITY")
    elif ratio < 0.20:       score += 20; flags.append("LOW_ACTIVITY")
    elif ratio < 0.30:       score += 10; flags.append("BELOW_AVG")
    if avg_speed > 120:      score += 25; flags.append("IMPOSSIBLE_SPEED")
    elif avg_speed < 0.5 and active_minutes > 30:
                             score += 20; flags.append("STATIONARY")
    score = max(0, min(100, score))
    verdict = "AUTO_APPROVED" if score < 40 else "MANUAL_REVIEW" if score < 70 else "AUTO_REJECTED"
    return {"score": score, "verdict": verdict, "flags": flags}

def evaluate_triggers(rain_mm, temp_c, aqi):
    t = []
    if rain_mm > 150: t.append({"type":"URBAN_FLOOD","value":rain_mm,"multiplier":1.5,"label":"Urban Flooding"})
    elif rain_mm > 80: t.append({"type":"HEAVY_RAIN","value":rain_mm,"multiplier":1.0,"label":"Heavy Rainfall"})
    elif rain_mm > 40: t.append({"type":"MODERATE_RAIN","value":rain_mm,"multiplier":0.6,"label":"Moderate Rain"})
    if temp_c > 42: t.append({"type":"EXTREME_HEAT","value":temp_c,"multiplier":1.0,"label":"Extreme Heat"})
    if aqi > 300:   t.append({"type":"SEVERE_AQI","value":aqi,"multiplier":1.0,"label":"Severe AQI"})
    return t

async def fetch_weather(city: str):
    if OWM_KEY:
        coords = CITY_COORDS.get(city)
        if coords:
            try:
                async with httpx.AsyncClient(timeout=4.0) as client:
                    lat, lon = coords
                    r = await client.get(
                        "https://api.openweathermap.org/data/2.5/weather",
                        params={"lat":lat,"lon":lon,"appid":OWM_KEY,"units":"metric"}
                    )
                    if r.status_code == 200:
                        d    = r.json()
                        rain = d.get("rain",{}).get("1h",0)
                        temp = d["main"]["temp"]
                        cond = d["weather"][0]["description"].title()
                        ar   = await client.get(
                            "https://api.openweathermap.org/data/2.5/air_pollution",
                            params={"lat":lat,"lon":lon,"appid":OWM_KEY}
                        )
                        aqi = 80
                        if ar.status_code == 200:
                            idx = ar.json()["list"][0]["main"]["aqi"]
                            aqi = {1:40,2:90,3:160,4:250,5:380}.get(idx,80)
                        return {"rain_mm":round(rain,1),"temp_celsius":round(temp,1),
                                "aqi":aqi,"condition":cond,"source":"live"}
            except Exception:
                pass
    base = MOCK_WEATHER.get(city, {"rain_mm":5,"temp_celsius":30,"aqi":100,"condition":"Clear"})
    return {**base, "source":"mock"}

# ═══════════════════════════════════════════════════════════════
# ROUTES
# ═══════════════════════════════════════════════════════════════

@app.get("/api/health")
def health():
    return {
        "status": "ok", "version": "3.0.0",
        "workers": len(DB["workers"]),
        "claims": len(DB["claims"]),
        "kyc_verified": sum(1 for k in DB["kyc"].values() if k["status"]=="VERIFIED"),
    }


# ── AUTH / OTP ────────────────────────────────────────────────────

class SendOTPReq(BaseModel):
    phone: str
    @field_validator("phone")
    @classmethod
    def validate_phone(cls, v):
        digits = v.replace(" ","").replace("-","").replace("+91","")
        if not digits.isdigit() or len(digits) != 10:
            raise ValueError("Enter valid 10-digit Indian mobile number")
        return digits

@app.post("/api/auth/send-otp")
async def send_otp(req: SendOTPReq):
    otp = _generate_otp(req.phone)
    await _send_otp(req.phone, otp)
    is_registered = req.phone in DB["users"]
    return {
        "success": True,
        "message": f"OTP sent to +91-{req.phone}",
        "is_registered": is_registered,
        # Return OTP in demo mode (remove in production!)
        "demo_otp": otp,
        "expires_in": 300,
    }


class VerifyOTPReq(BaseModel):
    phone: str
    otp: str

@app.post("/api/auth/verify-otp")
def verify_otp(req: VerifyOTPReq):
    phone = req.phone.replace("+91","").replace(" ","")
    if not _verify_otp(phone, req.otp):
        raise HTTPException(400, "Invalid or expired OTP. Please try again.")
    # If user exists → login
    if phone in DB["users"]:
        user   = DB["users"][phone]
        worker = DB["workers"].get(user["worker_id"])
        if not worker:
            raise HTTPException(404, "Account not found")
        token = _create_token({"worker_id": worker["id"], "phone": phone})
        DB["sessions"][token] = worker["id"]
        kyc   = DB["kyc"].get(worker["id"])
        _audit("LOGIN", worker["id"])
        return {
            "success": True, "action": "LOGIN",
            "token": token,
            "worker": _safe_worker(worker),
            "kyc_status": kyc["status"] if kyc else "NOT_SUBMITTED",
            "policy": DB["policies"].get(worker["id"]),
        }
    # New user → needs registration
    return {"success": True, "action": "REGISTER", "phone": phone,
            "message": "OTP verified. Complete registration."}


class RegisterReq(BaseModel):
    phone: str
    name: str
    city: str
    zone: str
    vehicle_type: str
    platform: str = "Swiggy"
    plan: str = "Standard"

@app.post("/api/auth/register")
def register(req: RegisterReq):
    phone = req.phone.replace("+91","").replace(" ","")
    # OTP must be verified first
    otp_rec = DB["otp_store"].get(phone)
    if not otp_rec or not otp_rec.get("verified"):
        raise HTTPException(400, "Phone not verified. Please complete OTP verification first.")
    if phone in DB["users"]:
        raise HTTPException(400, "Account already exists. Please login.")

    wid  = _next("user")
    now  = _now()
    p    = calc_premium(req.plan, req.zone, req.city)
    risk = compute_risk_score(req.zone, req.city, req.vehicle_type, req.platform)
    pred = predict_earnings(req.city, req.zone)

    DB["workers"][wid] = {
        "id": wid, "name": req.name, "phone": phone,
        "city": req.city, "zone": req.zone,
        "vehicle_type": req.vehicle_type, "platform": req.platform,
        "weeks_clean": 0, "created_at": now, "status": "ACTIVE",
    }
    DB["users"][phone] = {"worker_id": wid, "phone": phone, "created_at": now}
    DB["kyc"][wid] = {
        "worker_id": wid, "status": "PENDING",
        "full_name": req.name, "phone": phone,
        "aadhaar_last4": None, "pan": None,
        "submitted_at": None, "verified_at": None,
        "rejection_reason": None,
    }
    DB["policies"][wid] = {
        "worker_id": wid, "plan": req.plan, "status": "PENDING_KYC",
        "base_premium": p["base_premium"], "dynamic_premium": p["dynamic_premium"],
        "weekly_cap": p["weekly_cap"], "max_disruptions": p["max_disruptions"],
        "predicted_earnings": pred, "risk_score": risk["score"],
        "risk_level": risk["level"], "zone_risk": p["zone_risk"],
        "seasonal_factor": p["seasonal_factor"],
        "loyalty_discount": p["loyalty_discount"],
        "week_start": now, "created_at": now,
    }

    token = _create_token({"worker_id": wid, "phone": phone})
    DB["sessions"][token] = wid
    _audit("REGISTER", wid, f"Plan={req.plan} City={req.city}")

    return {
        "success": True, "token": token,
        "worker": _safe_worker(DB["workers"][wid]),
        "kyc_status": "PENDING",
        "policy": DB["policies"][wid],
        "message": f"Welcome to GigShield, {req.name}! Complete KYC to activate coverage.",
    }


@app.post("/api/auth/logout")
def logout(worker=Depends(get_current_worker),
           creds: HTTPAuthorizationCredentials = Depends(security)):
    token = creds.credentials
    DB["sessions"].pop(token, None)
    _audit("LOGOUT", worker["id"])
    return {"success": True, "message": "Logged out successfully"}


# ── KYC ───────────────────────────────────────────────────────────

class KYCSubmitReq(BaseModel):
    full_name: str
    aadhaar_last4: str
    pan: str

    @field_validator("aadhaar_last4")
    @classmethod
    def validate_aadhaar(cls, v):
        if not v.isdigit() or len(v) != 4:
            raise ValueError("Enter last 4 digits of Aadhaar")
        return v

    @field_validator("pan")
    @classmethod
    def validate_pan(cls, v):
        v = v.upper().strip()
        if len(v) != 10:
            raise ValueError("PAN must be 10 characters")
        return v

@app.post("/api/kyc/submit")
def submit_kyc(req: KYCSubmitReq, worker=Depends(get_current_worker)):
    kyc = DB["kyc"].get(worker["id"])
    if not kyc:
        raise HTTPException(404, "KYC record not found")
    if kyc["status"] == "VERIFIED":
        raise HTTPException(400, "KYC already verified")

    DB["kyc"][worker["id"]].update({
        "full_name": req.full_name,
        "aadhaar_last4": req.aadhaar_last4,
        "pan": req.pan,
        "status": "UNDER_REVIEW",
        "submitted_at": _now(),
    })

    # Auto-approve for demo (simulate instant verification)
    # In production: send to manual review or ID verification API
    _auto_verify_kyc(worker["id"])
    _audit("KYC_SUBMIT", worker["id"])

    return {
        "success": True,
        "kyc_status": DB["kyc"][worker["id"]]["status"],
        "message": "KYC submitted and verified successfully!",
        "policy_status": DB["policies"].get(worker["id"], {}).get("status"),
    }

def _auto_verify_kyc(worker_id: int):
    """Simulate instant KYC verification for demo. In prod: manual review."""
    DB["kyc"][worker_id]["status"]      = "VERIFIED"
    DB["kyc"][worker_id]["verified_at"] = _now()
    # Activate policy
    if worker_id in DB["policies"]:
        DB["policies"][worker_id]["status"]     = "ACTIVE"
        DB["policies"][worker_id]["week_start"] = _now()
    _audit("KYC_VERIFIED", worker_id)

@app.get("/api/kyc/status")
def kyc_status(worker=Depends(get_current_worker)):
    kyc = DB["kyc"].get(worker["id"])
    if not kyc: raise HTTPException(404, "KYC not found")
    return kyc

@app.get("/api/kyc/list")          # Admin endpoint
def kyc_list(admin: str = ""):
    if admin != ADMIN_TOKEN:
        raise HTTPException(403, "Admin access required")
    return list(DB["kyc"].values())

@app.post("/api/kyc/review")       # Admin approve/reject
def kyc_review(data: dict, admin: str = ""):
    if admin != ADMIN_TOKEN:
        raise HTTPException(403, "Admin access required")
    wid    = data.get("worker_id")
    action = data.get("action")   # "APPROVE" or "REJECT"
    reason = data.get("reason", "")
    if wid not in DB["kyc"]:
        raise HTTPException(404, "KYC record not found")
    if action == "APPROVE":
        _auto_verify_kyc(wid)
    elif action == "REJECT":
        DB["kyc"][wid]["status"]           = "REJECTED"
        DB["kyc"][wid]["rejection_reason"] = reason
        if wid in DB["policies"]:
            DB["policies"][wid]["status"]  = "SUSPENDED"
        _audit("KYC_REJECTED", wid, reason)
    return {"success": True, "kyc_status": DB["kyc"][wid]["status"]}


# ── WORKER / POLICY ───────────────────────────────────────────────

def _safe_worker(w):
    return {k: v for k, v in w.items() if k not in ("password_hash",)}

@app.get("/api/worker/me")
def get_me(worker=Depends(get_current_worker)):
    kyc    = DB["kyc"].get(worker["id"])
    policy = DB["policies"].get(worker["id"])
    return {
        "worker": _safe_worker(worker),
        "kyc": kyc,
        "policy": policy,
        "week_claims": _week_claims_summary(worker["id"]),
    }

@app.get("/api/policy/me")
def get_my_policy(worker=Depends(get_current_worker)):
    p = DB["policies"].get(worker["id"])
    if not p: raise HTTPException(404, "No policy found")
    return p

@app.get("/api/workers")           # Admin
def list_workers(admin: str = ""):
    if admin != ADMIN_TOKEN:
        raise HTTPException(403, "Admin access required")
    result = []
    for w in DB["workers"].values():
        result.append({
            **_safe_worker(w),
            "kyc_status": DB["kyc"].get(w["id"],{}).get("status","NOT_SUBMITTED"),
            "policy_status": DB["policies"].get(w["id"],{}).get("status","NONE"),
            "total_claims": sum(1 for c in DB["claims"].values() if c["worker_id"]==w["id"]),
        })
    return result


# ── WEATHER ───────────────────────────────────────────────────────

@app.get("/api/weather/{city}")
async def get_weather(city: str):
    w = await fetch_weather(city)
    t = evaluate_triggers(w["rain_mm"], w["temp_celsius"], w["aqi"])
    return {**w, "city": city, "active_triggers": t, "fetched_at": _now()}


# ── SHIFTS ────────────────────────────────────────────────────────

@app.post("/api/shifts/start")
def start_shift(worker=Depends(require_kyc)):
    for s in DB["shifts"].values():
        if s["worker_id"] == worker["id"] and s["status"] == "ACTIVE":
            raise HTTPException(400, "Shift already active")
    pol = DB["policies"].get(worker["id"])
    if not pol or pol["status"] != "ACTIVE":
        raise HTTPException(403, "Policy not active. Complete KYC to start shifts.")
    sid = _next("shift")
    DB["shifts"][sid] = {
        "id": sid, "worker_id": worker["id"], "start_time": _now(),
        "end_time": None, "active_minutes": 0,
        "gps_points": 0, "avg_speed": 0,
        "status": "ACTIVE", "fraud_score": 0,
    }
    _audit("SHIFT_START", worker["id"])
    return {"success": True, "shift_id": sid, "message": "Shift started. Coverage active."}

@app.post("/api/shifts/end")
def end_shift(worker=Depends(require_kyc)):
    shift = next((s for s in DB["shifts"].values()
                  if s["worker_id"] == worker["id"] and s["status"] == "ACTIVE"), None)
    if not shift: raise HTTPException(404, "No active shift")
    dur = random.randint(180, 360)
    act = int(dur * random.uniform(0.55, 0.85))
    gps = random.randint(60, 200)
    spd = random.uniform(12, 35)
    fr  = compute_fraud_score(gps, act, spd, dur)
    shift.update({
        "end_time": _now(), "active_minutes": act,
        "gps_points": gps, "avg_speed": round(spd,1),
        "status": "COMPLETED", "fraud_score": fr["score"],
    })
    _audit("SHIFT_END", worker["id"], f"fraud={fr['score']} verdict={fr['verdict']}")
    return {"success": True, "shift_id": shift["id"],
            "duration_minutes": dur, "active_minutes": act,
            "fraud_score": fr["score"], "verdict": fr["verdict"]}

@app.get("/api/shifts/active")
def active_shift(worker=Depends(get_current_worker)):
    s = next((s for s in DB["shifts"].values()
               if s["worker_id"] == worker["id"] and s["status"] == "ACTIVE"), None)
    return {"active": bool(s), "shift": s}

@app.get("/api/shifts/history")
def shift_history(worker=Depends(get_current_worker)):
    shifts = [s for s in DB["shifts"].values() if s["worker_id"] == worker["id"]]
    return sorted(shifts, key=lambda x: x["start_time"], reverse=True)


# ── TRIGGERS / CLAIMS ─────────────────────────────────────────────

class TriggerReq(BaseModel):
    simulate_rain: Optional[float] = None
    simulate_temp: Optional[float] = None
    simulate_aqi:  Optional[int]   = None

@app.post("/api/triggers/fire")
async def fire_trigger(req: TriggerReq, worker=Depends(require_kyc)):
    pol = DB["policies"].get(worker["id"])
    if not pol or pol["status"] != "ACTIVE":
        raise HTTPException(403, "Policy not active")

    weather = await fetch_weather(worker["city"])
    if req.simulate_rain is not None: weather["rain_mm"]      = req.simulate_rain; weather["source"] = "simulated"
    if req.simulate_temp is not None: weather["temp_celsius"] = req.simulate_temp; weather["source"] = "simulated"
    if req.simulate_aqi  is not None: weather["aqi"]          = req.simulate_aqi;  weather["source"] = "simulated"

    active_triggers = evaluate_triggers(weather["rain_mm"], weather["temp_celsius"], weather["aqi"])
    if not active_triggers:
        return {"triggered": False, "weather": weather,
                "message": "No parametric threshold crossed."}

    shift = next((s for s in DB["shifts"].values()
                  if s["worker_id"] == worker["id"] and s["status"] == "ACTIVE"), None)
    if not shift:
        return {"triggered": True, "weather": weather, "active_triggers": active_triggers,
                "claim_generated": False,
                "message": "Trigger fired. Start a shift to be eligible for claims."}

    week_id   = _week_id()
    # Duplicate claim check — same trigger type this week
    existing_types = {c["trigger_type"] for c in DB["claims"].values()
                      if c["worker_id"] == worker["id"] and c["week_id"] == week_id
                      and c["status"] != "BLOCKED"}
    week_count = sum(1 for c in DB["claims"].values()
                     if c["worker_id"] == worker["id"] and c["week_id"] == week_id
                     and c["status"] in ("APPROVED","MANUAL_REVIEW","PAID"))
    week_paid  = sum(c["final_amount"] for c in DB["claims"].values()
                     if c["worker_id"] == worker["id"] and c["week_id"] == week_id
                     and c["status"] in ("APPROVED","MANUAL_REVIEW","PAID"))

    claims_created = []
    for trig in active_triggers:
        if week_count >= pol["max_disruptions"]: break
        if week_paid  >= pol["weekly_cap"]:      break
        if trig["type"] in existing_types:       continue   # duplicate prevention

        hours = 1.5 if trig["multiplier"] == 0.6 else (2.5 if trig["multiplier"] == 1.5 else 2.0)
        fr    = compute_fraud_score(
            shift["gps_points"] or 45, shift["active_minutes"] or 120,
            shift["avg_speed"] or 20, 180
        )
        raw       = round(0.5 * (pol["predicted_earnings"] / 6) * hours * trig["multiplier"], 2)
        remaining = pol["weekly_cap"] - week_paid
        final     = round(min(raw, remaining), 2)
        status    = ("BLOCKED"       if fr["verdict"] == "AUTO_REJECTED"
                     else "MANUAL_REVIEW" if fr["verdict"] == "MANUAL_REVIEW"
                     else "APPROVED")
        cid = _next("claim")
        DB["claims"][cid] = {
            "id": cid, "worker_id": worker["id"], "shift_id": shift["id"],
            "trigger_type": trig["type"], "trigger_value": trig["value"],
            "trigger_label": trig["label"],
            "severity_multiplier": trig["multiplier"],
            "hours_disrupted": hours, "base_amount": raw, "final_amount": final,
            "fraud_score": fr["score"], "fraud_flags": fr["flags"],
            "status": status, "week_id": week_id,
            "created_at": _now(), "approved_at": _now() if status == "APPROVED" else None,
            "paid_at": None,
        }
        week_paid  += final
        week_count += 1
        existing_types.add(trig["type"])
        claims_created.append(DB["claims"][cid])
        _audit("CLAIM_CREATED", worker["id"],
               f"type={trig['type']} amount={final} status={status}")

    return {
        "triggered": True, "weather": weather, "active_triggers": active_triggers,
        "claim_generated": bool(claims_created), "claims": claims_created,
        "message": f"{len(claims_created)} claim(s) generated.",
    }


# ── CLAIMS ────────────────────────────────────────────────────────

@app.get("/api/claims")
def get_my_claims(worker=Depends(get_current_worker)):
    claims = [c for c in DB["claims"].values() if c["worker_id"] == worker["id"]]
    return sorted(claims, key=lambda x: x["created_at"], reverse=True)

@app.get("/api/claims/week")
def get_week_claims(worker=Depends(get_current_worker)):
    wk     = _week_id()
    claims = [c for c in DB["claims"].values()
               if c["worker_id"] == worker["id"] and c["week_id"] == wk]
    total  = sum(c["final_amount"] for c in claims
                 if c["status"] in ("APPROVED","MANUAL_REVIEW","PAID"))
    return {"week_id": wk, "claims": sorted(claims, key=lambda x: x["created_at"], reverse=True),
            "week_total": total, "approved_count": sum(1 for c in claims if c["status"]=="APPROVED")}

@app.get("/api/claims/all")        # Admin
def all_claims(admin: str = ""):
    if admin != ADMIN_TOKEN:
        raise HTTPException(403, "Admin access required")
    claims = list(DB["claims"].values())
    for c in claims:
        w = DB["workers"].get(c["worker_id"], {})
        c["worker_name"] = w.get("name","Unknown")
        c["worker_phone"]= w.get("phone","")
    return sorted(claims, key=lambda x: x["created_at"], reverse=True)

@app.post("/api/claims/{claim_id}/review")  # Admin
def review_claim(claim_id: int, data: dict, admin: str = ""):
    if admin != ADMIN_TOKEN:
        raise HTTPException(403, "Admin access required")
    c = DB["claims"].get(claim_id)
    if not c: raise HTTPException(404, "Claim not found")
    action = data.get("action")
    if action == "APPROVE":
        c["status"] = "APPROVED"; c["approved_at"] = _now()
        _audit("CLAIM_APPROVED", c["worker_id"], f"claim={claim_id}")
    elif action == "REJECT":
        c["status"] = "BLOCKED"
        _audit("CLAIM_REJECTED", c["worker_id"], f"claim={claim_id}")
    return {"success": True, "claim": c}


# ── PAYOUT ────────────────────────────────────────────────────────

@app.post("/api/payout/run")
def run_payout(data: dict = None, worker=Depends(get_current_worker)):
    wk     = _week_id()
    claims = [c for c in DB["claims"].values()
               if c["worker_id"] == worker["id"]
               and c["week_id"] == wk
               and c["status"] == "APPROVED"]
    if not claims:
        return {"processed": False, "message": "No approved claims this week."}
    pol   = DB["policies"].get(worker["id"], {})
    cap   = pol.get("weekly_cap", 200.0)
    total = sum(c["final_amount"] for c in claims)
    final = round(min(total, cap), 2)
    ref   = _upi()
    pid   = _next("payout")
    DB["payouts"][pid] = {
        "id": pid, "worker_id": worker["id"], "week_id": wk,
        "total_claimed": total, "cap_applied": cap, "final_payout": final,
        "claims_count": len(claims), "upi_ref": ref,
        "status": "PROCESSED", "created_at": _now(),
    }
    for c in claims:
        c["status"] = "PAID"; c["paid_at"] = _now()
    if not any(c["fraud_score"] > 40 for c in claims):
        DB["workers"][worker["id"]]["weeks_clean"] = DB["workers"][worker["id"]].get("weeks_clean",0) + 1
    _audit("PAYOUT_PROCESSED", worker["id"], f"amount={final} ref={ref}")
    return {"processed": True, "final_payout": final, "upi_ref": ref,
            "claims_count": len(claims), "message": f"₹{final:.0f} transferred via UPI · {ref}"}

@app.get("/api/payout/history")
def payout_history(worker=Depends(get_current_worker)):
    payouts = [p for p in DB["payouts"].values() if p["worker_id"] == worker["id"]]
    return sorted(payouts, key=lambda x: x["created_at"], reverse=True)

@app.post("/api/payout/run-all")   # Admin batch
def run_all_payouts(admin: str = ""):
    if admin != ADMIN_TOKEN:
        raise HTTPException(403, "Admin access required")
    wk, results = _week_id(), []
    for wid in list(DB["workers"].keys()):
        claims = [c for c in DB["claims"].values()
                   if c["worker_id"] == wid and c["week_id"] == wk and c["status"] == "APPROVED"]
        if not claims: continue
        cap   = DB["policies"].get(wid, {}).get("weekly_cap", 200.0)
        total = sum(c["final_amount"] for c in claims)
        final = round(min(total, cap), 2)
        ref   = _upi()
        pid   = _next("payout")
        DB["payouts"][pid] = {
            "id": pid, "worker_id": wid, "week_id": wk,
            "total_claimed": total, "cap_applied": cap, "final_payout": final,
            "claims_count": len(claims), "upi_ref": ref,
            "status": "PROCESSED", "created_at": _now(),
        }
        for c in claims: c["status"] = "PAID"; c["paid_at"] = _now()
        results.append({"worker_id": wid, "final_payout": final, "upi_ref": ref})
    return {"processed": len(results), "week_id": wk, "payouts": results}


# ── AUDIT / ADMIN ─────────────────────────────────────────────────

@app.get("/api/admin/audit")
def audit_log(admin: str = "", limit: int = 100):
    if admin != ADMIN_TOKEN:
        raise HTTPException(403, "Admin access required")
    return DB["audit_log"][-limit:]

@app.get("/api/admin/stats")
def admin_stats(admin: str = ""):
    if admin != ADMIN_TOKEN:
        raise HTTPException(403, "Admin access required")
    total_premium = sum(
        p.get("dynamic_premium", 0) for p in DB["policies"].values()
        if p.get("status") == "ACTIVE"
    )
    total_payouts = sum(p.get("final_payout", 0) for p in DB["payouts"].values())
    return {
        "total_workers": len(DB["workers"]),
        "kyc_verified": sum(1 for k in DB["kyc"].values() if k["status"]=="VERIFIED"),
        "kyc_pending": sum(1 for k in DB["kyc"].values() if k["status"] in ("PENDING","UNDER_REVIEW")),
        "active_policies": sum(1 for p in DB["policies"].values() if p["status"]=="ACTIVE"),
        "total_claims": len(DB["claims"]),
        "approved_claims": sum(1 for c in DB["claims"].values() if c["status"]=="APPROVED"),
        "paid_claims": sum(1 for c in DB["claims"].values() if c["status"]=="PAID"),
        "blocked_claims": sum(1 for c in DB["claims"].values() if c["status"]=="BLOCKED"),
        "total_premium_pool": round(total_premium, 2),
        "total_payouts_disbursed": round(total_payouts, 2),
        "loss_ratio": round(total_payouts / max(total_premium * 10, 1) * 100, 1),
    }


# ── HELPERS ───────────────────────────────────────────────────────

def _week_claims_summary(worker_id):
    wk     = _week_id()
    claims = [c for c in DB["claims"].values()
               if c["worker_id"] == worker_id and c["week_id"] == wk]
    total  = sum(c["final_amount"] for c in claims
                 if c["status"] in ("APPROVED","MANUAL_REVIEW","PAID"))
    return {"week_id": wk, "count": len(claims), "total": total,
            "approved": sum(1 for c in claims if c["status"]=="APPROVED")}


# ── MANGUM HANDLER ───────────────────────────────────────────────
handler = Mangum(app, lifespan="off")
