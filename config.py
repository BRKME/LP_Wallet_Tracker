"""
LP Wallet Tracker Configuration
"""

# Wallet addresses
WALLETS = {
    "Марта": "0x10082016a94920aBdf410CDB6f98c2Ead2c57340",
    "Аркаша": "0x305220d077474c5cab839E7C1cB3264Aca19f1B9"
}

# Whitelist of approved assets (symbols in lowercase for matching)
WHITELIST = {
    "Layer 1": ["btc", "eth", "bnb", "wbtc", "weth"],
    "Trading Infrastructure": ["hype", "aster", "pump"],  # Hyperliquid, Aster, Pump.fun
    "DeFi Credit": ["morpho"],
    "AI Narrative": ["tao"],
    "Privacy Hedge": ["zec"],  # Zcash
    # Stablecoins are always allowed
    "Stablecoins": ["usdt", "usdc", "dai", "busd", "usd+", "usde"]
}

# Flatten whitelist for easy checking
WHITELIST_FLAT = set()
for category, tokens in WHITELIST.items():
    WHITELIST_FLAT.update(tokens)

# Monthly plan (cumulative USD target) - update as needed
# Format: "YYYY-MM": target_usd
MONTHLY_PLAN = {
    "2025-01": 5000,
    "2025-02": 6000,
    "2025-03": 7000,
    "2025-04": 8000,
    "2025-05": 9000,
    "2025-06": 10000,
    "2025-07": 11000,
    "2025-08": 12000,
    "2025-09": 13000,
    "2025-10": 14000,
    "2025-11": 15000,
    "2025-12": 16000,
    "2026-01": 17000,
    "2026-02": 18000,
    "2026-03": 19000,
    "2026-04": 20000,
    "2026-05": 21000,
    "2026-06": 22000,
    "2026-07": 23000,
    "2026-08": 24000,
    "2026-09": 25000,
    "2026-10": 26000,
    "2026-11": 27000,
    "2026-12": 28000,
}

def get_current_plan(year_month: str) -> int:
    """Get plan for current month, or latest available"""
    if year_month in MONTHLY_PLAN:
        return MONTHLY_PLAN[year_month]
    # Return last known plan if month not configured
    sorted_months = sorted(MONTHLY_PLAN.keys())
    for month in reversed(sorted_months):
        if month <= year_month:
            return MONTHLY_PLAN[month]
    return 0

def is_whitelisted(symbol: str) -> bool:
    """Check if token symbol is in whitelist"""
    return symbol.lower() in WHITELIST_FLAT

def get_whitelist_category(symbol: str) -> str:
    """Get category for whitelisted token"""
    symbol_lower = symbol.lower()
    for category, tokens in WHITELIST.items():
        if symbol_lower in tokens:
            return category
    return None
