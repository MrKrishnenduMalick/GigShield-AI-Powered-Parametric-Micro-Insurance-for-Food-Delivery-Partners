"""
GigShield AI Service — FastAPI Backend v3.2
FIXED: Dynamic overlap hours + correct record creation in all paths
Aligned with GigShield PDF (Guidewire DEVTrails 2026)
"""

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import List, Optional
import math
from datetime import datetime, timezone, date
import random

app = FastAPI(title="GigShield AI Service", version="3.2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ═══════════════════════════════════════════════════════════════════
#  EARNINGS PREDICTION HELPERS
# ═══════════════════════════════════════════════════════════════════

def generate_mock_earnings_history(base: float = 650.0, days: int = 14) -> List[dict]:
    history = []
    today = date.today()
    for i in range(days, 0, -1):
        import datetime as _dt
        d = today - _dt.timedelta(days=i)
        dow = d.weekday()
        disrupted = random.random() < 0.08
        earnings = 0.0 if disrupted else base * (1.15 if dow >= 4 else 1.0) * random.uniform(0.85, 1.15)
        history.append({
            "date": d.isoformat(),
            "earnings": round(earnings, 2),
            "disrupted": disrupted,
            "day_of_week": dow,
        })
    return history


def predict_weekly_earnings(history: List[dict]) -> dict:
    if not history:
        default = random.uniform(3000, 4000)
        return {"predicted": round(default, 2), "method": "default_range"}

    clean = [h for h in history if not h.get("disrupted", False)]
    if not clean:
        clean = history

    if len(clean) < 5:
        weights = [0.55, 0.20, 0.10, 0.08, 0.07]
        recent = [h.get("earnings", 0) for h in clean[:5]]
        while len(recent) < 5:
            recent.append(recent[-1] if recent else 550)
        predicted_daily = sum(w * e for w, e in zip(weights, recent))
        if date.today().weekday() >= 4:
            predicted_daily *= 1.15
        return {"predicted": round(predicted_daily * 6, 2), "method": "weighted_moving_average"}

    last7 = clean[-7:]
    avg_daily = sum(h.get("earnings", 0) for h in last7) / len(last7)
    predicted_weekly = avg_daily * 6
    return {"predicted": round(predicted_weekly, 2), "method": "moving_average"}


# ═══════════════════════════════════════════════════════════════════
#  IN-MEMORY DATA STORE
# ═══════════════════════════════════════════════════════════════════

def _make_user(base_daily: float, plan: str) -> dict:
    plan_caps = {"basic": 150.0, "standard": 200.0, "premium": 300.0}
    history = generate_mock_earnings_history(base=base_daily)
    pred = predict_weekly_earnings(history)
    return {
        "predicted_income": pred["predicted"],
        "prediction_method": pred["method"],
        "plan": plan,
        "weekly_cap": plan_caps[plan],
        "active_policy": True,
        "earnings_history": history,
    }

users: dict = {
    "raju":  _make_user(633.0, "standard"),
    "priya": _make_user(700.0, "premium"),
    "demo":  _make_user(500.0, "basic"),
}

claims: list = []
weekly_totals: dict = {}

# ═══════════════════════════════════════════════════════════════════
#  CONSTANTS
# ═══════════════════════════════════════════════════════════════════

RAIN_THRESHOLD_MM = 80.0
AQI_THRESHOLD     = 300
MIN_WORKING_HOURS = 6
MIN_GPS_POINTS    = 5
GLOBAL_WEEKLY_CAP = 200.0

SEVERITY: dict = {
    "HEAVY_RAIN":   1.0,
    "EXTREME_HEAT": 1.0,
    "SEVERE_AQI":   1.0,
    "URBAN_FLOOD":  1.5,
    "CURFEW":       1.5,
}

DISRUPTION_LABELS = {
    "HEAVY_RAIN":   "Heavy Rain",
    "EXTREME_HEAT": "Extreme Heat",
    "SEVERE_AQI":   "Severe Air Quality",
    "URBAN_FLOOD":  "Urban Flood",
    "CURFEW":       "Curfew / Section 144",
}

# ═══════════════════════════════════════════════════════════════════
#  PYDANTIC MODELS
# ═══════════════════════════════════════════════════════════════════

class GpsPoint(BaseModel):
    lat: float
    lng: float
    speed: Optional[float] = None
    accuracy: Optional[float] = None
    timestamp: Optional[str] = None

class TriggerRequest(BaseModel):
    username: str
    working_hours: float
    rain: Optional[float] = None
    aqi: Optional[int] = None
    flood: Optional[bool] = False
    gps_points: Optional[List[GpsPoint]] = None
    movement: Optional[bool] = None

class PredictRequest(BaseModel):
    user_id: int
    earnings_history: List[dict]

class RiskScoreRequest(BaseModel):
    city: str
    zone: str
    vehicle_type: str
    plan_name: str

class RegisterUserRequest(BaseModel):
    username: str
    predicted_income: Optional[float] = None
    plan: str
    base_daily_earnings: Optional[float] = None

class AdminTriggerRequest(BaseModel):
    type: str

class AdminClaimAction(BaseModel):
    reason: Optional[str] = "Manual action"

# ═══════════════════════════════════════════════════════════════════
#  GPS FRAUD DETECTION ENGINE
# ═══════════════════════════════════════════════════════════════════

def haversine_km(lat1, lng1, lat2, lng2) -> float:
    R = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lng2 - lng1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlam/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))


