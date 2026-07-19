import os
import subprocess
import sys

def install(package):
    subprocess.check_call([sys.executable, "-m", "pip", "install", package])

try:
    import matplotlib.pyplot as plt
except ImportError:
    install('matplotlib')
    import matplotlib.pyplot as plt

import numpy as np

if not os.path.exists('docs/assets'):
    os.makedirs('docs/assets')

plt.style.use('dark_background')
fig, ax = plt.subplots(figsize=(12, 6.3))

years = np.arange(2015, 2026)
ccr = [100, 102, 104, 110, 115, 118, 125, 135, 142, 148, 155]
rcr = [100, 105, 112, 120, 128, 135, 148, 165, 175, 185, 195]
ocr = [100, 108, 116, 128, 140, 155, 175, 195, 210, 225, 240]
fd =  [100, 101, 102, 103, 104, 105, 106, 108, 111, 114, 117]
cpi = [100, 101, 102, 103, 104, 104, 106, 110, 115, 118, 121]

ax.plot(years, ccr, color='#a0aec0', linewidth=3, label='CCR')
ax.plot(years, rcr, color='#f0c84a', linewidth=3, label='RCR')
ax.plot(years, ocr, color='#9f7aea', linewidth=3, label='OCR')
ax.plot(years, fd,  color='#63b3ed', linewidth=3, linestyle='--', label='Fixed Deposit')
ax.plot(years, cpi, color='#fc8181', linewidth=3, linestyle='--', label='Inflation (CPI)')

ax.set_facecolor('#0a0e1a')
fig.patch.set_facecolor('#0a0e1a')

ax.grid(color='#2d3748', linestyle='-', linewidth=0.5, alpha=0.5)
ax.spines['top'].set_visible(False)
ax.spines['right'].set_visible(False)
ax.spines['bottom'].set_color('#4a5568')
ax.spines['left'].set_color('#4a5568')
ax.tick_params(colors='#a0aec0')

plt.title('Singapore Property Market Trends (2015-2025)', color='#ffffff', fontsize=20, pad=20, fontweight='bold')
plt.legend(facecolor='#1a202c', edgecolor='#2d3748', labelcolor='#ffffff', fontsize=12, loc='upper left')

plt.tight_layout()
plt.savefig('docs/assets/seo_chart.jpg', dpi=120, bbox_inches='tight')
print('Chart generated at docs/assets/seo_chart.jpg')
