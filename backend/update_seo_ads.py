import os
import re

ROOT_DIR = r"c:\Users\Administrator\Desktop\docker_mc\developers.github.io"

LANDLORD_HTML = os.path.join(ROOT_DIR, "LandlordSG.html")
HEATMAP_HTML = os.path.join(ROOT_DIR, "heatmap.html")

GOOGLE_TAG_SNIPPET = """  <!-- Google tag：GA4 + Google Ads -->
  <script async src="https://www.googletagmanager.com/gtag/js?id=G-ME9C947K1T"></script>
  <script>
    window.dataLayer = window.dataLayer || [];
    function gtag(){dataLayer.push(arguments);}
    gtag('js', new Date());
    gtag('config', 'G-ME9C947K1T');
    gtag('config', 'AW-18370524394');

    window.MELODY_TRACKING = {
      ga4MeasurementId: 'G-ME9C947K1T',
      googleAdsId: 'AW-18370524394',
      googleAdsConversionSendTo: 'AW-18370524394/Ug0RCJzO7NscEOrp37dE'
    };
  </script>

  <!-- Google Ads 聯絡人轉換事件 -->
  <script>
    function gtag_report_conversion(url) {
      var callback = function () {
        if (typeof(url) != 'undefined') {
          window.location = url;
        }
      };
      gtag('event', 'conversion', {
        'send_to': 'AW-18370524394/Ug0RCJzO7NscEOrp37dE',
        'value': 1.0,
        'currency': 'SGD',
        'event_callback': callback
      });
      return false;
    }
  </script>"""

