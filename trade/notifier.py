import os
import ast
import json
import requests

ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")


def load_line_config(env_path: str = ENV_PATH):
    """
    每次發送時動態從 trade/.env 讀取 USER_ID 與 LINE_TOKENS。
    支援格式：
      USER_ID='...' / USER_ID="..." / LINE_USER_ID=...
      LINE_TOKENS='["token1", "token2", ...]' 或逗號分隔字串
    """
    user_id = ""
    line_tokens = []

    if not os.path.exists(env_path):
        print(f"[LINE Push] 警告: 找不到 .env 檔案: {env_path}")
        return user_id, line_tokens

    try:
        with open(env_path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue

                for delim in ["=", ":"]:
                    if delim in line:
                        k, v = line.split(delim, 1)
                        k = k.strip().upper()
                        v = v.strip().strip("'").strip('"')

                        if k in ("USER_ID", "LINE_USER_ID"):
                            user_id = v
                        elif k in ("LINE_TOKENS", "LINE_TOKEN"):
                            parsed = False
                            # 1. 嘗試 JSON 解析
                            if v.startswith("[") and v.endswith("]"):
                                try:
                                    line_tokens = json.loads(v)
                                    parsed = True
                                except Exception:
                                    try:
                                        line_tokens = ast.literal_eval(v)
                                        parsed = True
                                    except Exception:
                                        pass

                            # 2. 若非列表格式或解析失敗，嘗試逗號分隔
                            if not parsed:
                                if "," in v:
                                    line_tokens = [t.strip().strip("'").strip('"') for t in v.split(",") if t.strip()]
                                elif v:
                                    line_tokens = [v]
                        break
    except Exception as e:
        print(f"[LINE Push] 警告: 讀取 .env 失敗: {e}")

    return user_id, line_tokens


def send_push_message(message_text: str) -> bool:
    """
    Directly push message to LINE with multi-token failover.
    每次發送時動態從 trade/.env 讀取最新的 USER_ID 與 LINE_TOKENS。
    """
    user_id, line_tokens = load_line_config()

    if not user_id:
        print("[LINE Push] 錯誤: trade/.env 內缺少 USER_ID 設定。")
        return False

    if not line_tokens:
        print("[LINE Push] 錯誤: trade/.env 內缺少 LINE_TOKENS 設定。")
        return False

    url = 'https://api.line.me/v2/bot/message/push'
    message_payload = {
        'to': user_id,
        'messages': [{'type': 'text', 'text': str(message_text)}]
    }

    for i, current_token in enumerate(line_tokens):
        headers = {
            'Content-Type': 'application/json',
            'Authorization': 'Bearer ' + current_token
        }
        try:
            response = requests.post(url, headers=headers, json=message_payload, timeout=10)
            if response.status_code == 200:
                print(f"[LINE Push] Token {i + 1} 發送成功")
                return True
            print(f"[LINE Push] Token {i + 1} 失敗，狀態碼: {response.status_code}，準備嘗試下一個。")
            if i == len(line_tokens) - 1:
                print("[LINE Push] 所有 LINE Token 皆已失效或達到額度上限。")
        except Exception as e:
            print(f"[LINE Push] Token {i + 1} 連線出錯: {e}")

    return False


def send_trade_notification(symbol: str, message: str, payload_data=None) -> bool:
    """
    Format and send trade notification matching GAS template:
    ${symbolPrefix}${responseData.message}\n\n原始訊號：\n${payloadString}
    """
    symbol_prefix = f"[{symbol}] " if symbol else ""
    if payload_data is not None:
        if isinstance(payload_data, (dict, list)):
            payload_str = json.dumps(payload_data, ensure_ascii=False, indent=2)
        else:
            payload_str = str(payload_data)
        full_text = f"{symbol_prefix}{message}\n\n原始訊號：\n{payload_str}"
    else:
        full_text = f"{symbol_prefix}{message}"

    return send_push_message(full_text)


def __getattr__(name):
    """
    向下相容：若有模組直接讀取 notifier.USER_ID 或 notifier.LINE_TOKENS，
    自動動態自 trade/.env 讀取回傳。
    """
    if name == "USER_ID":
        uid, _ = load_line_config()
        return uid
    elif name == "LINE_TOKENS":
        _, tokens = load_line_config()
        return tokens
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")
