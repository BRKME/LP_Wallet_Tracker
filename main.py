#!/usr/bin/env python3
"""
LP Wallet Tracker - Weekly portfolio monitoring
Sends Telegram reports with plan vs actual comparison
"""

import os
import json
import logging
import asyncio
import aiohttp
from datetime import datetime, timedelta
from pathlib import Path

from config import WALLETS, WHITELIST, WHITELIST_FLAT, get_plan_for_wallet, get_total_plan, is_whitelisted, get_whitelist_category

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Environment variables
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN', '')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '')
DEBANK_API_KEY = os.getenv('DEBANK_API_KEY', '')

# Files
STATE_DIR = Path("state")
HISTORY_FILE = STATE_DIR / "history.json"


class WalletTracker:
    def __init__(self):
        self.session = None
        STATE_DIR.mkdir(exist_ok=True)
    
    async def __aenter__(self):
        self.session = aiohttp.ClientSession()
        return self
    
    async def __aexit__(self, *args):
        if self.session:
            await self.session.close()
    
    # ============ DATA FETCHING ============
    
    async def get_wallet_balance_scrape(self, address: str) -> dict:
        """Scrape wallet balance directly from DeBank website"""
        try:
            from playwright.async_api import async_playwright
            
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                page = await browser.new_page()
                
                url = f"https://debank.com/profile/{address}"
                logger.info(f"Scraping {url}")
                
                await page.goto(url, wait_until="networkidle", timeout=60000)
                
                # Wait for balance to load
                await page.wait_for_selector('[class*="HeaderInfo_totalAssetInner"]', timeout=30000)
                
                # Get total balance text
                balance_el = await page.query_selector('[class*="HeaderInfo_totalAssetInner"]')
                if balance_el:
                    balance_text = await balance_el.inner_text()
                    # Parse "$12,345.67\n+0.79%" -> 12345.67
                    # Take first line only
                    first_line = balance_text.split('\n')[0].strip()
                    # Remove $ and commas
                    first_line = first_line.replace('$', '').replace(',', '').strip()
                    total_usd = float(first_line)
                    logger.info(f"Scraped balance: ${total_usd:,.2f}")
                else:
                    total_usd = 0
                
                # Get tokens (optional, for whitelist check)
                tokens = []
                token_elements = await page.query_selector_all('[class*="TokenCell_tokenCell"]')
                for el in token_elements[:20]:  # Limit to 20
                    try:
                        symbol_el = await el.query_selector('[class*="TokenCell_tokenSymbol"]')
                        value_el = await el.query_selector('[class*="TokenCell_tokenValue"]')
                        if symbol_el and value_el:
                            symbol = await symbol_el.inner_text()
                            value_text = await value_el.inner_text()
                            # Take first line, remove $ and commas
                            value_text = value_text.split('\n')[0].replace('$', '').replace(',', '').strip()
                            tokens.append({
                                'symbol': symbol,
                                'value': float(value_text) if value_text else 0
                            })
                    except:
                        pass
                
                await browser.close()
                
                return {"total_usd": total_usd, "tokens": tokens}
                
        except ImportError:
            logger.warning("Playwright not installed, skipping scrape")
            return None
        except Exception as e:
            logger.error(f"Scraping error: {e}")
            return None
    
    async def get_wallet_balance_covalent(self, address: str) -> dict:
        """Fetch wallet balance from Covalent API (free tier)"""
        api_key = os.getenv('COVALENT_API_KEY', 'cqt_rQy7cVXgKJhbRwqPJTfFFDCGWbgP')  # Free demo key
        
        try:
            # Chains to check: ETH=1, BSC=56, Arbitrum=42161, Polygon=137, Base=8453
            chains = [1, 56, 42161, 137, 8453]
            total_usd = 0
            all_tokens = []
            
            for chain_id in chains:
                url = f"https://api.covalenthq.com/v1/{chain_id}/address/{address}/balances_v2/"
                params = {"key": api_key, "quote-currency": "USD"}
                
                async with self.session.get(url, params=params, timeout=30) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        items = data.get('data', {}).get('items', [])
                        
                        for item in items:
                            quote = item.get('quote', 0) or 0
                            if quote > 0.01:  # Skip dust
                                total_usd += quote
                                all_tokens.append({
                                    'symbol': item.get('contract_ticker_symbol', ''),
                                    'value': quote,
                                    'chain_id': chain_id
                                })
                    else:
                        logger.warning(f"Covalent chain {chain_id}: {resp.status}")
            
            logger.info(f"Covalent API total: ${total_usd:,.2f}")
            return {"total_usd": total_usd, "tokens": all_tokens}
            
        except Exception as e:
            logger.error(f"Covalent API error: {e}")
            return None
    
    async def get_wallet_balance_debank(self, address: str) -> dict:
        """Fetch wallet balance from DeBank Pro API (paid)"""
        if not DEBANK_API_KEY:
            logger.info("DEBANK_API_KEY not set, skipping Pro API")
            return None
        
        headers = {
            "AccessKey": DEBANK_API_KEY,
            "accept": "application/json"
        }
        
        try:
            # Get total balance
            url = f"https://pro-openapi.debank.com/v1/user/total_balance?id={address}"
            async with self.session.get(url, headers=headers, timeout=30) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    total_usd = data.get('total_usd_value', 0)
                else:
                    logger.error(f"DeBank Pro API error: {resp.status}")
                    return None
            
            # Get token list for whitelist check
            url = f"https://pro-openapi.debank.com/v1/user/all_token_list?id={address}"
            async with self.session.get(url, headers=headers, timeout=30) as resp:
                if resp.status == 200:
                    tokens = await resp.json()
                else:
                    tokens = []
            
            return {
                "total_usd": total_usd,
                "tokens": tokens
            }
        
        except Exception as e:
            logger.error(f"Error fetching DeBank Pro data: {e}")
            return None
    
    async def get_wallet_balance_debank_public(self, address: str) -> dict:
        """Fetch wallet balance from DeBank public API (free)"""
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "application/json",
                "Origin": "https://debank.com",
                "Referer": "https://debank.com/"
            }
            
            # Get total balance
            url = f"https://api.debank.com/user/total_balance?addr={address.lower()}"
            async with self.session.get(url, headers=headers, timeout=30) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    total_usd = data.get('data', {}).get('total_usd_value', 0)
                    if total_usd is None:
                        total_usd = 0
                    logger.info(f"DeBank public API: ${total_usd:,.2f}")
                else:
                    text = await resp.text()
                    logger.error(f"DeBank public API error: {resp.status} - {text[:200]}")
                    return None
            
            # Get token list
            tokens = []
            url = f"https://api.debank.com/user/all_token_list?addr={address.lower()}"
            async with self.session.get(url, headers=headers, timeout=30) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    tokens = data.get('data', []) or []
            
            return {
                "total_usd": total_usd,
                "tokens": tokens
            }
        
        except Exception as e:
            logger.error(f"Error fetching DeBank public data: {e}")
            return None
    
    async def get_wallet_balance_zapper(self, address: str) -> dict:
        """Fallback: Fetch from Zapper (free tier)"""
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "application/json"
            }
            
            url = f"https://api.zapper.xyz/v2/balances?addresses%5B%5D={address}"
            async with self.session.get(url, headers=headers, timeout=30) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    # Parse Zapper response
                    total_usd = sum(item.get('balanceUSD', 0) for item in data.get('balances', []))
                    return {"total_usd": total_usd, "tokens": []}
                else:
                    logger.warning(f"Zapper API error: {resp.status}")
                    return None
        except Exception as e:
            logger.warning(f"Zapper API failed: {e}")
            return None
    
    async def get_btc_balance(self, btc_address: str) -> dict:
        """Fetch BTC balance via mempool.space API (free, no auth)"""
        try:
            url = f"https://mempool.space/api/address/{btc_address}"
            async with self.session.get(url, timeout=15) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    # Balance in satoshis (funded - spent)
                    funded = data.get("chain_stats", {}).get("funded_txo_sum", 0)
                    spent = data.get("chain_stats", {}).get("spent_txo_sum", 0)
                    # Include mempool (unconfirmed)
                    mempool_funded = data.get("mempool_stats", {}).get("funded_txo_sum", 0)
                    mempool_spent = data.get("mempool_stats", {}).get("spent_txo_sum", 0)
                    
                    balance_sat = (funded - spent) + (mempool_funded - mempool_spent)
                    balance_btc = balance_sat / 1e8
                    
                    logger.info(f"BTC balance: {balance_btc:.8f} BTC")
                else:
                    logger.warning(f"mempool.space error: {resp.status}")
                    return {"total_usd": 0, "btc": 0, "tokens": []}
            
            # Get BTC price
            btc_price = await self._get_btc_price()
            total_usd = balance_btc * btc_price
            
            logger.info(f"BTC value: ${total_usd:,.2f} ({balance_btc:.8f} BTC @ ${btc_price:,.0f})")
            
            return {
                "total_usd": total_usd,
                "btc": balance_btc,
                "btc_price": btc_price,
                "tokens": [{"symbol": "BTC", "value": total_usd}]
            }
        except Exception as e:
            logger.error(f"BTC balance error: {e}")
            return {"total_usd": 0, "btc": 0, "tokens": []}
    
    async def _get_btc_price(self) -> float:
        """Get current BTC price in USD"""
        try:
            url = "https://mempool.space/api/v1/prices"
            async with self.session.get(url, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return float(data.get("USD", 0))
        except:
            pass
        
        # Fallback: CoinGecko
        try:
            url = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd"
            async with self.session.get(url, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return float(data.get("bitcoin", {}).get("usd", 0))
        except:
            pass
        
        return 0
    
    async def get_zec_balance(self, zec_address: str) -> dict:
        """Fetch ZEC balance with multiple API fallbacks"""
        balance_zec = None
        
        # Try 1: blockchair.com
        try:
            url = f"https://api.blockchair.com/zcash/dashboards/address/{zec_address}"
            async with self.session.get(url, timeout=15) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    addr_data = data.get("data", {}).get(zec_address, {})
                    address_info = addr_data.get("address", {})
                    balance_zatoshi = address_info.get("balance", 0)
                    balance_zec = balance_zatoshi / 1e8
                    logger.info(f"ZEC balance (blockchair): {balance_zec:.8f} ZEC")
                else:
                    logger.warning(f"blockchair ZEC error: {resp.status}")
        except Exception as e:
            logger.warning(f"blockchair ZEC exception: {e}")
        
        # Try 2: mainnet.zcashexplorer.app
        if balance_zec is None:
            try:
                url = f"https://mainnet.zcashexplorer.app/api/v1/address/{zec_address}/info"
                async with self.session.get(url, timeout=15) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        balance_zec = float(data.get("balance", 0))
                        logger.info(f"ZEC balance (zcashexplorer): {balance_zec:.8f} ZEC")
            except Exception as e:
                logger.warning(f"zcashexplorer exception: {e}")
        
        # Try 3: zec.rocks API
        if balance_zec is None:
            try:
                url = f"https://api.zec.rocks/v1/address/{zec_address}"
                async with self.session.get(url, timeout=15) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        balance_zec = float(data.get("balance", 0)) / 1e8
                        logger.info(f"ZEC balance (zec.rocks): {balance_zec:.8f} ZEC")
            except Exception as e:
                logger.warning(f"zec.rocks exception: {e}")
        
        if balance_zec is None or balance_zec == 0:
            logger.warning(f"ZEC balance is 0 or unavailable for {zec_address}")
            return {"total_usd": 0, "zec": 0, "tokens": []}
        
        # Get ZEC price
        zec_price = await self._get_zec_price()
        total_usd = balance_zec * zec_price
        
        logger.info(f"ZEC value: ${total_usd:,.2f} ({balance_zec:.8f} ZEC @ ${zec_price:,.2f})")
        
        return {
            "total_usd": total_usd,
            "zec": balance_zec,
            "zec_price": zec_price,
            "tokens": [{"symbol": "ZEC", "value": total_usd}]
        }
    
    async def _get_zec_price(self) -> float:
        """Get current ZEC price in USD"""
        try:
            url = "https://api.coingecko.com/api/v3/simple/price?ids=zcash&vs_currencies=usd"
            async with self.session.get(url, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return float(data.get("zcash", {}).get("usd", 0))
        except:
            pass
        return 0
    
    async def get_wallet_balance(self, address: str) -> dict:
        """Get wallet balance, trying multiple sources"""
        # Try scraping DeBank directly (most accurate for total)
        logger.info("Trying to scrape DeBank...")
        result = await self.get_wallet_balance_scrape(address)
        if result and result.get('total_usd', 0) > 0:
            # If scrape didn't get tokens, try to get them from Covalent
            if not result.get('tokens'):
                logger.info("Scrape got total but no tokens, fetching from Covalent...")
                cov_result = await self.get_wallet_balance_covalent(address)
                if cov_result and cov_result.get('tokens'):
                    result['tokens'] = cov_result['tokens']
                    logger.info(f"Added {len(result['tokens'])} tokens from Covalent")
            return result
        
        # Try Covalent API (reliable for GitHub Actions)
        logger.info("Trying Covalent API...")
        result = await self.get_wallet_balance_covalent(address)
        if result and result.get('total_usd', 0) > 0:
            return result
        
        # Try DeBank Pro API (if key set)
        result = await self.get_wallet_balance_debank(address)
        if result and result.get('total_usd', 0) > 0:
            logger.info("Using DeBank Pro API")
            return result
        
        # Try DeBank public API (free)
        logger.info("Trying DeBank public API...")
        result = await self.get_wallet_balance_debank_public(address)
        if result and result.get('total_usd', 0) >= 0:
            return result
        
        # Fallback to Zapper
        logger.info("Falling back to Zapper API")
        result = await self.get_wallet_balance_zapper(address)
        if result:
            return result
        
        logger.error(f"Could not fetch balance for {address}")
        return {"total_usd": 0, "tokens": []}
    
    # ============ WHITELIST CHECK ============
    
    def check_whitelist(self, tokens: list) -> dict:
        """Check tokens against whitelist"""
        whitelisted = []
        not_whitelisted = []
        
        for token in tokens:
            symbol = token.get('symbol', '') or token.get('optimized_symbol', '')
            value = token.get('value', 0) or token.get('amount', 0) * token.get('price', 0)
            
            if value < 1:  # Skip dust
                continue
            
            if is_whitelisted(symbol):
                category = get_whitelist_category(symbol)
                whitelisted.append({
                    'symbol': symbol,
                    'value': value,
                    'category': category
                })
            else:
                not_whitelisted.append({
                    'symbol': symbol,
                    'value': value
                })
        
        return {
            'whitelisted': whitelisted,
            'not_whitelisted': not_whitelisted
        }
    
    # ============ HISTORY MANAGEMENT ============
    
    def load_history(self) -> dict:
        """Load historical data"""
        if HISTORY_FILE.exists():
            with open(HISTORY_FILE, 'r') as f:
                data = json.load(f)
                # Ensure ATH structures exist
                if "ath" not in data:
                    data["ath"] = {"value": 0, "date": None}
                if "wallet_ath" not in data:
                    data["wallet_ath"] = {}
                return data
        return {"records": [], "monthly_snapshots": {}, "ath": {"value": 0, "date": None}, "wallet_ath": {}}
    
    def save_history(self, history: dict):
        """Save historical data"""
        with open(HISTORY_FILE, 'w') as f:
            json.dump(history, f, indent=2, ensure_ascii=False)
    
    def add_record(self, history: dict, total_usd: float, plan_usd: float, details: dict):
        """Add a new record to history"""
        today = datetime.now().strftime("%Y-%m-%d")
        
        record = {
            "date": today,
            "total_usd": total_usd,
            "plan_usd": plan_usd,
            "difference": total_usd - plan_usd,
            "wallets": details
        }
        
        history["records"].append(record)
        
        # Keep only last 52 weeks
        if len(history["records"]) > 52:
            history["records"] = history["records"][-52:]
        
        # Update total ATH if new high
        if "ath" not in history:
            history["ath"] = {"value": 0, "date": None}
        
        if total_usd > history["ath"]["value"]:
            history["ath"]["value"] = total_usd
            history["ath"]["date"] = today
            logger.info(f"🏆 New ATH (total): ${total_usd:,.0f}")
        
        # Update per-wallet ATH
        if "wallet_ath" not in history:
            history["wallet_ath"] = {}
        
        for wallet_name, wallet_data in details.items():
            wallet_value = wallet_data.get("total_usd", 0)
            
            if wallet_name not in history["wallet_ath"]:
                history["wallet_ath"][wallet_name] = {"value": 0, "date": None}
            
            if wallet_value > history["wallet_ath"][wallet_name]["value"]:
                history["wallet_ath"][wallet_name]["value"] = wallet_value
                history["wallet_ath"][wallet_name]["date"] = today
                logger.info(f"🏆 New ATH ({wallet_name}): ${wallet_value:,.0f}")
        
        return record
    
    def get_last_week_data(self, history: dict) -> dict:
        """Get data from last week"""
        if len(history["records"]) >= 2:
            return history["records"][-2]
        return None
    
    def get_month_start_data(self, history: dict) -> dict:
        """Get data from start of current month"""
        current_month = datetime.now().strftime("%Y-%m")
        
        # Check monthly snapshots first
        if current_month in history.get("monthly_snapshots", {}):
            return history["monthly_snapshots"][current_month]
        
        # Find first record of the month
        for record in history["records"]:
            if record["date"].startswith(current_month):
                return record
        
        return None
    
    def save_monthly_snapshot(self, history: dict, record: dict):
        """Save snapshot at start of month"""
        current_month = datetime.now().strftime("%Y-%m")
        if "monthly_snapshots" not in history:
            history["monthly_snapshots"] = {}
        
        if current_month not in history["monthly_snapshots"]:
            history["monthly_snapshots"][current_month] = record
            logger.info(f"Saved monthly snapshot for {current_month}")
    
    # ============ MESSAGE FORMATTING ============
    
    def format_number(self, num: float) -> str:
        """Format number with commas"""
        return f"${num:,.0f}"
    
    def format_change(self, current: float, previous: float) -> str:
        """Format change with absolute and percentage"""
        diff = current - previous
        if previous > 0:
            pct = (diff / previous) * 100
            sign = "+" if diff >= 0 else ""
            return f"{sign}${diff:,.0f} ({sign}{pct:.1f}%)"
        return f"+${diff:,.0f}"
    
    def is_first_week_of_month(self) -> bool:
        """Check if it's the first week of the month"""
        today = datetime.now()
        return today.day <= 7
    
    def build_message(self, record: dict, last_week: dict, month_start: dict, whitelist_report: dict, history: dict = None, all_tokens: list = None) -> str:
        """Build the Telegram message"""
        now = datetime.now()
        today = now.strftime("%d.%m.%Y")
        week_num = now.isocalendar()[1]
        
        # Wallet purposes
        WALLET_PURPOSES = {
            "Марта": "копим на квартиру",
            "Аркаша": "копим на учебу",
            "Мама": "копим на домик у моря",
        }
        
        msg = f"📅 {today} · Неделя {week_num}\n\n"
        msg += f"<b>Накопления детей</b>\n"
        
        # Separate plan-tracked vs balance-only wallets
        plan_wallets = {n: d for n, d in record['wallets'].items() if d.get('plan_usd', 0) > 0}
        other_wallets = {n: d for n, d in record['wallets'].items() if d.get('plan_usd', 0) == 0}
        
        # Get wallet ATH data
        wallet_ath = history.get("wallet_ath", {}) if history else {}
        
        # Helper to format wallet name with purpose
        def wallet_label(name):
            purpose = WALLET_PURPOSES.get(name)
            if purpose:
                return f"{name} <i>({purpose})</i>"
            return name
        
        # Helper to get wallet dynamics
        def get_wallet_dynamics(wallet_name, current_value):
            parts = []
            
            # Weekly
            if last_week and wallet_name in last_week.get('wallets', {}):
                prev_val = last_week['wallets'][wallet_name].get('total_usd', 0)
                if prev_val > 0:
                    parts.append(f"нед: {self.format_change(current_value, prev_val)}")
            
            # Monthly
            if month_start and wallet_name in month_start.get('wallets', {}):
                prev_val = month_start['wallets'][wallet_name].get('total_usd', 0)
                if prev_val > 0:
                    parts.append(f"мес: {self.format_change(current_value, prev_val)}")
            
            # ATH
            if wallet_name in wallet_ath:
                ath_val = wallet_ath[wallet_name].get("value", 0)
                ath_date = wallet_ath[wallet_name].get("date", "")
                if ath_val > 0:
                    if current_value >= ath_val:
                        parts.append("🏆 ATH!")
                    else:
                        from_ath_pct = ((current_value - ath_val) / ath_val * 100)
                        parts.append(f"ATH: {from_ath_pct:.0f}%")
            
            return " · ".join(parts) if parts else ""
        
        # Helper to format asset split for a wallet
        def format_assets(tokens):
            if not tokens:
                return ""
            by_symbol = {}
            for t in tokens:
                sym = (t.get("symbol", "???") or "???").upper()
                val = t.get("value", 0) or 0
                if val > 0:
                    by_symbol[sym] = by_symbol.get(sym, 0) + val
            total_val = sum(by_symbol.values())
            if total_val <= 0:
                return ""
            sorted_assets = sorted(by_symbol.items(), key=lambda x: -x[1])
            parts = []
            for sym, val in sorted_assets[:6]:
                pct = val / total_val * 100
                if pct >= 1:
                    parts.append(f"{sym} {pct:.0f}%")
            return " · ".join(parts)
        
        # Per-wallet breakdown — plan wallets first
        for name, data in plan_wallets.items():
            wallet_plan = data.get('plan_usd', 0)
            wallet_fact = data['total_usd']
            wallet_diff = wallet_fact - wallet_plan
            wallet_pct = (wallet_diff / wallet_plan * 100) if wallet_plan > 0 else 0
            wallet_sign = "+" if wallet_diff >= 0 else ""
            
            msg += f"\n<b>{wallet_label(name)}:</b>\n"
            msg += f"├ План: {self.format_number(wallet_plan)}\n"
            msg += f"├ Факт: {self.format_number(wallet_fact)}\n"
            msg += f"├ {wallet_sign}${wallet_diff:,.0f} ({wallet_sign}{wallet_pct:.1f}%)\n"
            
            # Dynamics
            dynamics = get_wallet_dynamics(name, wallet_fact)
            if dynamics:
                msg += f"├ {dynamics}\n"
            
            # Asset split
            assets = format_assets(data.get('tokens', []))
            if assets:
                msg += f"└ 💼 {assets}\n"
            else:
                # Replace last ├ with └
                msg = msg.rsplit("├", 1)[0] + "└" + msg.rsplit("├", 1)[1]
        
        # Balance-only wallets (no plan)
        for name, data in other_wallets.items():
            wallet_fact = data['total_usd']
            msg += f"\n<b>{wallet_label(name)}:</b>\n"
            msg += f"├ Баланс: {self.format_number(wallet_fact)}\n"
            
            # Dynamics
            dynamics = get_wallet_dynamics(name, wallet_fact)
            if dynamics:
                msg += f"├ {dynamics}\n"
            
            # Asset split
            assets = format_assets(data.get('tokens', []))
            if assets:
                msg += f"└ 💼 {assets}\n"
            else:
                # Replace last ├ with └
                msg = msg.rsplit("├", 1)[0] + "└" + msg.rsplit("├", 1)[1]
        
        # Links
        msg += "\n"
        for name, addr_or_dict in WALLETS.items():
            evm_addr = addr_or_dict.get("evm") if isinstance(addr_or_dict, dict) else addr_or_dict
            if evm_addr:
                msg += f"<a href='https://debank.com/profile/{evm_addr}'>{name}</a> · "
        msg += f"<a href='https://brkme.github.io/LP_Wallet_Tracker/whitelist.html'>Белый список</a>"
        
        # Weekly wisdom
        msg += "\n\n─────────────────\n"
        msg += "<i>💡 Талант и интеллект сильно переоценены. Умные люди часто слишком много думают, чрезмерно планируют и анализируют. Они прячутся за активностью, которая не создаёт никакого прогресса.\n\n"
        msg += "Правда в том, что таланта и интеллекта в мире предостаточно. А вот смелости — нет. Люди, которыми вы восхищаетесь — это те, у кого хватило смелости действовать. Они не талантливее вас. Они просто предприняли действие.</i>"
        
        return msg
    
    # ============ TELEGRAM ============
    
    async def send_telegram(self, message: str):
        """Send message to Telegram"""
        if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
            logger.error("Telegram credentials not configured")
            print(message)  # Print for testing
            return
        
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }
        
        try:
            async with self.session.post(url, json=payload, timeout=10) as resp:
                if resp.status == 200:
                    logger.info("✅ Message sent to Telegram")
                else:
                    error = await resp.text()
                    logger.error(f"Telegram error: {error}")
        except Exception as e:
            logger.error(f"Error sending to Telegram: {e}")
    
    # ============ MAIN ============
    
    async def run(self):
        """Main execution"""
        logger.info("🚀 Starting LP Wallet Tracker")
        
        # Get current month for plan
        current_month = datetime.now().strftime("%Y-%m")
        total_plan_usd = get_total_plan(current_month)
        logger.info(f"📋 Total plan for {current_month}: ${total_plan_usd:,}")
        
        # Fetch balances for all wallets
        total_usd = 0
        wallet_details = {}
        all_tokens = []
        
        for name, address_or_dict in WALLETS.items():
            # Multi-chain wallet (dict with evm/btc keys)
            if isinstance(address_or_dict, dict):
                logger.info(f"📊 Fetching {name} (multi-chain)")
                wallet_total = 0
                wallet_tokens = []
                
                # EVM chains
                evm_addr = address_or_dict.get("evm")
                if evm_addr:
                    logger.info(f"   ├ EVM: {evm_addr[:8]}...")
                    evm_data = await self.get_wallet_balance(evm_addr)
                    wallet_total += evm_data["total_usd"]
                    wallet_tokens.extend(evm_data.get("tokens", []))
                
                # Bitcoin
                btc_addr = address_or_dict.get("btc")
                if btc_addr:
                    logger.info(f"   ├ BTC: {btc_addr[:12]}...")
                    btc_data = await self.get_btc_balance(btc_addr)
                    wallet_total += btc_data["total_usd"]
                    wallet_tokens.extend(btc_data.get("tokens", []))
                
                # Zcash
                zec_addr = address_or_dict.get("zec")
                if zec_addr:
                    logger.info(f"   ├ ZEC: {zec_addr[:12]}...")
                    zec_data = await self.get_zec_balance(zec_addr)
                    wallet_total += zec_data["total_usd"]
                    wallet_tokens.extend(zec_data.get("tokens", []))
                
                wallet_plan = get_plan_for_wallet(name, current_month)
                wallet_details[name] = {
                    "addresses": address_or_dict,
                    "total_usd": wallet_total,
                    "plan_usd": wallet_plan,
                    "tokens": wallet_tokens
                }
                total_usd += wallet_total
                all_tokens.extend(wallet_tokens)
                
                logger.info(f"   └ Total: ${wallet_total:,.2f}" + (f" (Plan: ${wallet_plan:,})" if wallet_plan else ""))
            
            # Single EVM address (string)
            else:
                address = address_or_dict
                logger.info(f"📊 Fetching {name} ({address[:8]}...)")
                data = await self.get_wallet_balance(address)
                
                wallet_plan = get_plan_for_wallet(name, current_month)
                wallet_details[name] = {
                    "address": address,
                    "total_usd": data["total_usd"],
                    "plan_usd": wallet_plan,
                    "tokens": data.get("tokens", [])
                }
                total_usd += data["total_usd"]
                all_tokens.extend(data.get("tokens", []))
                
                logger.info(f"   └ Balance: ${data['total_usd']:,.2f}" + (f" (Plan: ${wallet_plan:,})" if wallet_plan else ""))
        
        logger.info(f"💰 Total: ${total_usd:,.2f} (Plan: ${total_plan_usd:,})")
        
        # Check whitelist
        whitelist_report = self.check_whitelist(all_tokens)
        
        # Load history and add record
        history = self.load_history()
        last_week = self.get_last_week_data(history)
        month_start = self.get_month_start_data(history)
        
        record = self.add_record(history, total_usd, total_plan_usd, wallet_details)
        
        # Save monthly snapshot if first week
        if self.is_first_week_of_month():
            self.save_monthly_snapshot(history, record)
        
        self.save_history(history)
        
        # Build and send message
        message = self.build_message(record, last_week, month_start, whitelist_report, history, all_tokens)
        await self.send_telegram(message)
        
        logger.info("✅ Done!")


async def main():
    async with WalletTracker() as tracker:
        await tracker.run()


if __name__ == "__main__":
    asyncio.run(main())
