// ================= 設定區 =================
const NGROK_URL = 'https://imelda-nondeferent-deformedly.ngrok-free.dev/webhook';
const USER_ID = 'U974efcbaa3f4df1caff9b48e04a8d9e0'; 

// 將多個 Token 放入陣列，方便自動切換
const LINE_TOKENS = [
  'QHYd/p7erDBwt5JSv1mGEbsEwrhSX0poQ5rY9PJATrm37zCVuIH+I7Wl8uioLdnn84wXTpnmAbbxcMSjAoYdl3M81naXFhHRL5rRbo+1LQL4kBmeYFHnmjf9N7KNkjLuICwt8jDTqNpTFrIzxACIsgdB04t89/1O/w1cDnyilFU=', 
  '9NrIGW3Faov9BVp4WzhJ61+ldqXtDJV2J7aC5NeE9+7x3MmuMUBfmVb4/g9Ww4Xtcp/IZbTsiFTKkYUHH7Og2Ks6Z+FCyl8nhTR3cHnSfSm2G4Hx1H7DzABdo2u77IOJobyH1oq8s3Frzh7bqJWElAdB04t89/1O/w1cDnyilFU=', 
  'FCp+BYIkkZnMU35ByxHe3CG4lGDrKXw2zkM4Mc2naXrSUcdomGzOf10CbmgmkNeSckMvUMxJKutRqIuP4Jy9Wwi+vIADVPVG6dVFcWPwU1GDPMZBtkTeU6yD+d9kXF3CGs1KjaGdK+0kC+L5OQQJ7QdB04t89/1O/w1cDnyilFU='  
];
// ==========================================

function doPost(e) {
  const payloadString = e.postData.contents;
  const success = forwardToNgrok(payloadString);

  if (success) {
    // 這裡我們不再推播 "✅ 已順利送達 Ngrok！"，避免干擾下單邏輯的重要訊息
    // 若你想保留，可以取消註解下方這行：
    //sendPushMessage("✅ 已順利送達 Ngrok！\n" + payloadString);
  } else {
    scheduleRetry(payloadString);
    sendPushMessage("⚠️ Ngrok 無回應，已排程 1 分鐘後重試！\n" + payloadString);
  }

  return ContentService.createTextOutput(JSON.stringify({"status": "received"}))
    .setMimeType(ContentService.MimeType.JSON);
}

function forwardToNgrok(payloadString) {
  const options = {
    'method': 'post',
    'contentType': 'application/json',
    'payload': payloadString,
    'muteHttpExceptions': true 
  };

  try {
    const response = UrlFetchApp.fetch(NGROK_URL, options);
    const responseCode = response.getResponseCode();

    // 200 代表 Ngrok 與 Python 伺服器成功對接
    if (responseCode === 200) {
      // 解析 Python 傳回來的 JSON 內容
      const responseData = JSON.parse(response.getContentText());
      
      // 如果 Python 有回傳特殊的 message，代表有觸發我們的特定條件
      if (responseData.message) {
        if (responseData.message === "庫存為+1，這次不下單" || 
            responseData.message === "庫存為-1，這次不下單" || 
            responseData.message === "這次有下單，但下單失敗" ||
            responseData.message.startsWith("這次有下單，狀態:")) { // <=== ✨ 新增這一行
            
            // 觸發條件，推播給 LINE (加入 \n 換行讓排版更好看)
            sendPushMessage(`${responseData.message}\n\n原始訊號：\n${payloadString}`);
        }
      }
      return true;
    }
    return false;
  } catch (err) {
    // 日誌中也加入分隔符號，方便後台除錯閱讀
    Logger.log('Ngrok 連線失敗: ' + err.message + ' | Payload: ' + payloadString);
    return false;
  }
}

function scheduleRetry(payloadString) {
  const retryId = 'RETRY_' + new Date().getTime();
  PropertiesService.getScriptProperties().setProperty(retryId, payloadString);
  ScriptApp.newTrigger('processRetry')
           .timeBased()
           .after(3 * 60 * 1000)
           .create();
}

function processRetry() {
  const props = PropertiesService.getScriptProperties();
  const keys = props.getKeys();

  for (let i = 0; i < keys.length; i++) {
    const key = keys[i];
    if (key.startsWith('RETRY_')) {
      const payloadString = props.getProperty(key);
      const success = forwardToNgrok(payloadString);

      if (success) {
        sendPushMessage("✅ [GAS 重試成功] 訊號已送達 Ngrok！");
        props.deleteProperty(key); 
      } else {
        sendPushMessage("❌ [GAS 重試失敗] 重試依然失敗，已放棄。請檢查伺服器！");
        props.deleteProperty(key);
      }
    }
  }

  const triggers = ScriptApp.getProjectTriggers();
  for (let i = 0; i < triggers.length; i++) {
    if (triggers[i].getHandlerFunction() === 'processRetry') {
      ScriptApp.deleteTrigger(triggers[i]);
    }
  }
}

function sendPushMessage(messageText) {
  const url = 'https://api.line.me/v2/bot/message/push';
  
  const messagePayload = {
    'to': USER_ID,
    'messages': [{ 'type': 'text', 'text': messageText }]
  };

  for (let i = 0; i < LINE_TOKENS.length; i++) {
    const currentToken = LINE_TOKENS[i];
    
    const options = {
      'method': 'post',
      'contentType': 'application/json',
      'headers': { 'Authorization': 'Bearer ' + currentToken },
      'payload': JSON.stringify(messagePayload),
      'muteHttpExceptions': true 
    };

    try {
      const response = UrlFetchApp.fetch(url, options);
      const responseCode = response.getResponseCode();

      if (responseCode === 200) {
        Logger.log('Token ' + (i + 1) + ' 發送成功');
        return true; 
      } 
      
      Logger.log('Token ' + (i + 1) + ' 失敗，狀態碼: ' + responseCode + '，準備嘗試下一個。');
      if (i === LINE_TOKENS.length - 1) {
        Logger.log('所有 LINE Token 皆已失效或達到額度上限。');
      }
    } catch (e) {
      Logger.log('Token ' + (i + 1) + ' 連線出錯：' + e.message);
    }
  }
  return false;
}