def calculate_fraud_score(gps_points: List[GpsPoint]) -> dict:
    score = 0
    flags = []
    n = len(gps_points)

    if n < 10:
        score += 30
        flags.append("LOW_COVERAGE")

    if n > 0:
        spoofed = [p for p in gps_points if p.accuracy is not None and p.accuracy == 0]
        if len(spoofed) / n > 0.8:
            score += 35
            flags.append("GPS_SPOOFING")

    speed_violations = [p for p in gps_points if p.speed is not None and p.speed > 120]
    if speed_violations:
        score += min(20 * len(speed_violations), 40)
        flags.append("IMPOSSIBLE_SPEED")

    if n >= 2:
        jumps = sum(
            1 for i in range(1, n)
            if haversine_km(gps_points[i-1].lat, gps_points[i-1].lng,
                            gps_points[i].lat,   gps_points[i].lng) > 5.0
        )
        if jumps > 0:
            score += min(20 * jumps, 40)
            if "IMPOSSIBLE_SPEED" not in flags:
                flags.append("IMPOSSIBLE_SPEED")

    if n >= MIN_GPS_POINTS:
        stationary = sum(
            1 for i in range(1, n)
            if abs(gps_points[i].lat - gps_points[i-1].lat) < 0.00005
            and abs(gps_points[i].lng - gps_points[i-1].lng) < 0.00005
        )
        if stationary / (n - 1) > 0.85:
            score += 25
            flags.append("LOW_ACTIVITY")

    final = min(score, 100)
    decision = "APPROVE" if final < 40 else "REVIEW" if final < 70 else "REJECT"

    explanations = []
    if "LOW_COVERAGE" in flags:
        explanations.append("Insufficient GPS data points recorded during shift")
    if "GPS_SPOOFING" in flags:
        explanations.append("GPS coordinates appear artificially generated")
    if "IMPOSSIBLE_SPEED" in flags:
        explanations.append("Movement speed exceeds physically possible limits")
    if "LOW_ACTIVITY" in flags:
        explanations.append("Worker appears stationary — no genuine movement detected")
    if not flags:
        explanations.append("All GPS checks passed — genuine activity confirmed")

    return {
        "score": final,
        "flags": flags,
        "decision": decision,
        "explanation": explanations,
        "summary": f"Fraud Score: {final}/100 — {decision}",
    }


def resolve_fraud(gps_points: Optional[List[GpsPoint]], movement: Optional[bool]) -> dict:
    if gps_points and len(gps_points) >= 1:
        return calculate_fraud_score(gps_points)
    if movement is not None:
        return gps_from_movement_flag(movement)
    return {
        "score": 55,
        "flags": ["LOW_COVERAGE", "NO_MOVEMENT"],
        "decision": "REVIEW",
        "explanation": ["No GPS data or movement flag provided"],
        "summary": "Fraud Score: 55/100 — REVIEW",
    }


def gps_from_movement_flag(movement: Optional[bool]) -> dict:
    if movement is True:
        return {
            "score": 0,
            "flags": [],
            "decision": "APPROVE",
            "explanation": ["Movement flag confirmed — worker active during shift"],
            "summary": "Fraud Score: 0/100 — APPROVE",
        }
    return {
        "score": 55,
        "flags": ["LOW_ACTIVITY", "NO_MOVEMENT"],
        "decision": "REVIEW",
        "explanation": ["No movement detected — worker may be stationary"],
        "summary": "Fraud Score: 55/100 — REVIEW",
    }