def update_landlord_sg():
    with open(LANDLORD_HTML, 'r', encoding='utf-8') as f:
        content = f.read()

    keywords_match = re.search(r'<meta name="keywords" content="(.*?)">', content, re.DOTALL)
    keywords = keywords_match.group(1) if keywords_match else "新加坡房產, 新加坡房地產, 新加坡私宅, 新加坡商辦"

    head_start = content.find("<head>")
    head_end = content.find("</head>")
    if head_start == -1 or head_end == -1:
        print("Could not find <head> in LandlordSG.html")
        return

    style_match = re.search(r'(<style>.*?</style>)', content[head_start:head_end], re.DOTALL)
    style_block = style_match.group(1) if style_match else ""

    new_head = f"""<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>LandlordSG 新加坡房產即時行情資料庫｜私宅Condo、商辦店面、開發商排行與房價走勢</title>

  <!-- Canonical & Robots -->
  <link rel="canonical" href="https://developers.marketing/LandlordSG.html">
  <meta name="robots" content="index, follow, max-snippet:-1, max-image-preview:large, max-video-preview:-1">
  <meta name="author" content="LandlordSG">
  <meta name="description" content="LandlordSG 整合新加坡市區重建局 (URA) 官方最新私宅、商辦與店面交易資料，提供全島房產熱力圖、開發商排行榜、歷史成交呎價 (PSF) 與未來漲幅預估分析。">
  <meta name="keywords" content="{keywords}">

  <!-- PWA & Mobile Meta -->
  <link rel="manifest" href="manifest.json">
  <meta name="theme-color" content="#0a0e1a">
  <meta name="mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
  <meta name="apple-mobile-web-app-title" content="LandlordSG">
  <link rel="apple-touch-icon" href="icons/Icon-192.png">
  <link rel="icon" type="image/png" href="favicon.png">
  <link rel="shortcut icon" href="favicon.ico">

  <!-- Open Graph / Social Preview -->
  <meta property="og:locale" content="zh_TW">
  <meta property="og:type" content="website">
  <meta property="og:site_name" content="LandlordSG 新加坡房產資料平台">
  <meta property="og:title" content="LandlordSG 新加坡房產即時行情資料庫｜私宅Condo、商辦店面、開發商排行">
  <meta property="og:description" content="整合新加坡市區重建局 (URA) 官方最新私宅、商辦與店面交易資料，提供全島房產熱力圖、開發商排行榜與歷史行情走勢分析。">
  <meta property="og:url" content="https://developers.marketing/LandlordSG.html">
  <meta property="og:image" content="https://developers.marketing/assets/seo_chart.jpg">
  <meta property="og:image:width" content="1200">
  <meta property="og:image:height" content="630">
  <meta property="og:image:alt" content="LandlordSG 新加坡房產行情資料庫與熱力圖">

  <!-- Twitter Card -->
  <meta name="twitter:card" content="summary_large_image">
  <meta name="twitter:site" content="@LandlordSG">
  <meta name="twitter:title" content="LandlordSG 新加坡房產即時行情資料庫">
  <meta name="twitter:description" content="整合 URA 最新私宅、商辦與店面交易資料，全島房產熱力圖與開發商行情分析。">
  <meta name="twitter:image" content="https://developers.marketing/assets/seo_chart.jpg">

  <!-- Geo Meta -->
  <meta name="geo.region" content="SG">
  <meta name="geo.placename" content="Singapore">
  <meta name="geo.position" content="1.3521;103.8198">
  <meta name="ICBM" content="1.3521, 103.8198">

  <!-- Chart.js & Fonts -->
  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&display=swap" rel="stylesheet">
  <link href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.css" rel="stylesheet">

{style_block}

  <!-- Structured Data / JSON-LD for Google Rich Results -->
  <script type="application/ld+json">
  {{
    "@context": "https://schema.org",
    "@graph": [
      {{
        "@type": "WebApplication",
        "@id": "https://developers.marketing/LandlordSG.html#webapp",
        "name": "LandlordSG 新加坡房產即時行情與熱力圖資料庫",
        "url": "https://developers.marketing/LandlordSG.html",
        "applicationCategory": "RealEstateApplication",
        "operatingSystem": "All",
        "description": "整合新加坡 URA 官方最新私宅、商辦與店面交易資料，提供全島房產熱力圖、開發商排行榜與歷史行情漲幅預估分析。",
        "offers": {{
          "@type": "Offer",
          "price": "0",
          "priceCurrency": "SGD"
        }}
      }},
      {{
        "@type": "Dataset",
        "@id": "https://developers.marketing/LandlordSG.html#dataset",
        "name": "Singapore Real Estate Transactions & Developers Database",
        "description": "Singapore private residential (URA) and commercial property transaction records, price per square foot (PSF), CAGR growth rate, and developer analytics.",
        "creator": {{
          "@type": "Organization",
          "name": "LandlordSG",
          "url": "https://developers.marketing/"
        }},
        "spatialCoverage": {{
          "@type": "Place",
          "name": "Singapore",
          "geo": {{
            "@type": "GeoCoordinates",
            "latitude": 1.3521,
            "longitude": 103.8198
          }}
        }}
      }},
      {{
        "@type": "ItemList",
        "@id": "https://developers.marketing/LandlordSG.html#navigation",
        "name": "LandlordSG 快速功能導覽",
        "itemListElement": [
          {{
            "@type": "SiteNavigationElement",
            "position": 1,
            "name": "全島房產熱力圖",
            "url": "https://developers.marketing/heatmap.html"
          }},
          {{
            "@type": "SiteNavigationElement",
            "position": 2,
            "name": "私宅樓盤行情資料庫",
            "url": "https://developers.marketing/LandlordSG.html#tab-ura"
          }},
          {{
            "@type": "SiteNavigationElement",
            "position": 3,
            "name": "商辦 / 店面行情資料庫",
            "url": "https://developers.marketing/LandlordSG.html#tab-commercial"
          }},
          {{
            "@type": "SiteNavigationElement",
            "position": 4,
            "name": "各大開發商推案排行榜",
            "url": "https://developers.marketing/LandlordSG.html#tab-dev"
          }},
          {{
            "@type": "SiteNavigationElement",
            "position": 5,
            "name": "Thomson Reserve 專案分析",
            "url": "https://developers.marketing/thomson-reserve/"
          }},
          {{
            "@type": "SiteNavigationElement",
            "position": 6,
            "name": "聯絡諮詢與建議",
            "url": "https://developers.marketing/LandlordSG.html#contact"
          }}
        ]
      }},
      {{
        "@type": "FAQPage",
        "@id": "https://developers.marketing/LandlordSG.html#faq",
        "mainEntity": [
          {{
            "@type": "Question",
            "name": "如何查詢新加坡私宅 (Condo) 與商辦的最新成交行情？",
            "acceptedAnswer": {{
              "@type": "Answer",
              "text": "LandlordSG 資料庫整合新加坡市區重建局 (URA) 官方即時成交紀錄，涵蓋全島超過 2,900+ 私宅建案與 340+ 商辦大樓，支援按建案名稱、街道、呎價 (PSF) 及總價查詢。"
            }}
          }},
          {{
            "@type": "Question",
            "name": "新加坡推案量最多、歷史漲幅最佳的開發商是哪幾家？",
            "acceptedAnswer": {{
              "@type": "Answer",
              "text": "在 LandlordSG 開發商排行榜中，可即時查看如 City Developments Limited (CDL)、Far East Organization、CapitaLand、GuocoLand 等頂尖建商的旗下樓盤總數、最新均價與年均複合增長率 (CAGR)。"
            }}
          }},
          {{
            "@type": "Question",
            "name": "如何利用熱力圖視覺化觀察新加坡房產價格分布？",
            "acceptedAnswer": {{
              "@type": "Answer",
              "text": "點擊 LandlordSG 的全島熱力圖功能，可依據平均成交價、每平方呎價格 (PSF) 或交易量即時切換私宅與商辦/店面的全島地理熱度圖層。"
            }}
          }},
          {{
            "@type": "Question",
            "name": "如何獲取新加坡新推建案（如 Thomson Reserve）的一對一置業諮詢？",
            "acceptedAnswer": {{
              "@type": "Answer",
              "text": "您可前往 Thomson Reserve 專案專頁查看戶型資料與土地成本分析，並填寫表單或透過 WhatsApp 與 Melody 專業團隊預約一對一賞屋與諮詢。"
            }}
          }}
        ]
      }}
    ]
  }}
  </script>

{GOOGLE_TAG_SNIPPET}
"""

    content = content[:head_start] + new_head + content[head_end:]

    # 2. Add Google Ads conversion tracking to contact form submission and mailto links
    form_submit_code = "document.getElementById('contact-form')?.addEventListener('submit', function(e) {"
    if form_submit_code in content and "gtag_report_conversion" not in content[content.find(form_submit_code):content.find(form_submit_code)+600]:
        content = content.replace(
            "document.getElementById('contact-form')?.addEventListener('submit', function(e) {\n      e.preventDefault();",
            "document.getElementById('contact-form')?.addEventListener('submit', function(e) {\n      e.preventDefault();\n      if (typeof gtag_report_conversion === 'function') { gtag_report_conversion(); }"
        )

    content = content.replace(
        '<a href="mailto:Law@weishun.cc">Law@weishun.cc</a>',
        '<a href="mailto:Law@weishun.cc" onclick="return gtag_report_conversion(this.href);">Law@weishun.cc</a>'
    )

    with open(LANDLORD_HTML, 'w', encoding='utf-8') as f:
        f.write(content)
    print("Updated LandlordSG.html successfully.")

