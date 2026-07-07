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