# ═══════════════════════════════════════════════════════════════════
#  PAYOUT FORMULA
# ═══════════════════════════════════════════════════════════════════

def calculate_payout(predicted_weekly: float, overlap_hours: float, disruption_type: str) -> float:
    M = SEVERITY.get(disruption_type, 1.0)
    daily_slice = predicted_weekly / 6
    return round(0.5 * daily_slice * overlap_hours * M, 2)


# ═══════════════════════════════════════════════════════════════════
#  EVENT DETECTION
# ═══════════════════════════════════════════════════════════════════

def detect_event_type(rain, aqi, flood) -> Optional[str]:
    if flood:
        return "URBAN_FLOOD"
    if rain is not None and rain >= RAIN_THRESHOLD_MM:
        return "HEAVY_RAIN"
    if aqi is not None and aqi >= AQI_THRESHOLD:
        return "SEVERE_AQI"
    return None


# ═══════════════════════════════════════════════════════════════════
#  CLAIM VALIDATION PIPELINE
# ═══════════════════════════════════════════════════════════════════

def today_str() -> str:
    return date.today().isoformat()


def validate_claim(username: str, event_type: str, working_hours: float, fraud: dict) -> dict:
    user = users.get(username)
    if user is None or not user.get("active_policy"):
        return {"valid": False, "status": "rejected", "message": "Rejected: No active insurance policy for this week"}

    if working_hours < MIN_WORKING_HOURS:
        return {"valid": False, "status": "rejected", "message": f"Rejected: Minimum {MIN_WORKING_HOURS} working hours required"}

    if "NO_MOVEMENT" in fraud["flags"] or ("LOW_ACTIVITY" in fraud["flags"] and fraud["score"] >= 40):
        return {"valid": False, "status": "rejected", "message": "Rejected: No active movement detected"}

    if "IMPOSSIBLE_SPEED" in fraud["flags"] or "GPS_SPOOFING" in fraud["flags"]:
        return {"valid": False, "status": "rejected", "message": "Rejected: Suspicious GPS activity detected"}

    if fraud["decision"] == "REJECT":
        return {"valid": False, "status": "rejected", "message": "Rejected: Suspicious GPS activity detected"}

    today = today_str()
    duplicate = any(
        c["username"] == username
        and c["event_type"] == event_type
        and c.get("claim_date") == today
        for c in claims
    )
    if duplicate:
        return {"valid": False, "status": "duplicate", "message": "Rejected: Duplicate claim"}

    accumulated = weekly_totals.get(username, 0.0)
    cap = user.get("weekly_cap", GLOBAL_WEEKLY_CAP)
    if accumulated >= cap:
        return {"valid": False, "status": "rejected", "message": f"Rejected: Weekly payout cap of Rs.{cap:.0f} already reached"}

    if fraud["decision"] == "REVIEW":
        return {"valid": True, "status": "review", "message": "Claim under review — will be verified before Friday payout"}

    return {"valid": True, "status": "approved", "message": ""}


# ═══════════════════════════════════════════════════════════════════
#  FIXED TRIGGER ENDPOINT (Dynamic Payout + Safe Record Creation)
# ═══════════════════════════════════════════════════════════════════

