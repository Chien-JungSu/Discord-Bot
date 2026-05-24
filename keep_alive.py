from flask import Flask
from threading import Thread

app = Flask('')

@app.route('/')
def home():
    return "機器人正在運作中！" # 這是網頁顯示的文字，你可以隨便改

def run():
    # 設定讓伺服器監聽 0.0.0.0，這樣 Render 才能偵測到
    app.run(host='0.0.0.0', port=8080)

def keep_alive():
    # 使用執行緒（Thread）讓網頁與機器人可以同時運作
    t = Thread(target=run)
    t.start()