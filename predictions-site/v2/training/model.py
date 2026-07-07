"""
Single place defining what kind of model VORTEX V2 trains, so train.py and
backtest.py can't drift onto different hyperparameters.

HistGradientBoostingClassifier: ships in scikit-learn itself (no xgboost/
lightgbm native-binary dependency to worry about in a Vercel function
bundle later), trains fast on a few thousand rows, and gives well-calibrated
probability output out of the box.
"""
from sklearn.ensemble import HistGradientBoostingClassifier


def make_model() -> HistGradientBoostingClassifier:
    return HistGradientBoostingClassifier(max_depth=4, learning_rate=0.05, random_state=42)
