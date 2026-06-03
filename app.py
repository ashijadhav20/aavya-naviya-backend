import os
import math
import datetime
import requests
from flask import Flask, request, jsonify
from flask_cors import CORS
import ee

app = Flask(__name__)
CORS(app)

# 🔑 Your OpenWeatherMap API Key has been directly embedded here:
OPENWEATHER_API_KEY = "21dcd1d4623a0832583bfd6c4e25c85b"
GEE_PROJECT_ID = "ashijadhav20"

try:
    ee.Initialize(project=GEE_PROJECT_ID)
    print("Google Earth Engine initialized successfully on server cluster.")
except Exception as e:
    print(f"GEE Cloud Connection Failed: {e}")

# Complete Agricultural Soil Texture Database Map
SOIL_DATABASE = {
    "Sandy": {"awc": 75},       
    "Sandy Loam": {"awc": 120},  
    "Loam": {"awc": 165},        
    "Silt Loam": {"awc": 180},   
    "Clay Loam": {"awc": 170},   
    "Clay": {"awc": 175},        
    "Sandy Clay Loam": {"awc": 142},
    "Silty Clay": {"awc": 177},
    "Organic/Peat": {"awc": 325}
}

# Complete FAO-56 Duration Database Map
CROP_DATABASE = {
    "Rice (Paddy)": {"duration": 130},
    "Wheat": {"duration": 130},
    "Maize (Corn)": {"duration": 110},
    "Soybean": {"duration": 115},
    "Cotton": {"duration": 180},
    "Sugarcane": {"duration": 365},
    "Groundnut": {"duration": 115},
    "Mustard/Canola": {"duration": 125},
    "Tomato": {"duration": 135},
    "Potato": {"duration": 105},
    "Onion": {"duration": 125}
}

def calculate_fao56_eto(w):
    T, u2, P = w["temp"], w["wind_speed"], w["pressure"]
    gamma = 0.000665 * P
    mean_temp = (w["temp_max"] + w["temp_min"]) / 2.0
    delta = (4098 * (0.6108 * math.exp((17.27 * mean_temp) / (mean_temp + 237.3)))) / ((mean_temp + 237.3) ** 2)
    es = (0.6108 * math.exp((17.27 * w["temp_max"]) / (w["temp_max"] + 237.3)) + 0.6108 * math.exp((17.27 * w["temp_min"]) / (w["temp_min"] + 237.3))) / 2.0
    ea = es * (w["humidity"] / 100.0)
    vpd = es - ea
    R_s = (0.25 + 0.50 * ((100 - w["clouds"]) / 100.0)) * 25.0
    R_n = ((1 - 0.23) * R_s) - (3.4e-9 * (T + 273.16)**4 * (0.34 - 0.14 * math.sqrt(ea)) * (0.1 + 0.9 * (R_s / 25.0)))
    num = (0.408 * delta * R_n) + (gamma * (900 / (T + 273)) * u2 * vpd)
    den = delta + (gamma * (1 + 0.34 * u2))
    return max(0.1, round(num / den, 2))

@app.route('/api/calculate', methods=['POST'])
def handle_irrigation_request():
    data = request.json
    lat, lon = data.get("lat"), data.get("lon")
    crop_name, soil_name = data.get("crop_name"), data.get("soil_name")
    sow_str, cur_str = data.get("sowing_date"), data.get("current_date")
    geom, d1, d2 = data.get("geom"), data.get("dim1"), data.get("dim2")
    custom_water = data.get("custom_water_applied", 0.0)

    # 1. Pull OpenWeather parameters
    w_url = f"https://api.openweathermap.org/data/2.5/weather?lat={lat}&lon={lon}&appid={OPENWEATHER_API_KEY}&units=metric"
    res = requests.get(w_url).json()
    w = {
        "temp": res["main"]["temp"], "temp_max": res["main"]["temp_max"], "temp_min": res["main"]["temp_min"],
        "humidity": res["main"]["humidity"], "wind_speed": res["wind"]["speed"], "clouds": res["clouds"]["all"],
        "pressure": res["main"]["pressure"] / 10.0, "rainfall": res.get("rain", {}).get("1h", 0.0)
    }

    # 2. Reference Penman ET
    eto = calculate_fao56_eto(w)
    
    # 3. Pull GEE Sentinel-2 Reflectance NDVI Matrix
    ndvi, tile_url = 0.42, None
    try:
        point = ee.Geometry.Point([lon, lat])
        s2 = (ee.ImageCollection('COPERNICUS/S2_SR_HARMONIZED')
              .filterBounds(point.buffer(2500)).filterDate(str(datetime.date.today()-datetime.timedelta(days=45)), str(datetime.date.today()))
              .filter(ee.Filter.lt('CLOUDY_PIXEL_PERCENTAGE', 20)).sort('CLOUDY_PIXEL_PERCENTAGE'))
        img = s2.first()
        if img.getInfo() is not None:
            ndvi_val = img.normalizedDifference(['B8', 'B4']).reduceRegion(reducer=ee.Reducer.mean(), geometry=point.buffer(100), scale=10).get('nd').getInfo()
            if ndvi_val is not None: ndvi = round(ndvi_val, 3)
    except Exception:
        pass

    # 4. Corrected crop staging calculations
    sow = datetime.datetime.strptime(sow_str, '%Y-%m-%d').date()
    cur = datetime.datetime.strptime(cur_str, '%Y-%m-%d').date()
    das = max(0, (cur - sow).days)
    
    total_dur = CROP_DATABASE[crop_name]["duration"]
    if das <= (0.15 * total_dur): stage = "Initial Stage"
    elif das <= (0.65 * total_dur): stage = "Mid-Season Stage"
    elif das <= total_dur: stage = "Late Stage"
    else: stage = "Post-Harvest Lifecycle Phase"

    kc_modified = max(0.15, min(round((1.457 * ndvi) + 0.1, 2), 1.30))
    etc = round(eto * kc_modified, 2)

    # 5. Volumetric area computations
    field_area = d1 * d2 if geom == 'Rectangular' else (math.pi * (d1**2) if geom == 'Circular' else d1 * 850.0)
    eff_rain = max(0.0, round((0.8 * w["rainfall"]) - 25, 2) if w["rainfall"] > 8.3 else round(0.6 * w["rainfall"], 2))
    iwr_mm = max(0.0, round(etc - eff_rain, 2))
    vol_liters = round(field_area * iwr_mm, 1)

    # 6. Scheduling balances
    taw = SOIL_DATABASE[soil_name]["awc"] * 0.45
    depletion = etc - eff_rain
    days = max(0, math.floor((0.5 * taw) / depletion)) if depletion > 0 else 999
    custom_freq = max(0, math.floor(custom_water / etc)) if custom_water > 0 and etc > 0 else None

    return jsonify({
        "lat": lat, "lon": lon, "eto": eto, "kc_modified": kc_modified, "etc": etc,
        "iwr_mm": iwr_mm, "vol_liters": vol_liters, "vol_m3": round(vol_liters / 1000.0, 2),
        "days_to_irrigate": days, "custom_frequency": custom_freq, "field_area": round(field_area, 1), "geom": geom,
        "temp": w["temp"], "humidity": w["humidity"], "wind_speed": w["wind_speed"], "rainfall": w["rainfall"],
        "state_mapped": state, "district_mapped": dist, "taluka_mapped": tal, "village_mapped": vil,
        "das": das, "stage": stage
    })

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))