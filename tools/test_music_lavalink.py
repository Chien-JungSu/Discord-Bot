"""獨立驗證腳本：驗證 cogs/music.py 的 Lavalink 連線診斷與自動重連（不需要連上 Discord）。

涵蓋項目：
- diagnose_lavalink_node 把節點回應翻譯成人話（200 / 401 / 404 / 其他狀態碼）
- https:// 指到只講明文 HTTP 的節點時，自動改探 http:// 並給出「請把 LAVALINK_URI 改成…」的可行動建議
- 探測失敗（逾時／連線被拒／一般 ClientError／未預期錯誤）的中文訊息
- 診斷訊息永遠不會漏出 LAVALINK_PASSWORD、診斷本身絕不拋例外
- _connect_lavalink 偵測「Pool.connect 不拋例外但節點沒註冊」的靜默失敗
- 連線逾時時丟出的 TimeoutError 帶有訊息（修正開發者 DM「錯誤訊息」欄位空白）
- 失敗時呼叫 node.close(eject=True) 清理殘留節點與 session
- 每輪故障只 DM 一次；連線成功會重置「已通知」旗標
- node_health_check 在 Pool 為空時自動重連、受冷卻保護、連線中不重入
- node_health_check 對已註冊的 DISCONNECTED 節點仍維持只通知一次的行為

執行方式：.venv/Scripts/python.exe tools/test_music_lavalink.py
"""
import sys

sys.stdout.reconfigure(encoding='utf-8')  # Windows 主控台 cp950 印不出 emoji，先切到 utf-8

import asyncio
import os
import time
from types import SimpleNamespace

# 讓 import cogs.music 可運作（腳本位於 tools/，專案根目錄是上一層）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import aiohttp
import wavelink
from aiohttp.client_reqrep import ConnectionKey

import cogs.music as music

PASS: list[str] = []
FAIL: list[str] = []


def check(cond: bool, name: str):
    (PASS if cond else FAIL).append(name)
    print(('✅' if cond else '❌') + f' {name}')


class FakeBot:
    """notify_owner_error 的替身：只記錄呼叫，不真的送 DM。"""

    def __init__(self):
        self.notifications: list[dict] = []

    async def notify_owner_error(self, error, interaction=None, extra_info=''):
        self.notifications.append({'error': error, 'extra_info': extra_info})


def make_probe(status=None, body='', exc=None, record=None):
    """替換 _probe_lavalink_version：回傳預設結果，並記錄被探測過的 URL。"""

    async def fake(url, headers, timeout):
        if record is not None:
            record.append((url, dict(headers)))
        return status, body, exc

    return fake


def connector_error(host='zac.hidencloud.com', port=24686, os_error=None):
    key = ConnectionKey(host, port, True, None, None, None, None)
    return aiohttp.ClientConnectorError(key, os_error or OSError(111, 'Connection refused'))


