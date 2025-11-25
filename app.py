from flask import Flask, render_template, jsonify, request, redirect, url_for
import tinytuya
import time
from datetime import datetime, timedelta
import json
import os
import threading
from collections import defaultdict

# -----------------------------
# Configuration
# -----------------------------
DEVICE_ID = os.getenv('DEVICE_ID', 'bfc3b6613aef942483cwst')
LOCAL_KEY = os.getenv('LOCAL_KEY', '+xsL0^>f~Q?)Xh&J')
VERSION = os.getenv('VERSION', '3.5')
JSON_FILE = 'data.json'
SETTINGS_FILE = 'settings.json'
file_lock = threading.Lock()

# -----------------------------
# Initialize Flask
# -----------------------------
app = Flask(__name__, template_folder='templates')

# -----------------------------
# Initialize Tuya Device
# -----------------------------
def initialize_device():
    try:
        device_ip = os.getenv('DEVICE_IP', '')

        if not device_ip:
            print("Discovering device IP...")
            devices = tinytuya.deviceScan(False, 20)
            for ip, info in devices.items():
                if info.get('gwId') == DEVICE_ID:
                    device_ip = ip
                    print(f"Discovered device at IP: {device_ip}")
                    break
            if not device_ip:
                raise Exception("Device not found on network")

        device = tinytuya.OutletDevice(DEVICE_ID, device_ip, LOCAL_KEY)
        device.set_version(float(VERSION))
        return device
    except Exception as e:
        print(f"Device initialization error: {e}")
        return None

device = initialize_device()

# -----------------------------
# File & folder setup
# -----------------------------
def init_files():
    if not os.path.exists('templates'):
        os.makedirs('templates')
    if not os.path.exists('static'):
        os.makedirs('static')
    if not os.path.exists(JSON_FILE):
        with open(JSON_FILE, 'w') as f:
            json.dump([], f)
    # settings: store cost_per_kwh
    if not os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE, 'w') as f:
            json.dump({'cost_per_kwh': 10.0}, f)  # default 10 (currency units)

init_files()

def read_settings():
    try:
        with file_lock:
            with open(SETTINGS_FILE, 'r') as f:
                return json.load(f)
    except Exception:
        return {'cost_per_kwh': 10.0}

def write_settings(s):
    with file_lock:
        with open(SETTINGS_FILE, 'w') as f:
            json.dump(s, f)

# -----------------------------
# Save energy data to JSON
# -----------------------------
def save_energy_data(dps):
    try:
        entry = {
            'timestamp': datetime.now().isoformat(),
            'data': {
                'current_ma': dps.get('18', 0),
                'power_w': dps.get('19', 0)/10,
                'voltage_v': dps.get('20', 0)/10,
                'total_kwh': dps.get('17', 0)/1000,
                'is_on': bool(dps.get('1', False))
            }
        }

        with file_lock:
            with open(JSON_FILE, 'r') as f:
                existing_data = json.load(f)

            existing_data.append(entry)

            # Keep only last 30 days
            cutoff = datetime.now() - timedelta(days=30)
            existing_data = [
                e for e in existing_data
                if parse_time(e['timestamp']) > cutoff
            ]

            with open(JSON_FILE, 'w') as f:
                json.dump(existing_data, f)
    except Exception as e:
        print(f"Error saving data: {e}")

# -----------------------------
# Poll device every 8 seconds
# -----------------------------
def poll_device():
    while True:
        try:
            if device:
                dps = device.status().get('dps', {})
                save_energy_data(dps)
        except Exception as e:
            print("Error polling device:", e)
        time.sleep(8)

poll_thread = threading.Thread(target=poll_device)
poll_thread.daemon = True
poll_thread.start()

# -----------------------------
# Time helpers
# -----------------------------
def parse_time(ts: str) -> datetime:
    """Robust ISO timestamp parser: handles space/T and optional microseconds"""
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        ts2 = ts.replace(" ", "T")
        try:
            return datetime.fromisoformat(ts2)
        except ValueError:
            base = ts2.split('.')[0]
            return datetime.fromisoformat(base)

