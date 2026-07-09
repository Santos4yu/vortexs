"""
Small, dependency-free odds math shared by VORTEX V2. Deliberately NOT
imported from backend/moneyline.py (the repo-root backend/ directory isn't
part of the predictions-site Vercel deployment -- only files inside
predictions-site/ get bundled -- so reaching outside it crashes on Vercel
at import time). These two functions are pure and tiny enough to just live
here instead of introducing another cross-deployment dependency.
"""


def american_to_prob(odds: int) -> float:
    """American odds -> implied probability (includes the bookmaker's vig)."""
    if odds < 0:
        return -odds / (-odds + 100)
    return 100 / (odds + 100)


def devig_two_way(p_over: float, p_under: float) -> tuple[float, float]:
    """Remove the bookmaker's hold from a two-way market -> fair probabilities."""
    total = p_over + p_under
    if total <= 0:
        return 0.5, 0.5
    return p_over / total, p_under / total


def sharp_no_vig_prob(over_map: dict, under_map: dict, sharp_book: str) -> float | None:
    """True-probability anchor from `sharp_book` (Pinnacle) ONLY -- mirrors
    backend/update_board.py's _sharp_no_vig_prob(). Pinnacle's own two-sided
    price, de-vigged, is the best available estimate of how often a prop
    actually hits (it moves on real sharp money, not public bias/DFS
    single-sided pricing). Returns None if Pinnacle doesn't carry both sides
    of this exact line."""
    if sharp_book in over_map and sharp_book in under_map:
        p_over, _ = devig_two_way(american_to_prob(over_map[sharp_book]), american_to_prob(under_map[sharp_book]))
        return p_over
    return None


def consensus_no_vig_prob(over_map: dict, under_map: dict) -> float | None:
    """Average de-vigged probability across every book offering BOTH sides of
    this exact line -- mirrors backend/update_board.py's
    consensus_no_vig_prob(). Weaker than a sharp anchor but still a real
    two-way de-vig, unlike a single-sided DFS price."""
    probs = []
    for book, over_odds in over_map.items():
        if book in under_map:
            p_over, _ = devig_two_way(american_to_prob(over_odds), american_to_prob(under_map[book]))
            probs.append(p_over)
    return sum(probs) / len(probs) if probs else None