@app.post("/trigger")
def trigger_claim(req: TriggerRequest):
    username = req.username.lower().strip()

    event_type = detect_event_type(req.rain, req.aqi, req.flood)
    if event_type is None:
        raise HTTPException(
            status_code=400,
            detail="No parametric threshold met. Provide rain ≥ 80 mm/hr, AQI ≥ 300, or flood=true."
        )

    fraud = resolve_fraud(req.gps_points, req.movement)
    validation = validate_claim(username, event_type, req.working_hours, fraud)

    user = users.get(username, {})
    predicted_income = user.get("predicted_income", 3000.0)
    weekly_cap = user.get("weekly_cap", GLOBAL_WEEKLY_CAP)
    timestamp = datetime.now(timezone.utc).isoformat()

    # ====================== DYNAMIC OVERLAP (FIXED) ======================
    overlap_hours = round(req.working_hours * 0.6, 1)
    overlap_hours = max(0.5, min(overlap_hours, 4.0))
    payout_raw = calculate_payout(predicted_income, overlap_hours, event_type)
    accumulated = weekly_totals.get(username, 0.0)
    payout_capped = round(min(payout_raw, weekly_cap - accumulated), 2)
    # =====================================================================

    common = {
        "username": username,
        "event_type": event_type,
        "working_hours": req.working_hours,
        "overlap_hours": overlap_hours,
        "raw_payout": payout_raw,
        "fraud_score": fraud["score"],
        "fraud_flags": fraud["flags"],
        "fraud_decision": fraud["decision"],
        "fraud_explanation": fraud["explanation"],
        "claim_date": today_str(),
        "timestamp": timestamp,
    }

    if not validation["valid"]:
        record = {**common, "payout_amount": 0.0, "status": validation["status"], "message": validation["message"]}
        claims.append(record)
        return {
            "success": True,
            "claim": {
                **record,
                "event_label": DISRUPTION_LABELS.get(event_type, event_type),
            }
        }

    if validation["status"] == "review":
        record = {**common, "payout_amount": payout_capped, "status": "review", "message": "Claim under review — will be verified before Friday payout"}
        claims.append(record)
        return {
            "success": True,
            "claim": {
                **record,
                "event_label": DISRUPTION_LABELS.get(event_type, event_type),
            }
        }

    # APPROVED PATH
    record = {**common, "payout_amount": payout_capped, "status": "approved", "message": "Claim approved. Paid Friday."}
    claims.append(record)
    weekly_totals[username] = accumulated + payout_capped

    return {
        "success": True,
        "claim": {
            **record,
            "event_label": DISRUPTION_LABELS.get(event_type, event_type),
            "weekly_total": round(weekly_totals[username], 2),
            "weekly_cap": weekly_cap,
        }
    }


# ═══════════════════════════════════════════════════════════════════
#  ALL OTHER ENDPOINTS (exactly as in your original file)
# ═══════════════════════════════════════════════════════════════════

@app.post("/api/users/register")
def register_user(req: RegisterUserRequest):
    plan_caps = {"basic": 150.0, "standard": 200.0, "premium": 300.0}
    plan = req.plan.lower()
    if plan not in plan_caps:
        raise HTTPException(status_code=400, detail="Invalid plan. Choose basic | standard | premium")

    base_daily = req.base_daily_earnings or 633.0
    history = generate_mock_earnings_history(base=base_daily)
    pred = predict_weekly_earnings(history)

    predicted = req.predicted_income if req.predicted_income else pred["predicted"]

    users[req.username.lower()] = {
        "predicted_income": predicted,
        "prediction_method": pred["method"],
        "plan": plan,
        "weekly_cap": plan_caps[plan],
        "active_policy": True,
        "earnings_history": history,
    }
    return {
        "success": True,
        "message": f"User {req.username} registered on {plan} plan.",
        "predicted_income": predicted,
        "prediction_method": pred["method"],
        "weekly_cap": plan_caps[plan],
    }

@app.get("/api/users/{username}")
def get_user(username: str):
    user = users.get(username.lower())
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return {"success": True, "data": {k: v for k, v in user.items() if k != "earnings_history"}}

@app.get("/api/claims/{username}")
def get_claims_by_user(username: str):
    uname = username.lower()
    user_claims = [c for c in claims if c["username"] == uname]
    return {
        "success": True,
        "username": uname,
        "data": user_claims,
        "weekly_total": round(weekly_totals.get(uname, 0.0), 2),
        "count": len(user_claims),
    }

@app.get("/api/claims")
def get_all_claims():
    return {"success": True, "data": claims, "total": len(claims)}

@app.post("/api/analyze-gps")
def analyze_gps(points: List[GpsPoint]):
    result = calculate_fraud_score(points)
    return {"success": True, "data": result}

@app.post("/api/predict-earnings")
def predict_earnings_endpoint(req: PredictRequest):
    result = predict_weekly_earnings(req.earnings_history)
    return {"success": True, "data": result}

@app.post("/api/risk-score")
def risk_score(req: RiskScoreRequest):
    zone_risk = {
        "bandra": 0.85, "andheri": 0.75, "salt_lake": 0.70,
        "t_nagar": 0.60, "koramangala": 0.45, "connaught_place": 0.35,
        "sector_18": 0.40, "jubilee_hills": 0.30,
    }.get(req.zone.lower().replace(" ", "_"), 0.50)

    vehicle_risk = {"bicycle": 0.80, "motorcycle": 0.60, "ev": 0.55}.get(
        req.vehicle_type.lower(), 0.60)

    base = {"basic": 20, "standard": 30, "premium": 50}.get(req.plan_name.lower(), 30)
    dynamic = base * (1 + zone_risk * 0.3) * (1 + vehicle_risk * 0.15)

    return {"success": True, "data": {
        "base_premium": base,
        "dynamic_premium": round(dynamic, 2),
        "zone_risk": zone_risk,
        "vehicle_risk": vehicle_risk,
    }}