# -----------------------------
# Energy integration helpers
# -----------------------------
def compute_kwh_from_sorted_samples(samples):
    """
    samples: list of dicts with keys 'timestamp' (iso) and 'power_w' (float)
    Returns total kWh for the period covered by the samples using trapezoidal integration.
    If only single sample, approximates using 60 seconds duration.
    """
    if not samples:
        return 0.0

    # convert and sort by time
    pts = []
    for s in samples:
        try:
            t = parse_time(s['timestamp'])
            p = float(s['power_w'])
            pts.append((t, p))
        except Exception:
            continue
    if not pts:
        return 0.0

    pts.sort(key=lambda x: x[0])

    # if only one point, approximate 60 seconds
    if len(pts) == 1:
        duration_secs = 60.0
        avg_power = pts[0][1]
        kwh = (avg_power * duration_secs) / 3600.0 / 1000.0
        return round(kwh, 6)

    total_wh_seconds = 0.0  # power * seconds (W * s)
    for i in range(len(pts) - 1):
        t0, p0 = pts[i]
        t1, p1 = pts[i + 1]
        delta = (t1 - t0).total_seconds()
        if delta < 0:
            continue
        # trapezoid area in W * s
        total_wh_seconds += (p0 + p1) / 2.0 * delta

    # For the final sample we don't know how long it lasted; approximate using median delta
    deltas = []
    for i in range(len(pts) - 1):
        d = (pts[i+1][0] - pts[i][0]).total_seconds()
        if d > 0:
            deltas.append(d)
    median_delta = sorted(deltas)[len(deltas)//2] if deltas else 60.0
    # add last sample hold
    total_wh_seconds += pts[-1][1] * median_delta

    # convert W*s to kWh: (W * s) / 3600 = Wh; /1000 = kWh
    total_kwh = total_wh_seconds / 3600.0 / 1000.0
    return round(total_kwh, 6)

# -----------------------------
# Historical & analytics helpers
# -----------------------------
def get_historical_data(days=1):
    """
    Returns a dict keyed by YYYY-MM-DD with lists of power/voltage/current/timestamps for last `days`.
    """
    try:
        with file_lock:
            with open(JSON_FILE, 'r') as f:
                all_data = json.load(f)

        cutoff = datetime.now() - timedelta(days=days)
        recent_data = [
            e for e in all_data
            if parse_time(e['timestamp']) > cutoff
        ]

        daily_data = defaultdict(lambda: {'power': [], 'voltage': [], 'current': [], 'timestamps': []})

        for e in recent_data:
            date = parse_time(e['timestamp']).strftime('%Y-%m-%d')
            daily_data[date]['power'].append(e['data']['power_w'])
            daily_data[date]['voltage'].append(e['data']['voltage_v'])
            daily_data[date]['current'].append(e['data']['current_ma'])
            daily_data[date]['timestamps'].append(e['timestamp'])

        return daily_data
    except Exception as e:
        print(f"Error reading historical data: {e}")
        return {}

def analyze_data(data):
    analysis = {}
    for date, values in data.items():
        if values['power']:
            analysis[date] = {
                'power': {
                    'avg': round(sum(values['power'])/len(values['power']), 2),
                    'max': round(max(values['power']), 2),
                    'min': round(min(values['power']), 2)
                },
                'voltage': {
                    'avg': round(sum(values['voltage'])/len(values['voltage']), 2),
                    'max': round(max(values['voltage']), 2),
                    'min': round(min(values['voltage']), 2)
                },
                'current': {
                    'avg': round(sum(values['current'])/len(values['current'])/1000, 3),
                    'max': round(max(values['current'])/1000, 3),
                    'min': round(min(values['current'])/1000, 3)
                }
            }
    return analysis

def build_daily_summary(days_back=30):
    """
    Returns a list of daily summaries for up to `days_back` days (most recent first).
    Each item: {date, total_kwh, cost, avg_w, max_w, min_w}
    """
    try:
        with file_lock:
            with open(JSON_FILE, 'r') as f:
                all_data = json.load(f)
    except Exception as e:
        print("build_daily_summary read error:", e)
        return []

    # group by date
    by_date = defaultdict(list)
    for e in all_data:
        try:
            date_str = parse_time(e['timestamp']).strftime('%Y-%m-%d')
            by_date[date_str].append({'timestamp': e['timestamp'], 'power_w': e['data']['power_w']})
        except Exception:
            continue

    dates = sorted(by_date.keys(), reverse=True)[:days_back]
    settings = read_settings()
    rate = float(settings.get('cost_per_kwh', 10.0))

    summary = []
    for d in sorted(dates):
        samples = by_date.get(d, [])
        kwh = compute_kwh_from_sorted_samples(samples)
        powers = [s['power_w'] for s in samples] if samples else []
        avg_w = round(sum(powers)/len(powers), 2) if powers else 0.0
        max_w = round(max(powers), 2) if powers else 0.0
        min_w = round(min(powers), 2) if powers else 0.0
        cost = round(kwh * rate, 4)
        summary.append({
            'date': d,
            'total_kwh': kwh,
            'cost': cost,
            'avg_w': avg_w,
            'max_w': max_w,
            'min_w': min_w
        })

    # return sorted ascending by date
    return sorted(summary, key=lambda x: x['date'])

# -----------------------------
# Flask routes
# -----------------------------
@app.route('/')
def index():
    try:
        with file_lock:
            with open(JSON_FILE, 'r') as f:
                all_data = json.load(f)
    except Exception:
        all_data = []

    settings = read_settings()
    rate = float(settings.get('cost_per_kwh', 10.0))

    if all_data:
        latest = all_data[-1]['data']
        power_w = latest['power_w']
        # instantaneous kW
        kw = power_w / 1000.0
        # instantaneous cost per hour at current power
        cost_per_hour = round(kw * rate, 6)
        cost_per_min = round(cost_per_hour / 60.0, 6)
        # estimated daily cost if current power sustained 24h
        est_daily_cost = round(cost_per_hour * 24.0, 4)

        return render_template('dashboard.html',
                               is_on=latest['is_on'],
                               current_ma=latest['current_ma'],
                               power_w=power_w,
                               voltage_v=latest['voltage_v'],
                               total_kwh=latest['total_kwh'],
                               cost_per_kwh=rate,
                               est_daily_cost=est_daily_cost,
                               cost_per_min=cost_per_min,
                               cost_per_hour=cost_per_hour)
    # fallback
    return render_template('dashboard.html',
                           is_on=None,
                           current_ma=0,
                           power_w=0,
                           voltage_v=0,
                           total_kwh=0,
                           cost_per_kwh=rate,
                           est_daily_cost=0,
                           cost_per_min=0,
                           cost_per_hour=0)

@app.route('/live_data')
def live_data():
    try:
        with file_lock:
            with open(JSON_FILE, 'r') as f:
                all_data = json.load(f)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    if not all_data:
        return jsonify({'error': 'No data'}), 500

    latest = all_data[-1]['data']
    settings = read_settings()
    rate = float(settings.get('cost_per_kwh', 10.0))
    power_w = latest['power_w']
    kw = power_w / 1000.0
    cost_per_hour = round(kw * rate, 6)
    cost_per_min = round(cost_per_hour / 60.0, 6)

    return jsonify({
        'is_on': latest['is_on'],
        'power': latest['power_w'],
        'voltage': latest['voltage_v'],
        'current': latest['current_ma'],
        'total_kwh': latest['total_kwh'],
        'cost_per_kwh': rate,
        'cost_per_hour': cost_per_hour,
        'cost_per_min': cost_per_min
    })

# Page route that renders the template and provides the available dates list
@app.route('/historical_data_page')
def historical_data_page():
    try:
        with file_lock:
            with open(JSON_FILE, 'r') as f:
                all_data = json.load(f)
    except FileNotFoundError:
        return render_template('history.html', dates=[])
    except Exception as e:
        return f"Error reading data file: {e}", 500

    try:
        dates = sorted({ parse_time(e['timestamp']).strftime('%Y-%m-%d') for e in all_data })
    except Exception as e:
        dates = []

    settings = read_settings()
    rate = float(settings.get('cost_per_kwh', 10.0))

    return render_template('history.html', dates=dates, cost_per_kwh=rate)

# JSON API route used by the page to fetch chart data for a single date
@app.route('/historical_data')
def historical_data():
    date = request.args.get('date')
    if not date:
        return jsonify({"error": "Date is required"}), 400

    try:
        with file_lock:
            with open(JSON_FILE, 'r') as f:
                all_data = json.load(f)
    except Exception as e:
        return jsonify({"error": f"Unable to read data file: {e}"}), 500

    # Filter data for the selected date
    filtered = [
        e for e in all_data
        if parse_time(e["timestamp"]).strftime("%Y-%m-%d") == date
    ]

    if not filtered:
        return jsonify({"error": "No data for this date"}), 404

    # Prepare list for integration
    samples = [{'timestamp': e['timestamp'], 'power_w': e['data']['power_w']} for e in filtered]
    total_kwh = compute_kwh_from_sorted_samples(samples)

    timestamps = [e["timestamp"] for e in filtered]
    power = [e["data"]["power_w"] for e in filtered]
    voltage = [e["data"]["voltage_v"] for e in filtered]
    current = [round(e["data"]["current_ma"] / 1000, 3) for e in filtered]

    settings = read_settings()
    rate = float(settings.get('cost_per_kwh', 10.0))
    cost = round(total_kwh * rate, 4)

    return jsonify({
        "timestamps": timestamps,
        "power": power,
        "voltage": voltage,
        "current": current,
        "total_kwh": total_kwh,
        "cost": cost
    })

@app.route('/daily_summary')
def daily_summary():
    """
    Returns an array of daily summary objects for last 30 days:
    [{date, total_kwh, cost, avg_w, max_w, min_w}, ...]
    """
    days = int(request.args.get('days', 30))
    summary = build_daily_summary(days_back=days)
    return jsonify({'summary': summary})

@app.route('/set_rate', methods=['POST'])
def set_rate():
    try:
        payload = request.get_json(force=True)
        rate = float(payload.get('rate'))
        if rate < 0:
            return jsonify({'error': 'Rate must be non-negative'}), 400
    except Exception as e:
        return jsonify({'error': 'Invalid payload'}), 400

    settings = read_settings()
    settings['cost_per_kwh'] = rate
    write_settings(settings)
    return jsonify({'success': True, 'cost_per_kwh': rate})

@app.route('/manual')
def manual():
    return render_template('manual.html')

@app.route('/on')
def turn_on():
    if device:
        try:
            device.set_status(True, '1')
        except Exception as e:
            print("Turn on error:", e)
    return redirect(url_for('index'))

@app.route('/off')
def turn_off():
    if device:
        try:
            device.set_status(False, '1')
        except Exception as e:
            print("Turn off error:", e)
    return redirect(url_for('index'))

# -----------------------------
# Run Flask server
# -----------------------------
if __name__ == '__main__':
    print("Starting server...")
    print("Local: http://127.0.0.1:5000")
    print("Network: http://<your-local-ip>:5000")
    app.run(host='0.0.0.0', port=5000, debug=False)
