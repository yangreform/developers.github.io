import re

def modify_file():
    with open('docs/index.html', 'r', encoding='utf-8') as f:
        html = f.read()

    # 1. Nav link
    old_nav = '<a class="nav-link btn-cta" href="about.html">&#11088; 優質百萬團隊</a>'
    new_nav = '<!-- <a class="nav-link btn-cta" href="about.html">&#11088; 優質百萬團隊</a> -->'
    html = html.replace(old_nav, new_nav)

    # 2. Hero CTA
    old_hero_cta = '<a href="about.html" class="btn-gold-lg"><i class="bi bi-person-fill"></i> 聯絡顧問</a>'
    new_hero_cta = '<!-- <a href="about.html" class="btn-gold-lg"><i class="bi bi-person-fill"></i> 聯絡顧問</a> -->'
    html = html.replace(old_hero_cta, new_hero_cta)

    # 3. Feature Card
    old_feat = '''<a href="about.html" class="feat-card gold-hover">
      <div class="feat-icon gold">&#127775;</div>
      <div class="feat-title">優質百萬團隊</div>
      <div class="feat-desc">由資深顧問台灣小伶 Melody 帶領，提供 HDB 組屋、URA 私宅與海外買家全程服務，中英雙語，貼心到位。</div>
      <span class="feat-link">了解更多 &#8594;</span>
    </a>'''
    new_feat = '<!-- ' + old_feat + ' -->'
    html = html.replace(old_feat, new_feat)

    # 4. CTA Band & Footer
    # Find start of <!-- CTA Band --> and end of </footer>
    start_idx = html.find('<!-- CTA Band -->')
    end_idx = html.find('</footer>') + 9

    if start_idx != -1 and end_idx != -1:
        new_bottom = '''<!-- Disclaimer & Contact Section -->
<div class="cta-band" id="contact">
  <div style="max-width:800px; margin:0 auto; text-align:left;">
    <h2 style="text-align:center;">聯絡我們與功能建議</h2>
    <p style="text-align:center; color:var(--muted); font-size:14px; margin-bottom:30px;">
      ⚠️ <strong>聲明：本站非房屋仲介網頁</strong>，僅作為新加坡房市即時現況與數據視覺化平台。<br>
      若您有任何功能建議，或有實際的買賣/租賃需求需要推薦專業資源，歡迎填寫下方表單聯絡我們。
    </p>

    <!-- Contact Form -->
    <style>
      .form-group{margin-bottom:15px}
      .form-label{display:block;margin-bottom:6px;font-size:13px;color:var(--muted)}
      .form-input,.form-textarea{width:100%;padding:12px;background:var(--panel);border:1px solid var(--border);border-radius:8px;color:var(--text);font-family:inherit;font-size:14px;box-sizing:border-box}
      .form-input:focus,.form-textarea:focus{outline:none;border-color:var(--gold)}
      .form-textarea{resize:vertical;min-height:100px}
      .btn-submit{display:inline-block;width:100%;padding:14px;background:linear-gradient(135deg,var(--gold),#e0b82b);color:#0a0e1a;border:none;border-radius:8px;cursor:pointer;font-weight:bold;font-size:16px;margin-top:10px;transition:0.2s;}
      .btn-submit:hover{opacity:0.9; transform:translateY(-1px);}
      #form-success{display:none;text-align:center;padding:20px;color:#4ade80;font-weight:500}
    </style>
    
    <form id="contact-form">
      <div class="form-group">
        <label class="form-label" for="form-name">姓名 / 稱呼</label>
        <input class="form-input" id="form-name" type="text" placeholder="請輸入您的姓名" required>
      </div>
      <div class="form-group">
        <label class="form-label" for="form-email">Email</label>
        <input class="form-input" id="form-email" type="email" placeholder="example@email.com" required>
      </div>
      <div class="form-group">
        <label class="form-label" for="form-phone">電話 / WhatsApp (選填)</label>
        <input class="form-input" id="form-phone" type="tel" placeholder="+65 XXXX XXXX">
      </div>
      <div class="form-group">
        <label class="form-label" for="form-msg">建議或需求</label>
        <textarea class="form-textarea" id="form-msg" placeholder="請描述您的功能建議或置業/租賃需求..." required></textarea>
      </div>
      <button type="submit" class="btn-submit">&#128231; 送出訊息</button>
      <div id="form-success">&#10003; 訊息已送出！我們將盡快回覆您。</div>
    </form>
  </div>
</div>

<!-- Footer -->
<footer>
  <p>
    &copy; 2026 LandlordSG &nbsp;|&nbsp;
    新加坡房產即時資料平台 &nbsp;|&nbsp;
    <a href="mailto:Law@weishun.cc">Law@weishun.cc</a> &nbsp;|&nbsp;
    資料來源：HDB / URA 官方公開資料
  </p>
</footer>'''
        html = html[:start_idx] + new_bottom + html[end_idx:]

    # Add the JavaScript for the form submission right before </body>
    form_js = '''
  <script>
    document.getElementById('contact-form')?.addEventListener('submit', function(e) {
      e.preventDefault();
      var name  = document.getElementById('form-name').value.trim();
      var email = document.getElementById('form-email').value.trim();
      var phone = document.getElementById('form-phone').value.trim();
      var msg   = document.getElementById('form-msg').value.trim();
      if (!name || !email || !msg) return;

      const GOOGLE_FORM_URL = 'https://docs.google.com/forms/d/e/YOUR_FORM_ID/formResponse';
      const formData = new FormData();
      formData.append('entry.111111111', name);
      formData.append('entry.222222222', email);
      formData.append('entry.333333333', phone);
      formData.append('entry.444444444', msg);

      fetch(GOOGLE_FORM_URL, {
        method: 'POST',
        mode: 'no-cors',
        body: formData
      }).then(() => {
        console.log('Google Form submitted implicitly.');
      }).catch(err => {
        console.error('Form submission error:', err);
      });

      document.getElementById('contact-form').style.opacity = '0.3';
      document.getElementById('form-success').style.display = 'block';
      setTimeout(function() {
        document.getElementById('contact-form').reset();
        document.getElementById('contact-form').style.opacity = '1';
        document.getElementById('form-success').style.display = 'none';
      }, 4000);
    });
  </script>
'''
    body_idx = html.rfind('</body>')
    if body_idx != -1 and 'contact-form' not in html[body_idx-1000:]: # Only add if not already there
        html = html[:body_idx] + form_js + html[body_idx:]

    with open('docs/index.html', 'w', encoding='utf-8') as f:
        f.write(html)
        
    print("Modified docs/index.html successfully.")

if __name__ == '__main__':
    modify_file()