# ---------- 1. 節點回應 / 例外 → 中文診斷 ----------
async def test_diagnose():
    orig_probe = music._probe_lavalink_version
    password = 'super-secret-pw'
    try:
        music._probe_lavalink_version = make_probe(200, '4.2.2')
        msg = await music.diagnose_lavalink_node('http://node.example:2333', password)
        check('200' in msg and '4.2.2' in msg, '節點回 200：診斷帶出 Lavalink 版本號')
        check('WebSocket' in msg, '節點回 200 但仍然逾時：提示檢查 WebSocket 升級')

        music._probe_lavalink_version = make_probe(401, 'Unauthorized')
        msg = await music.diagnose_lavalink_node('http://node.example:2333', password)
        check('401' in msg and 'LAVALINK_PASSWORD' in msg, '401：指出是 LAVALINK_PASSWORD 錯誤')

        music._probe_lavalink_version = make_probe(404, 'Not Found')
        msg = await music.diagnose_lavalink_node('http://node.example:2333', password)
        check('404' in msg and 'LAVALINK_URI' in msg, '404：指出 LAVALINK_URI 指錯服務')

        music._probe_lavalink_version = make_probe(500, 'oops')
        msg = await music.diagnose_lavalink_node('http://node.example:2333', password)
        check('HTTP 500' in msg, '其他狀態碼：帶出實際 HTTP 狀態碼')

        # 例外類型的翻譯（這幾項不經過 probe，直接測純函式）
        msg = music._describe_lavalink_probe_error('http://n:1', TimeoutError(), 5.0)
        check('逾時' in msg, '探測逾時 → 顯示逾時秒數')
        msg = music._describe_lavalink_probe_error('http://n:1', connector_error(), 5.0)
        check('無法連線' in msg and 'http://n:1' in msg, '連線被拒 → 帶出網址與原因')
        msg = music._describe_lavalink_probe_error('http://n:1', aiohttp.ClientError('boom'), 5.0)
        check('ClientError' in msg, '一般 aiohttp ClientError → 帶出例外型別')
        msg = music._describe_lavalink_probe_error('http://n:1', ValueError('weird'), 5.0)
        check('未預期錯誤' in msg and 'ValueError' in msg, '未預期錯誤 → 帶出例外型別')

        # 本次事故的根因：https:// 指到只講明文 HTTP 的節點
        record: list = []
        music._probe_lavalink_version = _https_then_http(
            record, https_exc=connector_error(os_error=OSError(92, 'SSL wrong version number'))
        )
        msg = await music.diagnose_lavalink_node('https://zac.hidencloud.com:24686', password)
        check(len(record) == 2 and record[0][0].startswith('https://') and record[1][0].startswith('http://'),
              'https:// 探測失敗 → 自動改探同網址的 http://')
        check('改成「http://zac.hidencloud.com:24686」' in msg,
              '診斷直接給出「把 LAVALINK_URI 從…改成…」的可行動建議')
        check('https://' in msg and 'TLS' in msg, '訊息點明根因是 https://（TLS 握手失敗）')

        # https 失敗 + http 也失敗 → 訊息要把兩邊的失敗都講清楚
        record2: list = []
        music._probe_lavalink_version = _https_then_http(
            record2, https_exc=connector_error(), http_exc=TimeoutError()
        )
        msg = await music.diagnose_lavalink_node('https://node.example:2333', password)
        check('同樣失敗' in msg, 'https 與 http 都失敗：訊息同時帶出兩邊的錯誤')

        # https 失敗、http 通了但密碼錯 → 一併提醒密碼問題
        record3: list = []
        music._probe_lavalink_version = _https_then_http(
            record3, https_exc=connector_error(), http_status=401
        )
        msg = await music.diagnose_lavalink_node('https://node.example:2333', password)
        check('LAVALINK_PASSWORD' in msg, 'http:// 通但 401：診斷一併提醒密碼也錯了')

        check(password not in msg, '診斷訊息不包含密碼')

        # 診斷本身絕不拋例外（連 probe 丟未預期錯誤也要回一句話）
        async def evil(url, headers, timeout):
            raise RuntimeError('probe exploded')

        music._probe_lavalink_version = evil
        msg = await music.diagnose_lavalink_node('http://node.example:2333', password)
        check(bool(msg) and 'RuntimeError' in msg, 'probe 抛未預期例外：診斷仍回傳訊息而不炸掉')
        check(password not in msg, '例外路徑的診斷訊息也不含密碼')
    finally:
        music._probe_lavalink_version = orig_probe


def _https_then_http(record, https_exc=None, http_exc=None, http_status=200, http_body='4.2.2'):
    """https:// 探測失敗（或成功）後，第二次改探 http:// 的替身。"""

    async def fake(url, headers, timeout):
        record.append((url, dict(headers)))
        if url.startswith('https://'):
            return (None, '', https_exc) if https_exc is not None else (200, '4.2.2', None)
        if http_exc is not None:
            return None, '', http_exc
        return http_status, http_body, None

    return fake


