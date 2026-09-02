import json
import requests

LINE_TOKENS = [
    'QHYd/p7erDBwt5JSv1mGEbsEwrhSX0poQ5rY9PJATrm37zCVuIH+I7Wl8uioLdnn84wXTpnmAbbxcMSjAoYdl3M81naXFhHRL5rRbo+1LQL4kBmeYFHnmjf9N7KNkjLuICwt8jDTqNpTFrIzxACIsgdB04t89/1O/w1cDnyilFU=',
    '9NrIGW3Faov9BVp4WzhJ61+ldqXtDJV2J7aC5NeE9+7x3MmuMUBfmVb4/g9Ww4Xtcp/IZbTsiFTKkYUHH7Og2Ks6Z+FCyl8nhTR3cHnSfSm2G4Hx1H7DzABdo2u77IOJobyH1oq8s3Frzh7bqJWElAdB04t89/1O/w1cDnyilFU=',
    'FCp+BYIkkZnMU35ByxHe3CG4lGDrKXw2zkM4Mc2naXrSUcdomGzOf10CbmgmkNeSckMvUMxJKutRqIuP4Jy9Wwi+vIADVPVG6dVFcWPwU1GDPMZBtkTeU6yD+d9kXF3CGs1KjaGdK+0kC+L5OQQJ7QdB04t89/1O/w1cDnyilFU=',
    'nalX/ax//FnaN+4m59acghkXKuAtvEQAJ3EWNVa+CpSn+uGlPLHh4uPJo/LugkTBN6VocHtSmY1fO5E4zDctZZUS8UoojlH+RWQjERWdZG+T0Nx8IdjQuvorb6vLKXtGon5WOnYFeDtOJM1aI/dWIQdB04t89/1O/w1cDnyilFU=',
    'iYPqSdfErvsqSm99irxjTPNwURueqgMXnLX4VHNNnpOttsQL/nnJiSOTjdexFyinB5X5grVq4DTRhTmxzPTXICytgvrYQIhCDtK8qmBvJmF/HYF171+8dSE9xe/nmBMomBiRrYiJDejuL/PE6YWo2gdB04t89/1O/w1cDnyilFU='
]
USER_ID = 'U974efcbaa3f4df1caff9b48e04a8d9e0'

def send_push_message(message_text: str) -> bool:
    """
    Directly push message to LINE with multi-token failover
    """
    url = 'https://api.line.me/v2/bot/message/push'
    message_payload = {
        'to': USER_ID,
        'messages': [{'type': 'text', 'text': str(message_text)}]
    }

    for i, current_token in enumerate(LINE_TOKENS):
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
            if i == len(LINE_TOKENS) - 1:
                print("[LINE Push] 所有 LINE Token 皆已失效或達到額度上限。")
        except Exception as e:
            print(f"[LINE Push] Token {i + 1} 連線出錯: {e}")

    return False

def send_trade_notification(symbol: str, message: str, payload_data = None) -> bool:
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
