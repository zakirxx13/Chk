#!/usr/bin/env python3
"""
Toffee Live Playlist Scraper
Extracts accessible channels and their M3U8 streams
"""

import json
import os
import re
import sys
from datetime import datetime, timezone
from playwright.sync_api import sync_playwright, Request
import requests

# Config
API_BASE = "https://entitlement-prod.services.toffeelive.com/toffee/BD/DK/web/playback/watch"
LIVE_URL = "https://toffeelive.com/en/live"
TOKEN = os.getenv("TOFFEE_TOKEN", "")

# Storage
channels_data = []
m3u8_links = {}
accessible_channels = []


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


def check_entitlement(channel_id):
    """Check if user has access to this channel"""
    try:
        headers = {
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "application/json",
            "Referer": "https://toffeelive.com/",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        
        response = requests.post(
            API_BASE,
            headers=headers,
            json={"id": channel_id},
            timeout=10
        )
        
        data = response.json()
        access = data.get("access", "deny")
        
        log(f"Channel {channel_id}: access={access}")
        
        if access in ["allow", "true", "yes", "granted"]:
            return {
                "has_access": True,
                "data": data
            }
        return {"has_access": False, "data": data}
        
    except Exception as e:
        log(f"Error checking entitlement for {channel_id}: {e}")
        return {"has_access": False, "data": {}}


def intercept_m3u8(route, request):
    """Intercept M3U8 network requests"""
    url = request.url
    if ".m3u8" in url or "playlist" in url.lower():
        log(f"Found M3U8: {url[:80]}...")
        # Store by channel name if available in referer
        m3u8_links["last_found"] = url
    route.continue_()


def extract_channels_from_page(page):
    """Extract channel list from live page"""
    log("Extracting channels from page...")
    
    channels = []
    
    # Wait for page to load
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(3000)  # Extra wait for JS rendering
    
    # Try multiple selectors based on common patterns
    selectors = [
        '[data-channel-id]',
        '[data-id]',
        '.channel-card',
        '.live-channel',
        '[class*="channel"]',
        'a[href*="watch"]',
        'a[href*="live"]'
    ]
    
    for selector in selectors:
        try:
            elements = page.query_selector_all(selector)
            log(f"Selector '{selector}' found {len(elements)} elements")
            
            for el in elements:
                try:
                    # Extract ID
                    channel_id = el.get_attribute('data-channel-id') or \
                                el.get_attribute('data-id') or \
                                el.get_attribute('data-channel')
                    
                    # Extract name
                    name = el.inner_text().strip() or \
                           el.get_attribute('title') or \
                           el.get_attribute('alt') or \
                           "Unknown"
                    
                    # Extract logo
                    logo = ""
                    img = el.query_selector('img')
                    if img:
                        logo = img.get_attribute('src') or \
                               img.get_attribute('data-src') or ""
                    
                    # Extract category
                    category = "Live TV"
                    cat_el = el.query_selector('[class*="category"], [class*="genre"]')
                    if cat_el:
                        category = cat_el.inner_text().strip()
                    
                    # Get watch URL
                    href = el.get_attribute('href') or ""
                    if href and not href.startswith('http'):
                        href = f"https://toffeelive.com{href}"
                    
                    if channel_id or href:
                        channels.append({
                            "id": channel_id or "",
                            "name": name.replace('\n', ' ').strip(),
                            "logo": logo,
                            "category": category,
                            "watch_url": href
                        })
                        
                except Exception as e:
                    continue
                    
        except Exception as e:
            continue
    
    # Remove duplicates
    seen = set()
    unique = []
    for ch in channels:
        key = ch.get('id') or ch.get('name')
        if key and key not in seen:
            seen.add(key)
            unique.append(ch)
    
    log(f"Found {len(unique)} unique channels")
    return unique


def get_stream_from_watch_page(page, watch_url, channel_name):
    """Visit watch page and extract M3U8"""
    log(f"Visiting watch page: {channel_name}")
    
    try:
        # Clear previous M3U8
        m3u8_links["last_found"] = None
        
        # Navigate to watch page
        page.goto(watch_url, wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(5000)  # Wait for player to load
        
        # Check for M3U8 in page source
        content = page.content()
        
        # Common patterns for M3U8 URLs
        patterns = [
            r'(https?://[^\s"\']+\.m3u8[^\s"\']*)',
            r'(https?://[^\s"\']+playlist[^\s"\']*\.m3u8)',
            r'"streamUrl"\s*:\s*"([^"]+)"',
            r'"url"\s*:\s*"(https?://[^"]+\.m3u8[^"]*)"',
            r'src:\s*["\']([^"\']+\.m3u8)["\']',
        ]
        
        found_url = m3u8_links.get("last_found")
        
        if not found_url:
            for pattern in patterns:
                match = re.search(pattern, content)
                if match:
                    found_url = match.group(1)
                    break
        
        # Check localStorage or sessionStorage for stream data
        if not found_url:
            storage_data = page.evaluate("""() => {
                return {
                    local: JSON.stringify(localStorage),
                    session: JSON.stringify(sessionStorage)
                };
            }""")
            
            for storage in [storage_data.get('local', ''), storage_data.get('session', '')]:
                match = re.search(r'(https?://[^\s"\']+\.m3u8)', storage)
                if match:
                    found_url = match.group(1)
                    break
        
        # Try to get from window.__INITIAL_STATE__ or similar
        if not found_url:
            initial_state = page.evaluate("""() => {
                return window.__INITIAL_STATE__ || 
                       window.__DATA__ || 
                       window.__APP_STATE__ || 
                       {};
            }""")
            
            if initial_state:
                state_str = json.dumps(initial_state)
                match = re.search(r'(https?://[^\s"\']+\.m3u8)', state_str)
                if match:
                    found_url = match.group(1)
        
        return found_url
        
    except Exception as e:
        log(f"Error getting stream for {channel_name}: {e}")
        return None


def main():
    log("Starting Toffee Scraper...")
    
    if not TOKEN:
        log("WARNING: TOFFEE_TOKEN not set!")
    
    with sync_playwright() as p:
        # Launch browser
        browser = p.chromium.launch(
            headless=True,
            args=['--disable-blink-features=AutomationControlled']
        )
        
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWeb/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1920, "height": 1080},
            locale="en-US",
            timezone_id="Asia/Dhaka"
        )
        
        # Add auth cookie if token available
        if TOKEN:
            context.add_cookies([{
                "name": "token",
                "value": TOKEN,
                "domain": ".toffeelive.com",
                "path": "/"
            }])
        
        page = context.new_page()
        
        # Intercept network requests
        page.route("**/*.m3u8*", intercept_m3u8)
        page.route("**/*playlist*", intercept_m3u8)
        
        # Navigate to live page
        log(f"Loading {LIVE_URL}")
        page.goto(LIVE_URL, wait_until="networkidle")
        
        # Extract channels
        channels = extract_channels_from_page(page)
        
        # Save raw data
        with open("channels_raw.json", "w", encoding="utf-8") as f:
            json.dump(channels, f, indent=2, ensure_ascii=False)
        
        # Process each channel
        for idx, channel in enumerate(channels, 1):
            log(f"\n--- Processing {idx}/{len(channels)}: {channel['name']} ---")
            
            channel_id = channel.get('id', '')
            
            # Check entitlement first
            if TOKEN and channel_id:
                entitlement = check_entitlement(channel_id)
                
                if not entitlement["has_access"]:
                    log(f"Skipping {channel['name']} - no access")
                    continue
                
                # Add entitlement data
                channel["entitlement"] = entitlement["data"]
                
                # Try to get M3U8 from entitlement response first
                stream_url = entitlement["data"].get("url") or \
                            entitlement["data"].get("streamUrl") or \
                            entitlement["data"].get("playbackUrl")
                
                if stream_url:
                    channel["stream_url"] = stream_url
                    log(f"Got stream from API: {stream_url[:60]}...")
                
                # If watch URL exists and no stream yet, visit page
                elif channel.get("watch_url"):
                    stream_url = get_stream_from_watch_page(
                        context.new_page(), 
                        channel["watch_url"], 
                        channel["name"]
                    )
                    if stream_url:
                        channel["stream_url"] = stream_url
            
            accessible_channels.append(channel)
        
        browser.close()
    
    # Generate output files
    log(f"\nGenerating playlists for {len(accessible_channels)} accessible channels...")
    
    # Save detailed JSON
    with open("channels_data.json", "w", encoding="utf-8") as f:
        json.dump({
            "generated": datetime.now(timezone.utc).isoformat(),
            "total_channels": len(accessible_channels),
            "channels": accessible_channels
        }, f, indent=2, ensure_ascii=False)
    
    # Generate M3U playlist
    with open("zoo_playlist.m3u", "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        f.write(f"# Playlist: Toffee Live\n")
        f.write(f"# Generated: {datetime.now(timezone.utc).isoformat()}\n")
        f.write(f"# Total Channels: {len(accessible_channels)}\n\n")
        
        for ch in accessible_channels:
            if ch.get("stream_url"):
                logo = ch.get("logo", "")
                category = ch.get("category", "Live TV")
                name = ch.get("name", "Unknown")
                
                f.write(f'#EXTINF:-1 tvg-id="{ch.get("id", "")}" ')
                f.write(f'tvg-name="{name}" ')
                f.write(f'tvg-logo="{logo}" ')
                f.write(f'group-title="{category}",{name}\n')
                f.write(f'{ch["stream_url"]}\n\n')
    
    # Generate simplified JSON playlist
    playlist_json = {
        "playlist_name": "Toffee Live",
        "generated": datetime.now(timezone.utc).isoformat(),
        "channels": [
            {
                "id": ch.get("id", ""),
                "name": ch.get("name", ""),
                "logo": ch.get("logo", ""),
                "category": ch.get("category", ""),
                "url": ch.get("stream_url", "")
            }
            for ch in accessible_channels if ch.get("stream_url")
        ]
    }
    
    with open("zoo_playlist.json", "w", encoding="utf-8") as f:
        json.dump(playlist_json, f, indent=2, ensure_ascii=False)
    
    log("\n✅ Done!")
    log(f"Files generated:")
    log(f"  - zoo_playlist.m3u ({len(playlist_json['channels'])} channels with streams)")
    log(f"  - zoo_playlist.json")
    log(f"  - channels_data.json")


if __name__ == "__main__":
    main()
