import re

def refactor():
    with open('backend/update_board.py', 'r', encoding='utf-8') as f:
        code = f.read()

    # 1. Add SESSION override
    if 'SESSION = requests.Session()' not in code:
        code = code.replace('import requests', '''import requests

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Origin": "https://www.mlb.com",
    "Referer": "https://www.mlb.com/"
})''')
    
    code = code.replace('requests.get(', 'SESSION.get(')

    # 2. Add MIN_LINE logic inside _add_side
    min_line_logic = """
                # Filter out line noise below minimum operational thresholds
                prop_type = MARKET_TO_PROP_TYPE.get(market) or NBA_MARKET_TO_PROP_TYPE.get(market)
                if prop_type in MIN_LINE:
                    min_allowed = MIN_LINE[prop_type].get(side_key)
                    if min_allowed is not None and line < min_allowed:
                        return
"""
    if "min_allowed = MIN_LINE[prop_type].get(side_key)" not in code:
        code = code.replace(
            '            def _add_side(side_key: str, price_map: dict, true_prob: float):\n                if len(price_map) < MIN_BOOKS:',
            '            def _add_side(side_key: str, price_map: dict, true_prob: float):\n' + min_line_logic + '                if len(price_map) < MIN_BOOKS:'
        )

    # 3. Change DB table active_board to props_board
    code = code.replace('active_board', 'props_board')

    with open('backend/update_board.py', 'w', encoding='utf-8') as f:
        f.write(code)
    
    print("Refactor complete.")

if __name__ == '__main__':
    refactor()
