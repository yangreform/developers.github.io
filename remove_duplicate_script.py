with open('docs/index.html', 'r', encoding='utf-8') as f:
    lines = f.readlines()

# Lines to keep: 0 to 728 (inclusive, which is up to line 729)
# Lines to skip: 729 to 812 (inclusive, which is the <script> to </script>)
# Lines to keep: 813 onwards
new_lines = lines[:729] + lines[813:]

with open('docs/index.html', 'w', encoding='utf-8') as f:
    f.writelines(new_lines)
    
print("Successfully removed duplicate static script.")
