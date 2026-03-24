"""
GigShield API — Vercel Serverless (FastAPI + Mangum)
All routes in one file for Vercel Python runtime.
"""

import os, random, string
from datetime import datetime
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from mangum import Mangum
from pydantic import BaseModel
import httpx

# ── APP ──────────────────────────────────────────────────────────
app = FastAPI(title="GigShield API", version="2.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── IN-MEMORY STORE ──────────────────────────────────────────────
DB = {
    "workers":  {},
    "policies": {},
    "shifts":   {},
    "claims":   {},
    "payouts":  {},
}
_seq = {"worker": 0, "shift": 0, "claim": 0, "payout": 0}

def _next(kind):
    _seq[kind] += 1
    return _seq[kind]

def _now():
    return datetime.utcnow().isoformat()

def _week_id():
    return datetime.utcnow().strftime("%Y-W%U")

def _upi():
    return "UPI" + "".join(random.choices(string.digits, k=12))


# ── CONSTANTS ────────────────────────────────────────────────────
ZONE_RISK = {
    "Bandra": 1.30, "Andheri": 1.22, "Salt Lake": 1.18,
    "T Nagar": 1.12, "Koramangala": 1.08, "Sector 18": 1.05,
    "Connaught Place": 1.04, "Jubilee Hills": 1.02,
}
CITY_SEASONAL = {
    "Mumbai": 1.20, "Kolkata": 1.15, "Chennai": 1.10,
    "Delhi": 1.08, "Bangalore": 1.05, "Hyderabad": 1.04, "Noida": 1.06,
}
BASE_PREMIUM = {"Basic": 20.0, "Standard": 30.0, "Premium": 50.0}
WEEKLY_CAP   = {"Basic": 150.0, "Standard": 200.0, "Premium": 300.0}
VEHICLE_RISK = {"Bicycle": 25, "Motorcycle": 15, "Scooter": 18, "Car": 8}
PLATFORM_RISK= {"Zepto": 20, "Blinkit": 18, "Swiggy": 15, "Zomato": 15}

CITY_COORDS = {
    "Mumbai":    (19.0760, 72.8777),
    "Kolkata":   (22.5726, 88.3639),
    "Delhi":     (28.6139, 77.2090),
    "Chennai":   (13.0827, 80.2707),
    "Bangalore": (12.9716, 77.5946),
    "Hyderabad": (17.3850, 78.4867),
    "Noida":     (28.5355, 77.3910),
}

MOCK_WEATHER = {
    "Mumbai":    {"rain_mm": 12.0, "temp_celsius": 29.5, "aqi": 85,  "condition": "Partly Cloudy"},
    "Kolkata":   {"rain_mm": 8.0,  "temp_celsius": 31.0, "aqi": 120, "condition": "Humid"},
    "Delhi":     {"rain_mm": 0.0,  "temp_celsius": 38.0, "aqi": 210, "condition": "Hazy"},
    "Chennai":   {"rain_mm": 3.0,  "temp_celsius": 33.0, "aqi": 90,  "condition": "Sunny"},
    "Bangalore": {"rain_mm": 5.0,  "temp_celsius": 26.0, "aqi": 75,  "condition": "Cloudy"},
    "Hyderabad": {"rain_mm": 1.0,  "temp_celsius": 35.0, "aqi": 95,  "condition": "Clear"},
    "Noida":     {"rain_mm": 0.0,  "temp_celsius": 37.0, "aqi": 195, "condition": "Hazy"},
}


# ── SERVICES ─────────────────────────────────────────────────────

def compute_risk_score(zone, city, vehicle_type, platform,
                       rain_mm=0, temp_c=30, aqi=80):
    score = 0
    zr = ZONE_RISK.get(zone, 1.0)
    score += min(int((zr - 1.0) / 0.30 * 30), 30)
    cs = CITY_SEASONAL.get(city, 1.0)
    score += min(int((cs - 1.0) / 0.20 * 20), 20)
    ws = 0
    if rain_mm > 80:   ws = 20
    elif rain_mm > 40: ws = 12
    elif temp_c > 42:  ws = 18
    elif temp_c > 38:  ws = 10
    elif aqi > 300:    ws = 15
    elif aqi > 200:    ws = 8
    score += min(ws, 20)
    score += int(VEHICLE_RISK.get(vehicle_type, 15) * 15 / 25)
    score += int(PLATFORM_RISK.get(platform, 15) * 10 / 20)
    score = max(0, min(100, score))
    level = "LOW" if score < 35 else ("MEDIUM" if score < 65 else "HIGH")
    return {"score": score, "level": level}


def calc_premium(plan, zone, city, weeks_clean=0,
                 rain_mm=0, temp_c=30, aqi=80):
    base = BASE_PREMIUM.get(plan, 30.0)
    zr   = ZONE_RISK.get(zone, 1.0)
    sf   = CITY_SEASONAL.get(city, 1.0)
    if rain_mm > 80 or temp_c > 42 or aqi > 300:
        sf = min(sf * 1.1, 1.3)
    loyalty = min(weeks_clean * 0.5, 3.0) if weeks_clean >= 8 else 0.0
    dynamic = round(base * zr * sf - loyalty, 2)
    return {
        "base_premium": base, "zone_risk": zr,
        "seasonal_factor": round(sf, 3), "loyalty_discount": loyalty,
        "dynamic_premium": dynamic, "weekly_cap": WEEKLY_CAP.get(plan, 200.0)
    }


def predict_earnings(city, zone):
    base = 3500.0
    cm   = {"Mumbai":1.15,"Bangalore":1.10,"Delhi":1.08,
            "Chennai":1.05,"Kolkata":1.03,"Hyderabad":1.02}
    zr   = ZONE_RISK.get(zone, 1.0)
    return round(base * cm.get(city, 1.0) * (0.9 + zr * 0.1), 0)


def get_fraud_score(gps_points, active_minutes, avg_speed, duration_minutes):
    score, flags = 0, []
    coverage = gps_points / max(1, duration_minutes)
    if gps_points < 10:       score += 30; flags.append("LOW_GPS_COVERAGE")
    elif coverage < 0.3:      score += 20; flags.append("SPARSE_GPS")
    ratio = active_minutes / max(1, duration_minutes)
    if ratio < 0.10:          score += 30; flags.append("VERY_LOW_ACTIVITY")
    elif ratio < 0.20:        score += 20; flags.append("LOW_ACTIVITY")
    elif ratio < 0.30:        score += 10; flags.append("BELOW_AVG")
    if avg_speed > 120:       score += 25; flags.append("IMPOSSIBLE_SPEED")
    elif avg_speed < 0.5 and active_minutes > 30:
                              score += 20; flags.append("STATIONARY")
    score = max(0, min(100, score))
    verdict = ("AUTO_APPROVED" if score < 40
               else "MANUAL_REVIEW" if score < 70
               else "AUTO_REJECTED")
    return {"score": score, "verdict": verdict, "flags": flags}


def evaluate_triggers(rain_mm, temp_c, aqi):
    triggers = []
    if rain_mm > 150:
        triggers.append({"type":"URBAN_FLOOD",   "value":rain_mm, "multiplier":1.5, "label":"Urban Flooding"})
    elif rain_mm > 80:
        triggers.append({"type":"HEAVY_RAIN",    "value":rain_mm, "multiplier":1.0, "label":"Heavy Rainfall"})
    elif rain_mm > 40:
        triggers.append({"type":"MODERATE_RAIN", "value":rain_mm, "multiplier":0.6, "label":"Moderate Rain"})
    if temp_c > 42:
        triggers.append({"type":"EXTREME_HEAT",  "value":temp_c,  "multiplier":1.0, "label":"Extreme Heat"})
    if aqi > 300:
        triggers.append({"type":"SEVERE_AQI",    "value":aqi,     "multiplier":1.0, "label":"Severe AQI"})
    return triggers


async def fetch_weather(city: str):
    key = os.getenv("OWM_API_KEY", "")
    if key:
        coords = CITY_COORDS.get(city)
        if coords:
            try:
                async with httpx.AsyncClient(timeout=4.0) as client:
                    lat, lon = coords
                    r = await client.get(
                        "https://api.openweathermap.org/data/2.5/weather",
                        params={"lat": lat, "lon": lon, "appid": key, "units": "metric"}
                    )
                    if r.status_code == 200:
                        d    = r.json()
                        rain = d.get("rain", {}).get("1h", 0)
                        temp = d["main"]["temp"]
                        cond = d["weather"][0]["description"].title()
                        ar   = await client.get(
                            "https://api.openweathermap.org/data/2.5/air_pollution",
                            params={"lat": lat, "lon": lon, "appid": key}
                        )
                        aqi = 80
                        if ar.status_code == 200:
                            idx = ar.json()["list"][0]["main"]["aqi"]
                            aqi = {1:40,2:90,3:160,4:250,5:380}.get(idx, 80)
                        return {"rain_mm": round(rain,1), "temp_celsius": round(temp,1),
                                "aqi": aqi, "condition": cond, "source": "live"}
            except Exception:
                pass
    base = MOCK_WEATHER.get(city, {"rain_mm":5,"temp_celsius":30,"aqi":100,"condition":"Clear"})
    return {**base, "source": "mock"}


# ── ROUTES ───────────────────────────────────────────────────────

@app.get("/api/health")
def health():
    return {"status": "ok", "version": "2.0.0", "workers": len(DB["workers"])}


@app.get("/api/weather/{city}")
async def get_weather(city: str):
    w = await fetch_weather(city)
    t = evaluate_triggers(w["rain_mm"], w["temp_celsius"], w["aqi"])
    return {**w, "city": city, "active_triggers": t, "fetched_at": _now()}


# ── REGISTER ──
class WorkerIn(BaseModel):
    name: str
    phone: str
    city: str
    zone: str
    vehicle_type: str
    platform: str = "Swiggy"
    plan: str = "Standard"

@app.post("/api/register")
def register(data: WorkerIn):
    for w in DB["workers"].values():
        if w["phone"] == data.phone:
            raise HTTPException(400, "Phone already registered")
    wid  = _next("worker")
    DB["workers"][wid] = {
        "id": wid, "name": data.name, "phone": data.phone,
        "city": data.city, "zone": data.zone,
        "vehicle_type": data.vehicle_type, "platform": data.platform,
        "weeks_clean": 0, "created_at": _now()
    }
    p    = calc_premium(data.plan, data.zone, data.city)
    risk = compute_risk_score(data.zone, data.city, data.vehicle_type, data.platform)
    pred = predict_earnings(data.city, data.zone)
    DB["policies"][wid] = {
        "worker_id": wid, "plan": data.plan,
        "base_premium": p["base_premium"], "dynamic_premium": p["dynamic_premium"],
        "weekly_cap": p["weekly_cap"], "predicted_earnings": pred,
        "risk_score": risk["score"], "risk_level": risk["level"],
        "zone_risk": p["zone_risk"], "seasonal_factor": p["seasonal_factor"],
        "loyalty_discount": p["loyalty_discount"], "is_active": True,
        "week_start": _now()
    }
    return {
        "success": True, "worker_id": wid,
        "message": f"Welcome to GigShield, {data.name}! Your policy is active.",
        "policy": DB["policies"][wid]
    }

@app.get("/api/worker/{worker_id}")
def get_worker(worker_id: int):
    w = DB["workers"].get(worker_id)
    if not w: raise HTTPException(404, "Worker not found")
    return w

@app.get("/api/workers")
def list_workers():
    return list(DB["workers"].values())


# ── POLICY ──
@app.get("/api/policy/{worker_id}")
def get_policy(worker_id: int):
    if worker_id not in DB["workers"]:
        raise HTTPException(404, "Worker not found")
    p = DB["policies"].get(worker_id)
    if not p: raise HTTPException(404, "No policy found")
    w = DB["workers"][worker_id]
    return {**p, "name": w["name"], "city": w["city"], "zone": w["zone"],
            "vehicle_type": w["vehicle_type"], "platform": w["platform"]}

class PremiumReq(BaseModel):
    worker_id: int
    plan: str = "Standard"

@app.post("/api/calculate-premium")
async def calculate_premium_ep(req: PremiumReq):
    w = DB["workers"].get(req.worker_id)
    if not w: raise HTTPException(404, "Worker not found")
    weather = await fetch_weather(w["city"])
    p    = calc_premium(req.plan, w["zone"], w["city"], w["weeks_clean"],
                        weather["rain_mm"], weather["temp_celsius"], weather["aqi"])
    risk = compute_risk_score(w["zone"], w["city"], w["vehicle_type"], w["platform"],
                              weather["rain_mm"], weather["temp_celsius"], weather["aqi"])
    return {
        "worker_id": req.worker_id, "plan": req.plan,
        "premium": p, "risk": risk, "weather_context": weather,
        "formula": f"₹{p['base_premium']} × {p['zone_risk']} × {p['seasonal_factor']} − ₹{p['loyalty_discount']} = ₹{p['dynamic_premium']}"
    }


# ── SHIFTS ──
@app.post("/api/start-shift")
def start_shift(data: dict):
    wid = data.get("worker_id")
    if not wid or wid not in DB["workers"]:
        raise HTTPException(404, "Worker not found")
    for s in DB["shifts"].values():
        if s["worker_id"] == wid and s["status"] == "ACTIVE":
            raise HTTPException(400, "Shift already active")
    sid = _next("shift")
    DB["shifts"][sid] = {
        "id": sid, "worker_id": wid, "start_time": _now(),
        "end_time": None, "active_minutes": 0,
        "gps_points": 0, "avg_speed": 0,
        "status": "ACTIVE", "fraud_score": 0
    }
    return {"success": True, "shift_id": sid, "status": "ACTIVE",
            "message": "Shift started. GigShield is monitoring your route."}

@app.post("/api/end-shift")
def end_shift(data: dict):
    wid   = data.get("worker_id")
    shift = next((s for s in DB["shifts"].values()
                  if s["worker_id"] == wid and s["status"] == "ACTIVE"), None)
    if not shift: raise HTTPException(404, "No active shift")
    dur = random.randint(180, 360)
    act = int(dur * random.uniform(0.55, 0.85))
    gps = random.randint(60, 200)
    spd = random.uniform(12, 35)
    fr  = get_fraud_score(gps, act, spd, dur)
    shift.update({
        "end_time": _now(), "active_minutes": act,
        "gps_points": gps, "avg_speed": round(spd, 1),
        "status": "COMPLETED", "fraud_score": fr["score"]
    })
    return {"success": True, "shift_id": shift["id"], "status": "COMPLETED",
            "duration_minutes": dur, "active_minutes": act,
            "fraud_score": fr["score"], "verdict": fr["verdict"]}

@app.get("/api/shift/{worker_id}/active")
def active_shift(worker_id: int):
    s = next((s for s in DB["shifts"].values()
               if s["worker_id"] == worker_id and s["status"] == "ACTIVE"), None)
    return {"active": bool(s), "shift": s}


# ── TRIGGER / AUTO-CLAIM ──
class TriggerReq(BaseModel):
    worker_id: int
    simulate_rain: float = None
    simulate_temp: float = None
    simulate_aqi:  int   = None

@app.post("/api/trigger-event")
async def trigger_event(req: TriggerReq):
    wid = req.worker_id
    if wid not in DB["workers"]: raise HTTPException(404, "Worker not found")
    pol = DB["policies"].get(wid)
    if not pol: raise HTTPException(404, "No active policy")

    w       = DB["workers"][wid]
    weather = await fetch_weather(w["city"])
    if req.simulate_rain is not None:
        weather["rain_mm"]      = req.simulate_rain
        weather["source"]       = "simulated"
    if req.simulate_temp is not None:
        weather["temp_celsius"] = req.simulate_temp
        weather["source"]       = "simulated"
    if req.simulate_aqi is not None:
        weather["aqi"]          = req.simulate_aqi
        weather["source"]       = "simulated"

    active_triggers = evaluate_triggers(
        weather["rain_mm"], weather["temp_celsius"], weather["aqi"]
    )
    if not active_triggers:
        return {"triggered": False, "weather": weather,
                "message": "No parametric threshold crossed — no claim generated."}

    shift = next((s for s in DB["shifts"].values()
                  if s["worker_id"] == wid and s["status"] == "ACTIVE"), None)
    if not shift:
        return {"triggered": True, "weather": weather,
                "active_triggers": active_triggers, "claim_generated": False,
                "message": "Trigger fired but no active shift. Start a shift to be covered."}

    week_id   = _week_id()
    week_paid = sum(
        c["final_amount"] for c in DB["claims"].values()
        if c["worker_id"] == wid and c["week_id"] == week_id
        and c["status"] in ("APPROVED", "MANUAL_REVIEW")
    )
    claims_created = []

    for trig in active_triggers:
        if week_paid >= pol["weekly_cap"]: break
        hours = 1.5 if trig["multiplier"] == 0.6 else (2.5 if trig["multiplier"] == 1.5 else 2.0)
        fr    = get_fraud_score(
            shift["gps_points"] or 45,
            shift["active_minutes"] or 120,
            shift["avg_speed"] or 20, 180
        )
        raw       = round(0.5 * (pol["predicted_earnings"] / 6) * hours * trig["multiplier"], 2)
        remaining = pol["weekly_cap"] - week_paid
        final     = min(raw, remaining)
        status    = ("BLOCKED"       if fr["verdict"] == "AUTO_REJECTED"
                     else "MANUAL_REVIEW" if fr["verdict"] == "MANUAL_REVIEW"
                     else "APPROVED")
        cid = _next("claim")
        DB["claims"][cid] = {
            "id": cid, "worker_id": wid, "shift_id": shift["id"],
            "trigger_type": trig["type"], "trigger_value": trig["value"],
            "severity_multiplier": trig["multiplier"],
            "hours_disrupted": hours, "base_amount": raw,
            "final_amount": round(final, 2), "fraud_score": fr["score"],
            "status": status, "week_id": week_id,
            "created_at": _now(), "paid_at": None
        }
        week_paid += final
        claims_created.append({
            **DB["claims"][cid],
            "paid": "Friday 23:59" if status == "APPROVED" else status
        })

    return {
        "triggered": True, "weather": weather,
        "active_triggers": active_triggers,
        "claim_generated": bool(claims_created),
        "claims": claims_created,
        "message": f"{len(claims_created)} claim(s) generated. Payout Friday 23:59."
    }

@app.post("/api/auto-claim")
async def auto_claim(data: dict):
    return await trigger_event(TriggerReq(
        worker_id=data.get("worker_id"),
        simulate_rain=data.get("rain_mm", 85),
        simulate_temp=data.get("temp_c"),
        simulate_aqi=data.get("aqi"),
    ))


# ── CLAIMS ──
@app.get("/api/claims/{worker_id}")
def get_claims(worker_id: int):
    return sorted(
        [c for c in DB["claims"].values() if c["worker_id"] == worker_id],
        key=lambda x: x["created_at"], reverse=True
    )

@app.get("/api/claims/{worker_id}/week")
def get_week_claims(worker_id: int):
    wk     = _week_id()
    claims = [c for c in DB["claims"].values()
               if c["worker_id"] == worker_id and c["week_id"] == wk]
    total  = sum(c["final_amount"] for c in claims
                 if c["status"] in ("APPROVED", "MANUAL_REVIEW"))
    return {
        "week_id": wk,
        "claims": sorted(claims, key=lambda x: x["created_at"], reverse=True),
        "week_total": total
    }


# ── PAYOUT ──
@app.post("/api/weekly-payout")
def weekly_payout(data: dict = None):
    wid     = (data or {}).get("worker_id")
    wk      = _week_id()
    wids    = [wid] if wid else list(DB["workers"].keys())
    results = []

    for w in wids:
        claims = [c for c in DB["claims"].values()
                   if c["worker_id"] == w
                   and c["week_id"] == wk
                   and c["status"] == "APPROVED"]
        if not claims: continue
        pol   = DB["policies"].get(w, {})
        cap   = pol.get("weekly_cap", 200.0)
        total = sum(c["final_amount"] for c in claims)
        final = min(total, cap)
        ref   = _upi()
        pid   = _next("payout")
        DB["payouts"][pid] = {
            "id": pid, "worker_id": w, "week_id": wk,
            "total_claimed": total, "cap_applied": cap,
            "final_payout": final, "claims_count": len(claims),
            "upi_ref": ref, "created_at": _now()
        }
        for c in claims:
            c["status"] = "PAID"
            c["paid_at"] = _now()
        if not any(c["fraud_score"] > 40 for c in claims):
            if w in DB["workers"]:
                DB["workers"][w]["weeks_clean"] += 1
        results.append({
            "worker_id": w, "final_payout": round(final, 2),
            "upi_ref": ref, "claims_count": len(claims),
            "message": f"₹{final:.0f} transferred · Ref: {ref}"
        })

    if not results:
        return {"processed": 0, "message": "No approved claims this week."}
    return {"processed": len(results), "week_id": wk, "payouts": results,
            "message": f"Payout complete. {len(results)} worker(s) paid."}

@app.get("/api/payouts/{worker_id}")
def get_payouts(worker_id: int):
    return sorted(
        [p for p in DB["payouts"].values() if p["worker_id"] == worker_id],
        key=lambda x: x["created_at"], reverse=True
    )


# ── MANGUM HANDLER ───────────────────────────────────────────────
handler = Mangum(app, lifespan="off")
