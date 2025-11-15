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

init_files()

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
                if datetime.fromisoformat(e['timestamp']) > cutoff
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
# Historical & analytics helpers
# -----------------------------
def get_historical_data(days=1):
    try:
        with file_lock:
            with open(JSON_FILE, 'r') as f:
                all_data = json.load(f)

        cutoff = datetime.now() - timedelta(days=days)
        recent_data = [
            e for e in all_data
            if datetime.fromisoformat(e['timestamp']) > cutoff
        ]

        daily_data = defaultdict(lambda: {'power': [], 'voltage': [], 'current': [], 'timestamps': []})

        for e in recent_data:
            date = datetime.fromisoformat(e['timestamp']).strftime('%Y-%m-%d')
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

# -----------------------------
# Flask routes
# -----------------------------
@app.route('/')
def index():
    try:
        with file_lock:
            with open(JSON_FILE, 'r') as f:
                all_data = json.load(f)

        if all_data:
            latest = all_data[-1]['data']
            return render_template('dashboard.html',
                                   is_on=latest['is_on'],
                                   current_ma=latest['current_ma'],
                                   power_w=latest['power_w'],
                                   voltage_v=latest['voltage_v'],
                                   total_kwh=latest['total_kwh'])
        # fallback
        return render_template('dashboard.html',
                               is_on=None,
                               current_ma=0,
                               power_w=0,
                               voltage_v=0,
                               total_kwh=0)
    except Exception as e:
        print(f"Dashboard error: {e}")
        return render_template('dashboard.html',
                               is_on=None,
                               current_ma=0,
                               power_w=0,
                               voltage_v=0,
                               total_kwh=0)

@app.route('/live_data')
def live_data():
    try:
        with file_lock:
            with open(JSON_FILE, 'r') as f:
                all_data = json.load(f)

        if not all_data:
            return jsonify({'error': 'No data'}), 500

        latest = all_data[-1]['data']
        return jsonify({
            'is_on': latest['is_on'],
            'power': latest['power_w'],
            'voltage': latest['voltage_v'],
            'current': latest['current_ma'],
            'total_kwh': latest['total_kwh']
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# Helper to parse ISO timestamps robustly (handles " " vs "T" and microseconds)
def parse_time(ts: str) -> datetime:
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        ts2 = ts.replace(" ", "T")
        try:
            return datetime.fromisoformat(ts2)
        except ValueError:
            base = ts2.split('.')[0]
            return datetime.fromisoformat(base)

# Page route that renders the template and provides the available dates list
@app.route('/historical_data_page')
def historical_data_page():
    try:
        with file_lock:
            with open(JSON_FILE, 'r') as f:
                all_data = json.load(f)
    except FileNotFoundError:
        # File missing — render page with empty dates so template shows "no data available"
        return render_template('history.html', dates=[])
    except Exception as e:
        return f"Error reading data file: {e}", 500

    # Build sorted unique date list (YYYY-MM-DD)
    try:
        dates = sorted({ parse_time(e['timestamp']).strftime('%Y-%m-%d') for e in all_data })
    except Exception as e:
        # if parsing fails, return empty list to template
        dates = []

    return render_template('history.html', dates=dates)


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
        if datetime.fromisoformat(e["timestamp"]).strftime("%Y-%m-%d") == date
    ]

    if not filtered:
        return jsonify({"error": "No data for this date"}), 404

    timestamps = [e["timestamp"] for e in filtered]
    power = [e["data"]["power_w"] for e in filtered]
    voltage = [e["data"]["voltage_v"] for e in filtered]
    current = [round(e["data"]["current_ma"] / 1000, 3) for e in filtered]

    # compute estimated kwh (1 entry per minute)
    total_kwh = round(sum(power) / 1000 / 60, 4)

    return jsonify({
        "timestamps": timestamps,
        "power": power,
        "voltage": voltage,
        "current": current,
        "total_kwh": total_kwh
    })


@app.route('/manual')
def manual():
    return render_template('manual.html')

@app.route('/on')
def turn_on():
    if device:
        device.set_status(True, '1')
    return redirect(url_for('index'))

@app.route('/off')
def turn_off():
    if device:
        device.set_status(False, '1')
    return redirect(url_for('index'))

# -----------------------------
# Run Flask server
# -----------------------------
if __name__ == '__main__':
    print("Starting server...")
    print("Local: http://127.0.0.1:5000")
    print("Network: http://<your-local-ip>:5000")
    app.run(host='0.0.0.0', port=5000, debug=False)
