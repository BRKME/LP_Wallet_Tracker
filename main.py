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
    
    async def get_wallet_balance(self, address: str) -> dict:
        """Get wallet balance, trying multiple sources"""
        # Try scraping DeBank directly (most accurate)
        logger.info("Trying to scrape DeBank...")
        result = await self.get_wallet_balance_scrape(address)
        if result and result.get('total_usd', 0) > 0:
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
                return json.load(f)
        return {"records": [], "monthly_snapshots": {}}
    
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
    
    def build_message(self, record: dict, last_week: dict, month_start: dict, whitelist_report: dict) -> str:
        """Build the Telegram message"""
        today = datetime.now().strftime("%d.%m.%Y")
        
        msg = f"📊 <b>LP Portfolio Report</b>\n"
        msg += f"📅 {today}\n\n"
        
        # Total Plan vs Fact
        msg += f"<b>💰 ИТОГО:</b>\n"
        msg += f"├ План: {self.format_number(record['plan_usd'])}\n"
        msg += f"├ Факт: {self.format_number(record['total_usd'])}\n"
        
        diff = record['total_usd'] - record['plan_usd']
        diff_pct = (diff / record['plan_usd'] * 100) if record['plan_usd'] > 0 else 0
        emoji = "✅" if diff >= 0 else "⚠️"
        sign = "+" if diff >= 0 else ""
        msg += f"└ {emoji} Разница: {sign}${diff:,.0f} ({sign}{diff_pct:.1f}%)\n\n"
        
        # Per-wallet breakdown with plan/fact
        msg += f"<b>👛 По кошелькам:</b>\n"
        for name, data in record['wallets'].items():
            wallet_plan = data.get('plan_usd', 0)
            wallet_fact = data['total_usd']
            wallet_diff = wallet_fact - wallet_plan
            wallet_emoji = "✅" if wallet_diff >= 0 else "⚠️"
            wallet_sign = "+" if wallet_diff >= 0 else ""
            
            msg += f"\n<b>{name}:</b>\n"
            msg += f"├ План: {self.format_number(wallet_plan)}\n"
            msg += f"├ Факт: {self.format_number(wallet_fact)}\n"
            msg += f"└ {wallet_emoji} {wallet_sign}${wallet_diff:,.0f}\n"
        
        msg += "\n"
        
        # Weekly change
        if last_week:
            msg += f"<b>📈 Изменения за неделю:</b>\n"
            msg += f"└ {self.format_change(record['total_usd'], last_week['total_usd'])}\n\n"
        
        # Monthly change (first week only)
        if self.is_first_week_of_month() and month_start:
            msg += f"<b>📆 Изменения за месяц:</b>\n"
            msg += f"└ {self.format_change(record['total_usd'], month_start['total_usd'])}\n\n"
        
        # Links
        msg += f"\n🔗 <a href='https://debank.com/profile/{WALLETS['Аркаша']}'>DeBank Аркаша</a>"
        msg += f" | <a href='https://debank.com/profile/{WALLETS['Марта']}'>DeBank Марта</a>"
        msg += f"\n📋 <a href='https://brkme.github.io/LP_Wallet_Tracker/whitelist.html'>Белый список токенов</a>"
        
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
        
        for name, address in WALLETS.items():
            logger.info(f"📊 Fetching {name} ({address[:8]}...)")
            data = await self.get_wallet_balance(address)
            
            wallet_plan = get_plan_for_wallet(name, current_month)
            wallet_details[name] = {
                "address": address,
                "total_usd": data["total_usd"],
                "plan_usd": wallet_plan
            }
            total_usd += data["total_usd"]
            all_tokens.extend(data.get("tokens", []))
            
            logger.info(f"   └ Balance: ${data['total_usd']:,.2f} (Plan: ${wallet_plan:,})")
        
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
        message = self.build_message(record, last_week, month_start, whitelist_report)
        await self.send_telegram(message)
        
        logger.info("✅ Done!")


async def main():
    async with WalletTracker() as tracker:
        await tracker.run()


if __name__ == "__main__":
    asyncio.run(main())
