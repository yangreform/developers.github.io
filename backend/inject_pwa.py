import os

files = ['docs/index.html', 'docs/about.html', 'docs/heatmap.html']

head_injection = """
  <!-- PWA & Mobile Meta -->
  <link rel="manifest" href="manifest.json">
  <meta name="theme-color" content="#0a0e1a">
  <meta name="mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
  <meta name="apple-mobile-web-app-title" content="LandlordSG">
  <link rel="apple-touch-icon" href="icons/Icon-192.png">

  <!-- SEO & Open Graph (Social Preview) -->
  <meta name="description" content="全島私宅熱力圖與行情分析，掌握新加坡房市與宏觀指標趨勢。提供 HDB 與 URA 最新成交資料。">
  <meta property="og:title" content="LandlordSG - 新加坡房產即時資料庫">
  <meta property="og:description" content="全島私宅熱力圖與行情分析，掌握新加坡房市與宏觀指標趨勢。">
  <meta property="og:image" content="https://yangreform.github.io/developers.github.io/assets/seo_chart.jpg">
  <meta property="og:type" content="website">
  <meta name="twitter:card" content="summary_large_image">
  <meta name="twitter:title" content="LandlordSG - 新加坡房產即時資料庫">
  <meta name="twitter:description" content="全島私宅熱力圖與行情分析，掌握新加坡房市與宏觀指標趨勢。">
  <meta name="twitter:image" content="https://yangreform.github.io/developers.github.io/assets/seo_chart.jpg">
"""

body_injection = """
  <script>
    // 註冊 PWA Service Worker
    if ('serviceWorker' in navigator) {
      window.addEventListener('load', () => {
        navigator.serviceWorker.register('sw.js')
          .then(reg => console.log('ServiceWorker 註冊成功，範圍:', reg.scope))
          .catch(err => console.log('ServiceWorker 註冊失敗:', err));
      });
    }
  </script>
"""

for filepath in files:
    if not os.path.exists(filepath):
        continue
        
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()
        
    # Check if already injected
    if 'apple-mobile-web-app-capable' in content:
        print(f"Skipping {filepath}, already has tags.")
        continue
        
    # Inject into head
    # find </title> and insert after
    title_end = content.find('</title>')
    if title_end != -1:
        content = content[:title_end + 8] + "\n" + head_injection + content[title_end + 8:]
    else:
        head_end = content.find('</head>')
        if head_end != -1:
            content = content[:head_end] + head_injection + "\n" + content[head_end:]
            
    # Inject into body
    body_end = content.find('</body>')
    if body_end != -1:
        content = content[:body_end] + body_injection + "\n" + content[body_end:]
        
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(content)
        
    print(f"Updated {filepath} successfully.")
