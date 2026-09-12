import os
import threading
from flask import Flask, render_template

app = Flask(__name__, template_folder='../templates')

@app.route('/')
def home():
    return render_template('index.html')


def run():
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=False)


def keep_alive():
    t = threading.Thread(target=run, daemon=True)
    t.start()