@app.get("/admin/stats")
def admin_stats():
    approved = [c for c in claims if c["status"] == "approved"]
    return {
        "totalUsers": len(users),
        "activeShifts": 1,
        "totalClaims": len(claims),
        "pendingReview": len([c for c in claims if c["status"] == "review"]),
        "totalPaid": round(sum(c["payout_amount"] for c in approved), 2),
    }

@app.get("/admin/users")
def admin_users():
    result = []
    for i, (uname, u) in enumerate(users.items()):
        result.append({
            "id": i + 1,
            "phone": uname,
            "name": uname.title(),
            "city": "Mumbai",
            "vehicleType": "Motorcycle",
            "kycDone": True,
            "plan": u.get("plan"),
            "predicted_income": u.get("predicted_income"),
        })
    return result

@app.get("/admin/shifts")
def admin_shifts():
    return [{"id": 1, "userId": "raju", "startTime": datetime.now(timezone.utc).isoformat(),
             "endTime": None, "totalActiveMinutes": 240, "active": True}]

@app.get("/admin/claims")
def admin_claims():
    result = []
    for i, c in enumerate(claims):
        result.append({
            "id": i + 1,
            "userId": c["username"],
            "disruptionType": c["event_type"],
            "payoutAmount": c["payout_amount"],
            "fraudScore": c["fraud_score"],
            "status": c["status"].upper(),
            "createdAt": c["timestamp"],
        })
    return result

@app.post("/admin/claims/{claim_id}/approve")
def admin_approve_claim(claim_id: int):
    idx = claim_id - 1
    if idx < 0 or idx >= len(claims):
        raise HTTPException(status_code=404, detail="Claim not found")
    claims[idx]["status"] = "approved"
    username = claims[idx]["username"]
    weekly_totals[username] = weekly_totals.get(username, 0.0) + claims[idx]["payout_amount"]
    return {"success": True, "message": "Claim approved"}

@app.post("/admin/claims/{claim_id}/reject")
def admin_reject_claim(claim_id: int, body: AdminClaimAction):
    idx = claim_id - 1
    if idx < 0 or idx >= len(claims):
        raise HTTPException(status_code=404, detail="Claim not found")
    claims[idx]["status"] = "rejected"
    claims[idx]["message"] = f"Rejected: {body.reason}"
    return {"success": True, "message": "Claim rejected"}

@app.post("/admin/simulate-trigger")
def admin_simulate_trigger(req: AdminTriggerRequest):
    triggered = 0
    for username in list(users.keys()):
        user = users[username]
        if not user.get("active_policy"):
            continue
        fraud = {"score": 0, "flags": [], "decision": "APPROVE", "explanation": ["Simulation"], "summary": "APPROVE"}
        validation = validate_claim(username, req.type, 7, fraud)
        predicted_income = user.get("predicted_income", 3000.0)
        weekly_cap = user.get("weekly_cap", GLOBAL_WEEKLY_CAP)
        overlap_hours = 1.5
        payout_raw = calculate_payout(predicted_income, overlap_hours, req.type)
        accumulated = weekly_totals.get(username, 0.0)
        payout_capped = round(min(payout_raw, weekly_cap - accumulated), 2)
        timestamp = datetime.now(timezone.utc).isoformat()

        status = validation["status"] if not validation["valid"] else "approved"
        record = {
            "username": username,
            "event_type": req.type,
            "payout_amount": payout_capped if status == "approved" else 0.0,
            "status": status,
            "message": validation["message"] if not validation["valid"] else "Claim approved. Paid Friday.",
            "fraud_score": 0,
            "fraud_flags": [],
            "fraud_decision": "APPROVE",
            "fraud_explanation": ["Simulation trigger"],
            "working_hours": 7,
            "claim_date": today_str(),
            "timestamp": timestamp,
        }
        claims.append(record)
        if status == "approved":
            weekly_totals[username] = accumulated + payout_capped
            triggered += 1
    return {"success": True, "message": f"Trigger fired for {triggered} workers."}

@app.post("/api/weekly-reset")
def weekly_reset():
    weekly_totals.clear()
    claims.clear()
    return {"success": True, "message": "Weekly totals and claims reset for new insurance week."}

@app.get("/health")
def health():
    return {
        "status": "ok",
        "service": "GigShield AI",
        "version": "3.2.0",
        "registered_users": len(users),
        "total_claims": len(claims),
    }