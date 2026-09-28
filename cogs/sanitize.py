"""錯誤訊息脫敏工具（M2）。

背景：aiohttp 等套件丟出的例外訊息常會包含完整請求 URL，而本專案的
CWA 天氣 API 金鑰是放在 URL query string（Authorization=...），一旦把
例外原文 print 到後台 log 或 DM 給開發者，金鑰就會外洩到：

  - 雲端平台的常駐 log（可能被平台留存、被有權限的第三方檢視）
  - 開發者的 Discord DM 歷史

另外 web_server 的公開 API 也不該把原始例外字串回傳給未認證訪客。

統一做法：所有要進 log、DM、或對外回應的外部來源文字，先過
`redact_secrets()` 把敏感的 query string 參數與 Bearer token 換成
[REDACTED]。原文仍會保留足夠的除錯資訊（錯誤類型、主機名稱等）。
"""
import re

# 會出現在 URL query string 或 body 裡、值得遮蔽的參數名稱。
_SECRET_PARAM_PATTERN = re.compile(
    r'\b(authorization|api[_-]?key|access[_-]?token|token|client[_-]?secret|'
    r'client[_-]?id|password|secret)'
    r'=([^&\s\'"<>\]]+)',
    re.IGNORECASE,
)

# Authorization header 形式（例如 TDX API 的 "Bearer xxx"）。
_BEARER_PATTERN = re.compile(
    r'\bBearer\s+[A-Za-z0-9\-._~+/]+=*',
    re.IGNORECASE,
)

_REPLACEMENT = r'\1=[REDACTED]'


def redact_secrets(text: object) -> str:
    """把字串中的機密參數值（query string 與 Bearer token）換成 [REDACTED]。"""
    if text is None:
        return ''
    result = _SECRET_PARAM_PATTERN.sub(_REPLACEMENT, str(text))
    result = _BEARER_PATTERN.sub('Bearer [REDACTED]', result)
    return result