# ---------- 2. _connect_lavalink：靜默失敗、逾時訊息、清理、通知去重 ----------
async def test_connect_lavalink():
    orig_connect = wavelink.Pool.__dict__['connect']
    orig_close = wavelink.Node.__dict__['close']
    orig_diagnose = music.diagnose_lavalink_node
    orig_timeout = music.LAVALINK_CONNECT_TIMEOUT
    pool_nodes = getattr(wavelink.Pool, '_Pool__nodes')
    closed: list = []

    async def spy_close(self, eject=False):
        closed.append((self.uri, eject))
        await orig_close(self, eject=eject)  # 真的執行一次，確認清理流程本身不炸

    async def fake_diagnose(uri, password, timeout=music.LAVALINK_PROBE_TIMEOUT):
        return f'（測試診斷）{uri} 無法連線，請檢查節點。'

    async def fake_no_register(*, nodes, client=None, cache_capacity=None):
        # 實測的 wavelink 行為：連不上時不拋例外、只回傳沒註冊節點的 dict
        return {}

    async def fake_register(*, nodes, client=None, cache_capacity=None):
        for n in nodes:
            pool_nodes[n.identifier] = n
        return dict(pool_nodes)

    async def fake_hang(*, nodes, client=None, cache_capacity=None):
        await asyncio.sleep(60)

    wavelink.Node.close = spy_close
    music.diagnose_lavalink_node = fake_diagnose
    try:
        pool_nodes.clear()

        # 2-1 靜默失敗：Pool.connect 回空 dict、節點沒註冊
        bot = FakeBot()
        cog = music.Music(bot)
        wavelink.Pool.connect = fake_no_register
        await cog._connect_lavalink('http://node.example:2333', 'pw')
        check(len(bot.notifications) == 1, '節點沒註冊：回報開發者一次')
        err = bot.notifications[0]['error']
        check(str(err) != '', '回報的錯誤訊息非空白（修正 DM 錯誤訊息空白的問題）')
        check('未註冊' in str(err) or '拒絕' in str(err), '錯誤訊息說明 Pool.connect 沒有註冊節點')
        check('（測試診斷）' in bot.notifications[0]['extra_info'],
              'extra_info 帶上節點診斷結果')
        check(closed and closed[-1][1] is True, '失敗後呼叫 node.close(eject=True) 清理')
        check(cog._lavalink_failure_notified is True, '標記「已通知」避免重試洗版')

        # 2-2 同一輪故障的第二次失敗 → 不再 DM（log 照樣印）
        await cog._connect_lavalink('http://node.example:2333', 'pw')
        check(len(bot.notifications) == 1, '同一輪故障重試不會重複 DM 開發者')

        # 2-3 連線逾時：TimeoutError 必須帶有訊息
        bot2 = FakeBot()
        cog2 = music.Music(bot2)
        wavelink.Pool.connect = fake_hang
        music.LAVALINK_CONNECT_TIMEOUT = 0.1
        t0 = time.monotonic()
        await cog2._connect_lavalink('http://slow.example:2333', 'pw')
        elapsed = time.monotonic() - t0
        music.LAVALINK_CONNECT_TIMEOUT = orig_timeout
        check(elapsed < 5, f'連線逾時受 LAVALINK_CONNECT_TIMEOUT 限制（實際 {elapsed:.2f} 秒）')
        check(len(bot2.notifications) == 1, '逾時：回報開發者一次')
        err2 = bot2.notifications[0]['error']
        check(isinstance(err2, TimeoutError), '逾時回報的錯誤型別是 TimeoutError')
        check(str(err2) != '' and '逾時' in str(err2) and 'slow.example' in str(err2),
              '逾時錯誤訊息非空白且帶有原因與網址')
        check('（測試診斷）' in bot2.notifications[0]['extra_info'], '逾時回報也附上診斷')
        check(closed and closed[-1][1] is True, '逾時後一樣清掉失敗的節點')

        # 2-4 連線成功：節點真的註冊進 Pool → 不通知、不清理、重置已通知旗標
        pool_nodes.clear()
        bot3 = FakeBot()
        cog3 = music.Music(bot3)
        cog3._lavalink_failure_notified = True  # 預設上次故障中
        closed_before = len(closed)
        wavelink.Pool.connect = fake_register
        await cog3._connect_lavalink('http://good.example:2333', 'pw')
        check(len(pool_nodes) == 1, '連線成功：節點真的註冊進 Pool')
        check(len(bot3.notifications) == 0, '連線成功：不回報錯誤')
        check(cog3._lavalink_failure_notified is False, '連線成功：重置「已通知」旗標')
        check(len(closed) == closed_before, '連線成功：不會把節點關掉')
        # wavelink 的 Node 一建立就開 aiohttp session，測試結束要自己收掉，
        # 否則解釋器結束時會噴 Unclosed client session 警告。
        for leftover in list(pool_nodes.values()):
            sess = getattr(leftover, '_session', None)
            if sess is not None and not sess.closed:
                await sess.close()
        pool_nodes.clear()

        # 2-5 重入保護：連線中不允許再開一條
        bot4 = FakeBot()
        cog4 = music.Music(bot4)
        called = []

        async def counting_connect(*, nodes, client=None, cache_capacity=None):
            called.append(1)
            return {}

        wavelink.Pool.connect = counting_connect
        cog4._lavalink_connecting = True
        await cog4._connect_lavalink('http://node.example:2333', 'pw')
        check(len(called) == 0, '已有連線程序在跑時不會重入（避免開兩條 WebSocket）')
        cog4._lavalink_connecting = False
    finally:
        wavelink.Pool.connect = orig_connect
        wavelink.Node.close = orig_close
        music.diagnose_lavalink_node = orig_diagnose
        music.LAVALINK_CONNECT_TIMEOUT = orig_timeout
        pool_nodes.clear()


