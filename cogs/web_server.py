import json
import math
import os
import threading
import time
import urllib.parse
import urllib.request

import discord
from flask import Flask, jsonify, render_template

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
app = Flask(__name__, template_folder=os.path.join(BASE_DIR, 'templates'))
APP_START_TIME = time.monotonic()


def _format_uptime_seconds(total_seconds: float) -> str:
    total_seconds = max(0, int(total_seconds))
    days, remainder = divmod(total_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{days}天, {hours}小時\n{minutes}分, {seconds}秒"


def _fetch_uptime_data():
    api_key = os.getenv('UPTIMEROBOT_API_KEY')
    monitor_id = os.getenv('UPTIMEROBOT_MONITOR_ID')

    if not api_key or not monitor_id:
        return {
            'status': 'unconfigured',
            'message': '尚未設定 UPTIMEROBOT_API_KEY / UPTIMEROBOT_MONITOR_ID',
        }

    # 修正: logs 只能是 0 或 1（是否回傳事件記錄），不是筆數。
    # 另外要拿 24h/7d/30d/90d 的正常運行率，正確參數是 custom_uptime_ratios
    # （輸入天數，用 "-" 分隔），custom_uptime_ranges 是另一個參數，
    # 格式是時間戳記區間（start_end-start_end...），傳天數進去會驗證失敗。
    payload = urllib.parse.urlencode({
        'api_key': api_key,
        'format': 'json',
        'monitors': monitor_id,
        'logs': 0,
        'custom_uptime_ratios': '1-7-30-90',
        'all_time_uptime_ratio': '1',
    }).encode('utf-8')

    try:
        req = urllib.request.Request(
            'https://api.uptimerobot.com/v2/getMonitors',
            data=payload,
            headers={'Content-Type': 'application/x-www-form-urlencoded'},
            method='POST',
        )
        with urllib.request.urlopen(req, timeout=15) as response:
            data = json.loads(response.read().decode('utf-8'))
    except Exception as exc:  # pragma: no cover - runtime dependency on external API
        return {
            'status': 'error',
            'message': str(exc),
        }

    if data.get('stat') != 'ok':
        return {
            'status': 'error',
            'message': data.get('error', {}).get('message', 'UptimeRobot API 查詢失敗'),
        }

    monitors = data.get('monitors') or []
    if not monitors:
        return {
            'status': 'error',
            'message': '找不到對應的 UptimeRobot Monitor',
        }

    monitor = monitors[0]

    # 修正: 請求參數是 custom_uptime_ratios，但 UptimeRobot 回傳的欄位名稱是
    # 單數的 custom_uptime_ratio，值是用 "-" 分隔的字串（依照請求順序對應 1-7-30-90）
    custom_ranges_raw = monitor.get('custom_uptime_ratio') or ''
    ranges_list = custom_ranges_raw.split('-') if custom_ranges_raw else []

    def _range_value(index: int) -> str:
        if index < len(ranges_list) and ranges_list[index]:
            return ranges_list[index]
        return '0'

    uptime = {
        '24h': _range_value(0),
        '7d': _range_value(1),
        '30d': _range_value(2),
        '90d': _range_value(3),
        'all': monitor.get('all_time_uptime_ratio') or '0',
    }

    status_text = {
        0: '暫停',
        1: '待命',
        2: '在線',
        8: '維護',
        9: '離線',
    }.get(monitor.get('status'), '未知')

    return {
        'status': 'ok',
        'friendly_name': monitor.get('friendly_name', '機器人監控'),
        'status_code': monitor.get('status'),
        'status_text': status_text,
        'uptime': uptime,
        'logs': monitor.get('logs') or [],
    }


@app.route('/')
def home():
    return render_template('index.html')


@app.route('/api/bot-stats')
def bot_stats_api():
    bot = app.config.get('BOT')
    if bot is None:
        return jsonify({
            'status': 'unavailable',
            'message': 'Discord bot 尚未註冊到 Web 伺服器',
        })

    latency_value = getattr(bot, 'latency', None)
    if latency_value is None or not math.isfinite(latency_value):
        latency_ms = 0
    else:
        latency_ms = int(round(latency_value * 1000))

    total_servers = len(bot.guilds)
    # 修正: guild.member_count 在缺少 Server Members Intent 或快取尚未就緒時可能是 None，
    # 直接 sum() 會丟 TypeError 導致整支 API 回傳 500，前端因此全部顯示 "--"
    total_users = sum((guild.member_count or 0) for guild in bot.guilds)
    uptime_seconds = time.monotonic() - APP_START_TIME
    uptime_text = _format_uptime_seconds(uptime_seconds)
    version = os.getenv('APP_VERSION') or getattr(bot, 'version', None) or discord.__version__

    return jsonify({
        'status': 'ok',
        'guilds': total_servers,
        'users': total_users,
        'latency_ms': latency_ms,
        'uptime_seconds': uptime_seconds,
        'uptime_text': uptime_text,
        'version': version,
    })


@app.route('/api/uptime')
def uptime_api():
    return jsonify(_fetch_uptime_data())


def run():
    port = int(os.environ.get('PORT', 20198))
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)


def start_web_server():
    """Start the Flask web server in a background thread for local bot use."""
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


if __name__ == '__main__':
    run()
