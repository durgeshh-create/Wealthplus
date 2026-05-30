"""
refresh_token.py
================
Spins up a local server on port 5050 to capture the enctoken manually.
This is the most reliable, un-blockable method for authentication.
"""

import json
import os
import threading
import webbrowser
import logging
from pathlib import Path
from datetime import datetime
from flask import Flask, request, render_template_string

# Disable Flask startup logging to keep the console clean
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

PROJECT_DIR  = Path(__file__).parent
CONFIG_DIR   = PROJECT_DIR / "config"
CONFIG_DIR.mkdir(exist_ok=True)
ENCTOKEN_FILE = CONFIG_DIR / "enctoken.json"

app = Flask(__name__)

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Wealth++ Login</title>
    <style>
        body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background-color: #0d1117; color: #c9d1d9; display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; }
        .container { background-color: #161b22; padding: 30px; border-radius: 8px; box-shadow: 0 4px 15px rgba(0,0,0,0.5); width: 100%; max-width: 500px; border: 1px solid #30363d; }
        h2 { color: #58a6ff; margin-top: 0; }
        p { color: #8b949e; line-height: 1.5; font-size: 14px;}
        input[type="text"] { width: 100%; padding: 12px; margin: 15px 0; background-color: #0d1117; border: 1px solid #30363d; color: #c9d1d9; border-radius: 6px; box-sizing: border-box; font-family: monospace; }
        input[type="text"]:focus { outline: none; border-color: #58a6ff; }
        button { background-color: #238636; color: white; border: none; padding: 12px 20px; text-align: center; display: inline-block; font-size: 16px; border-radius: 6px; cursor: pointer; width: 100%; font-weight: bold; }
        button:hover { background-color: #2ea043; }
        .success { color: #3fb950; font-weight: bold; text-align: center; display: none; margin-top: 15px; }
    </style>
</head>
<body>
    <div class="container">
        <h2>Connect to Zerodha</h2>
        <p>1. Open <strong>kite.zerodha.com</strong> and log in normally.<br>
           2. Press <strong>F12</strong> to open Developer Tools.<br>
           3. Go to <strong>Application</strong> -> <strong>Cookies</strong> -> kite.zerodha.com.<br>
           4. Copy the value next to <strong>enctoken</strong>.</p>
        
        <form id="tokenForm">
            <input type="text" id="enctoken" name="enctoken" placeholder="Paste your long enctoken here..." required>
            <button type="submit">Save Token & Start Bot</button>
        </form>
        <div id="successMessage" class="success">✅ Token Saved! You can close this window.</div>
    </div>

    <script>
        document.getElementById('tokenForm').onsubmit = function(e) {
            e.preventDefault();
            const token = document.getElementById('enctoken').value;
            fetch('/save', {
                method: 'POST',
                headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
                body: 'enctoken=' + encodeURIComponent(token)
            }).then(() => {
                document.getElementById('tokenForm').style.display = 'none';
                document.getElementById('successMessage').style.display = 'block';
                // Give the server a second to save, then tell it to shutdown
                setTimeout(() => fetch('/shutdown', {method: 'POST'}), 1000);
            });
        };
    </script>
</body>
</html>
"""

@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route('/save', methods=['POST'])
def save():
    token = request.form.get('enctoken')
    if token:
        # Load credentials to get user_id, or use a default if missing
        creds_file = CONFIG_DIR / "credentials.json"
        user_id = "UNKNOWN"
        if creds_file.exists():
            try:
                user_id = json.loads(creds_file.read_text()).get("user_id", "UNKNOWN")
            except Exception: pass

        payload = {
            "user_id": user_id,
            "enctoken": token.strip(),
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        ENCTOKEN_FILE.write_text(json.dumps(payload, indent=2))
        print(f"\n[INFO] ✓ SUCCESS! enctoken saved for User: {user_id}")
    return "OK", 200

@app.route('/shutdown', methods=['POST'])
def shutdown():
    # Attempt to cleanly shut down the Flask server
    func = request.environ.get('werkzeug.server.shutdown')
    if func is None:
        os._exit(0) # Force exit if running in a non-werkzeug server environment
    func()
    return "Shutting down...", 200

def main():
    print("=" * 60)
    print("ZERODHA LOCAL LOGIN HANDLER")
    print("=" * 60)
    print("[INFO] Starting local capture server...")
    
    port = 5050
    url = f"http://localhost:{port}"
    
    # Open the browser automatically
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    
    print(f"[INFO] 🌐 Please paste your enctoken at: {url}")
    print("[INFO] Waiting for token...")
    
    # Run the server (this will block until /shutdown is called)
    app.run(host='127.0.0.1', port=port, debug=False, use_reloader=False)
    
    print("[INFO] Server closed. Token captured successfully.")

if __name__ == "__main__":
    main()