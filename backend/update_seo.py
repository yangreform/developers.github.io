import json
import re

with open('backend/seo_keywords.json', 'r', encoding='utf-8') as f:
    seo_data = json.load(f)

developers = seo_data['developers']
ura_projects = seo_data['ura_projects']
commercial_projects = seo_data['commercial_projects']

core_keywords = [
    "新加坡房產", "新加坡房地產", "新加坡私宅", "新加坡公寓", "新加坡商辦", "新加坡店面", "新加坡寫字樓",
    "新加坡買房", "新加坡租屋", "新加坡物業投資", "新加坡房價走勢", "新加坡房產熱力圖", "URA 交易紀錄", 
    "Singapore Real Estate", "Singapore Condo", "Singapore Commercial Property", "Singapore Shophouse",
    "Singapore Property Heatmap", "URA Property Transactions", "Singapore Property Developers",
    "CCR", "RCR", "OCR", "Orchard", "Marina Bay", "CBD", "Bugis", "Tampines", "Jurong", "Woodlands"
]

top_devs = [
    "City Developments Limited", "Far East Organization", "CapitaLand Residential", "Keppel Land Limited",
    "GuocoLand", "Frasers Property", "MCL Land", "SingHaiyi Group", "UOL Group", "Bukit Sembawang Estates",
    "Wing Tai Holdings", "Hoi Hup Realty", "Sim Lian Group", "Qingjian Realty", "Allgreen Properties",
    "Oxley Holdings", "Kingsford Development", "CEL Development", "Tuan Sing Holdings", "Hong Leong Holdings"
]

top_commercial = [
    "International Plaza", "Suntec City", "Shenton House", "Peninsula Plaza", "The Adelphi",
    "Tong Eng Building", "Samsung Hub", "Prudential Tower", "The Central", "SBF Center",
    "GB Building", "High Street Centre", "Textile Centre", "Crown at Robinson", "Oxley Tower",
    "Robinson Square", "Plus Building", "PS100", "Eon Shenton", "Vision Exchange"
]

meta_keywords_list = core_keywords + top_devs + top_commercial + ura_projects[:40]
meta_keywords_str = ", ".join(meta_keywords_list)

all_devs_str = ", ".join(developers)
all_ura_str = ", ".join(ura_projects)
all_com_str = ", ".join(commercial_projects)

seo_hidden_block = f"""
<!-- SEO Search Engine Keywords Index: All Singapore Developers, Condos & Commercial Properties -->
<div style="display:none;" aria-hidden="true" class="seo-keywords-index">
  <h2>新加坡所有房地產開發商名錄 (Singapore Real Estate Developers Directory)</h2>
  <p>{all_devs_str}</p>
  <h2>新加坡所有私宅與公寓樓盤名錄 (Singapore Private Condominiums Directory)</h2>
  <p>{all_ura_str}</p>
  <h2>新加坡所有商辦、寫字樓與店面名錄 (Singapore Commercial Buildings & Offices Directory)</h2>
  <p>{all_com_str}</p>
</div>
"""

heatmap_json_ld = """
  <!-- Structured Data / JSON-LD for Google Rich Results -->
  <script type="application/ld+json">
  {
    "@context": "https://schema.org",
    "@type": "WebApplication",
    "name": "LandlordSG 新加坡房產全島熱力圖",
    "url": "https://yangreform.github.io/developers.github.io/heatmap.html",
    "applicationCategory": "RealEstateApplication",
    "operatingSystem": "All",
    "description": "即時視覺化新加坡 URA 私宅與商辦/店面的成交價格、每平方呎價格 (PSF) 與交易量分布熱力圖。",
    "creator": {
      "@type": "Organization",
      "name": "LandlordSG"
    }
  }
  </script>
"""

# Update heatmap.html
with open('docs/heatmap.html', 'r', encoding='utf-8') as f:
    heatmap_html = f.read()

# Add meta keywords if not present
if '<meta name="keywords"' not in heatmap_html:
    kw_tag = f'  <meta name="keywords" content="{meta_keywords_str}">\n'
    heatmap_html = heatmap_html.replace('<!-- SEO & Open Graph (Social Preview) -->', f'<!-- SEO & Open Graph (Social Preview) -->\n{kw_tag}')
else:
    heatmap_html = re.sub(r'<meta name="keywords" content="[^"]*">', f'<meta name="keywords" content="{meta_keywords_str}">', heatmap_html)

if '<link rel="canonical"' not in heatmap_html:
    canonical_meta = """  <link rel="canonical" href="https://yangreform.github.io/developers.github.io/heatmap.html">
  <meta name="robots" content="index, follow, max-snippet:-1, max-image-preview:large, max-video-preview:-1">
  <meta name="author" content="LandlordSG">
  <meta name="geo.region" content="SG">
  <meta name="geo.placename" content="Singapore">
  <meta name="geo.position" content="1.3521;103.8198">
  <meta name="ICBM" content="1.3521, 103.8198">
""" + heatmap_json_ld
    heatmap_html = heatmap_html.replace('</head>', canonical_meta + '\n</head>')

if '<!-- SEO Search Engine Keywords Index' not in heatmap_html:
    heatmap_html = heatmap_html.replace('</body>', seo_hidden_block + '\n</body>')

with open('docs/heatmap.html', 'w', encoding='utf-8') as f:
    f.write(heatmap_html)

print("Updated docs/heatmap.html successfully!")