def update_heatmap():
    with open(HEATMAP_HTML, 'r', encoding='utf-8') as f:
        content = f.read()

    keywords_match = re.search(r'<meta name="keywords" content="(.*?)">', content, re.DOTALL)
    keywords = keywords_match.group(1) if keywords_match else "新加坡房產, 新加坡房地產, 新加坡私宅, 新加坡商辦"

    head_start = content.find("<head>")
    head_end = content.find("</head>")
    if head_start == -1 or head_end == -1:
        print("Could not find <head> in heatmap.html")
        return

    style_match = re.search(r'(<style>.*?</style>)', content[head_start:head_end], re.DOTALL)
    style_block = style_match.group(1) if style_match else ""

    new_head = f"""<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>LandlordSG 新加坡房產全島熱力圖｜私宅與商辦/店面即時成交行情與呎價分布地圖</title>

  <!-- Canonical & Robots -->
  <link rel="canonical" href="https://developers.marketing/heatmap.html">
  <meta name="robots" content="index, follow, max-snippet:-1, max-image-preview:large, max-video-preview:-1">
  <meta name="author" content="LandlordSG">
  <meta name="description" content="LandlordSG 新加坡房產全島熱力圖：即時視覺化 URA 私宅公寓與商辦、店面之成交價格、每平方呎價格 (PSF) 與交易量分布，一眼掌握新加坡全島房市行情脈動。">
  <meta name="keywords" content="{keywords}">

  <!-- PWA & Mobile Meta -->
  <link rel="manifest" href="manifest.json">
  <meta name="theme-color" content="#0a0e1a">
  <meta name="mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
  <meta name="apple-mobile-web-app-title" content="LandlordSG">
  <link rel="apple-touch-icon" href="icons/Icon-192.png">
  <link rel="icon" type="image/png" href="favicon.png">
  <link rel="shortcut icon" href="favicon.ico">

  <!-- Open Graph / Social Preview -->
  <meta property="og:locale" content="zh_TW">
  <meta property="og:type" content="website">
  <meta property="og:site_name" content="LandlordSG 新加坡房產資料平台">
  <meta property="og:title" content="LandlordSG 新加坡房產全島熱力圖 | 私宅與商辦/店面成交地圖">
  <meta property="og:description" content="即時視覺化 URA 私宅與商辦/店面的成交價格、每平方呎價格 (PSF) 與交易量分布，掌握新加坡全島房產趨勢。">
  <meta property="og:url" content="https://developers.marketing/heatmap.html">
  <meta property="og:image" content="https://developers.marketing/assets/img/heat.png">
  <meta property="og:image:width" content="1200">
  <meta property="og:image:height" content="630">
  <meta property="og:image:alt" content="LandlordSG 新加坡房產全島熱力圖">

  <!-- Twitter Card -->
  <meta name="twitter:card" content="summary_large_image">
  <meta name="twitter:site" content="@LandlordSG">
  <meta name="twitter:title" content="LandlordSG 新加坡房產全島熱力圖">
  <meta name="twitter:description" content="全島私宅與商辦熱力圖行情分析，掌握新加坡房市與宏觀指標趨勢。">
  <meta name="twitter:image" content="https://developers.marketing/assets/img/heat.png">

  <!-- Geo Meta -->
  <meta name="geo.region" content="SG">
  <meta name="geo.placename" content="Singapore">
  <meta name="geo.position" content="1.3521;103.8198">
  <meta name="ICBM" content="1.3521, 103.8198">

  <!-- Leaflet.js -->
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <!-- leaflet-heat 热力图插件 -->
  <script src="https://unpkg.com/leaflet.heat@0.2.0/dist/leaflet-heat.js"></script>

  <!-- Google Fonts -->
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet" />

{style_block}

  <!-- Structured Data / JSON-LD for Google Rich Results -->
  <script type="application/ld+json">
  {{
    "@context": "https://schema.org",
    "@graph": [
      {{
        "@type": "WebApplication",
        "@id": "https://developers.marketing/heatmap.html#webapp",
        "name": "LandlordSG 新加坡房產全島熱力圖",
        "url": "https://developers.marketing/heatmap.html",
        "applicationCategory": "RealEstateApplication",
        "operatingSystem": "All",
        "description": "即時視覺化新加坡 URA 私宅與商辦/店面的成交價格、每平方呎價格 (PSF) 與交易量分布熱力圖。",
        "creator": {{
          "@type": "Organization",
          "name": "LandlordSG",
          "url": "https://developers.marketing/"
        }}
      }},
      {{
        "@type": "Dataset",
        "@id": "https://developers.marketing/heatmap.html#dataset",
        "name": "Singapore Real Estate Geolocation & Heatmap Database",
        "description": "Singapore property transaction heatmap dataset covering private condos, commercial buildings, shophouses, coordinates, PSF, and price metrics.",
        "spatialCoverage": {{
          "@type": "Place",
          "name": "Singapore",
          "geo": {{
            "@type": "GeoCoordinates",
            "latitude": 1.3521,
            "longitude": 103.8198
          }}
        }}
      }},
      {{
        "@type": "BreadcrumbList",
        "@id": "https://developers.marketing/heatmap.html#breadcrumb",
        "itemListElement": [
          {{
            "@type": "ListItem",
            "position": 1,
            "name": "首頁資料庫",
            "item": "https://developers.marketing/LandlordSG.html"
          }},
          {{
            "@type": "ListItem",
            "position": 2,
            "name": "全島房產熱力圖",
            "item": "https://developers.marketing/heatmap.html"
          }}
        ]
      }}
    ]
  }}
  </script>

{GOOGLE_TAG_SNIPPET}
"""

    content = content[:head_start] + new_head + content[head_end:]

    with open(HEATMAP_HTML, 'w', encoding='utf-8') as f:
        f.write(content)
    print("Updated heatmap.html successfully.")

if __name__ == "__main__":
    update_landlord_sg()
    update_heatmap()
