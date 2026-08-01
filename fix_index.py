import re

def fix_index():
    with open('docs/index.html', 'r', encoding='utf-8') as f:
        content = f.read()

    # 1. Fix the chart destruction
    content = re.sub(r"let existingChart = Chart\.getChart\('macroChart'\);\s*if \(existingChart\) \{\s*existingChart\.destroy\(\);\s*\}",
                     "if (macroChartInstance) {\n  macroChartInstance.destroy();\n}", content)

    # 2. Fix the tr onclick
    lines = content.split('\\n')
    for i in range(len(lines)):
        if '<tr data-row="1"' in lines[i] and 'encodeURIComponent(d.project)' in lines[i]:
            # Replace the onclick part
            lines[i] = re.sub(
                r"onclick=\"window\.location\.href=\\\'heatmap\.html\?project=\\\' \+ encodeURIComponent\(d\.project\)\"",
                r'onclick=\"window.location.href=\'heatmap.html?project=" + encodeURIComponent(d.project || d.label || d.town || "") + "\'\"',
                lines[i]
            )

    content = '\\n'.join(lines)
    
    with open('docs/index.html', 'w', encoding='utf-8') as f:
        f.write(content)
        
    print("Fixed docs/index.html")

if __name__ == '__main__':
    fix_index()
