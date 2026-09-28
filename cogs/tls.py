"""共用的 TLS/SSL 設定。

背景：Python 3.14 起 `ssl.create_default_context()` 預設開啟
`VERIFY_X509_STRICT`（RFC 5280 嚴格模式），會強制檢查 CA 憑證上的
Subject Key Identifier 等擴充欄位。部分政府網站（中央氣象署 CWA、
交通部 TDX）的憑證鏈上游 CA 是在舊規範時代簽發的、缺少 SKI 欄位，
在新版 OpenSSL 嚴格模式下會回報：

    [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed:
    Missing Subject Key Identifier

但同一張憑證鏈在瀏覽器與歷來的寬鬆模式下一直都被正常信任，並不是
「網站沒有憑證」。

處理方式：只關閉嚴格模式這一項旗標。CA 信任鏈驗證與 hostname 驗證
全部保留，因此中間人攻擊（MITM）仍會被攔截；我們只是容忍「CA 憑證
缺 SKI 欄位」這類歷史遗留的合規瑕疵，而不是像 `ssl=False` 一樣完全
放棄驗證。所有對外 HTTPS 請求都應使用這裡產生的 context。
"""
import ssl

import certifi


def create_verified_ssl_context() -> ssl.SSLContext:
    """建立「會驗證伺服器憑證」的 SSL context，相容舊式政府 CA 憑證鏈。"""
    context = ssl.create_default_context(cafile=certifi.where())
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED

    # 只關閉 RFC 5280 嚴格模式；在沒有這個旗標的舊版 Python 上是 no-op。
    strict_flag = getattr(ssl, "VERIFY_X509_STRICT", 0)
    context.verify_flags &= ~strict_flag

    return context
