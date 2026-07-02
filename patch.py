for filename in ['backend/update_board.py', 'backend/stats_mlb.py']:
    with open(filename, 'r', encoding='utf-8') as f:
        code = f.read()
    
    code = code.replace(
        '"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"',
        '"Accept": "application/json, text/html, application/xhtml+xml, */*"'
    )
    
    with open(filename, 'w', encoding='utf-8') as f:
        f.write(code)
    print(f'Patched Accept header in {filename}')