# ---------- 3. node_health_check：空 Pool 自動重連 + 冷卻 + 既有通知行為 ----------
async def test_health_check():
    pool_nodes = getattr(wavelink.Pool, '_Pool__nodes')
    pool_nodes.clear()
    orig_uri = os.environ.get('LAVALINK_URI')
    orig_pw = os.environ.get('LAVALINK_PASSWORD')
    os.environ['LAVALINK_URI'] = 'http://node.example:2333'
    os.environ['LAVALINK_PASSWORD'] = 'pw'
    coro = music.Music.node_health_check.coro
    try:
        bot = FakeBot()
        cog = music.Music(bot)
        calls: list[tuple] = []

        async def fake_connect(uri, pwd):
            calls.append((uri, pwd))

        cog._connect_lavalink = fake_connect  # 實例屬性遮住方法，避免真的連外

        # 3-1 Pool 為空 → 自動重連
        await coro(cog)
        check(len(calls) == 1, 'Pool 為空時 health check 觸發自動重連')
        check(calls[0] == ('http://node.example:2333', 'pw'), '重連使用環境變數裡的節點與密碼')

        # 3-2 冷卻保護：短時間內不重複重連
        await coro(cog)
        check(len(calls) == 1, f'冷卻（{music.LAVALINK_RECONNECT_COOLDOWN:g} 秒）內不重複重連')

        # 3-3 連線中不重入
        cog._lavalink_last_reconnect_attempt = 0.0
        cog._lavalink_connecting = True
        await coro(cog)
        check(len(calls) == 1, '連線進行中不重入（不開第二條 WebSocket）')

        # 3-4 冷卻過了就再次嘗試
        cog._lavalink_connecting = False
        await coro(cog)
        check(len(calls) == 2, '冷卻結束後會再次嘗試重連')

        # 3-5 沒設定 LAVALINK_URI → 不做無意義的重連
        os.environ.pop('LAVALINK_URI', None)
        cog._lavalink_last_reconnect_attempt = 0.0
        await coro(cog)
        check(len(calls) == 2, '未設定 LAVALINK_URI 時不自動重連')

        # 3-6 Pool 有已連線節點 → 不重連、不通知
        os.environ['LAVALINK_URI'] = 'http://node.example:2333'
        cog._lavalink_last_reconnect_attempt = 0.0
        pool_nodes['node-1'] = SimpleNamespace(uri='http://node.example:2333',
                                               status=SimpleNamespace(name='CONNECTED'))
        await coro(cog)
        check(len(calls) == 2, '節點已連線時不觸發重連')
        check(len(bot.notifications) == 0, '節點已連線時不通知開發者')

        # 3-7 離線節點 → 只通知一次（既有行為不能被改壞）
        pool_nodes['node-1'].status = SimpleNamespace(name='DISCONNECTED')
        await coro(cog)
        check(len(bot.notifications) == 1, '節點離線：通知開發者一次')
        check('離線' in str(bot.notifications[0]['error']), '離線通知的錯誤訊息非空白')
        await coro(cog)
        check(len(bot.notifications) == 1, '持續離線不會每輪重複通知')
        pool_nodes['node-1'].status = SimpleNamespace(name='CONNECTED')
        await coro(cog)
        pool_nodes['node-1'].status = SimpleNamespace(name='DISCONNECTED')
        await coro(cog)
        check(len(bot.notifications) == 2, '恢復後再次離線會重新通知')
        check(len(calls) == 2, '離線節點仍在 Pool 中時不會改走重連分支')
    finally:
        pool_nodes.clear()
        if orig_uri is None:
            os.environ.pop('LAVALINK_URI', None)
        else:
            os.environ['LAVALINK_URI'] = orig_uri
        if orig_pw is None:
            os.environ.pop('LAVALINK_PASSWORD', None)
        else:
            os.environ['LAVALINK_PASSWORD'] = orig_pw


# ---------- 4. 實機探測（網路不可用只列資訊，不計入失敗） ----------
async def test_live_probe():
    from dotenv import load_dotenv
    load_dotenv()
    uri = os.getenv('LAVALINK_URI')
    password = os.getenv('LAVALINK_PASSWORD')
    if not uri or not password:
        print('ℹ️ 未設定 LAVALINK_URI／LAVALINK_PASSWORD，略過實機探測')
        return
    try:
        msg = await asyncio.wait_for(
            music.diagnose_lavalink_node(uri, password), timeout=20
        )
    except Exception as exc:  # 網路環境不穩定 → 只列資訊
        print(f'ℹ️ 實機探測無法完成（{type(exc).__name__}: {exc}），略過')
        return
    check(bool(msg), f'實機診斷有回傳訊息：{msg[:80]}')
    check(password not in msg, '實機診斷訊息不含密碼')


async def main():
    await test_diagnose()
    await test_connect_lavalink()
    await test_health_check()
    await test_live_probe()

    print()
    print(f'共 {len(PASS) + len(FAIL)} 項測試：{len(PASS)} 通過，{len(FAIL)} 失敗')
    if FAIL:
        print('失敗項目：')
        for name in FAIL:
            print(f'  - {name}')
        sys.exit(1)


if __name__ == '__main__':
    asyncio.run(main())
