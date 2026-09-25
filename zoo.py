#!/usr/bin/env python3
"""
Toffee Live Playlist Scraper
Visits each channel's watch page to extract stream details
"""

import json
import os
import re
import sys
from datetime import datetime, timezone
from playwright.sync_api import sync_playwright
import requests

# Config
API_BASE = "https://entitlement-prod.services.toffeelive.com/toffee/BD/DK/web/playback/watch"
LIVE_URL = "https://toffeelive.com/en/live"
TOKEN = os.getenv("TOFFEE_TOKEN", "")

# Global storage
all_channels = []
accessible_channels = []


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


def check_entitlement(channel_id):
    """Check if user has access to this channel via API"""
    if not TOKEN:
        return {"has_access": True, "data": {}}  # Skip check if no token
    
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
        
        if access in ["allow", "true", "yes", "granted"]:
            return {"has_access": True, "data": data}
        return {"has_access": False, "data": data}
        
    except Exception as e:
        log(f"API Error: {e}")
        return {"has_access": False, "data": {}}


def extract_channels_from_main_page(page):
    """Extract all channel links from the main live page"""
    log(f"Loading main page: {LIVE_URL}")
    
    page.goto(LIVE_URL, wait_until="networkidle")
    page.wait_for_timeout(3000)
    
    # Scroll to load all channels (lazy loading)
    log("Scrolling to load all channels...")
    for _ in range(5):
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(1000)
    
    channels = []
    
    # Try multiple selectors to find channel cards/links
    selectors = [
        'a[href*="/watch/"]',
        'a[href*="/live/"]',
        '[data-testid*="channel"] a',
        '.channel-card a',
        '.live-item a',
        'a[data-channel-id]',
        '[class*="channel"] a[href]'
    ]
    
    for selector in selectors:
        elements = page.query_selector_all(selector)
        log(f"Selector '{selector}' found {len(elements)} elements")
        
        for el in elements:
            try:
                href = el.get_attribute('href')
                if not href:
                    continue
                
                # Make full URL
                if href.startswith('/'):
                    watch_url = f"https://toffeelive.com{href}"
                elif href.startswith('http'):
                    watch_url = href
                else:
                    continue
                
                # Extract channel ID from URL
                channel_id = ""
                id_match = re.search(r'/watch/([^/?]+)', href) or \
                          re.search(r'/live/([^/?]+)', href) or \
                          re.search(r'id=([^&]+)', href)
                if id_match:
                    channel_id = id_match.group(1)
                
                # Get channel name from element text or attributes
                name = el.inner_text().strip() or \
                       el.get_attribute('title') or \
                       el.get_attribute('aria-label') or \
                       "Unknown Channel"
                
                # Clean up name
                name = re.sub(r'\s+', ' ', name).strip()
                
                # Get logo from image inside the link
                logo = ""
                img = el.query_selector('img')
                if img:
                    logo = img.get_attribute('src') or \
                           img.get_attribute('data-src') or \
                           img.get_attribute('data-original') or ""
                
                # Get category from parent elements
                category = "Live TV"
                parent = el.query_selector('xpath=..')
                if parent:
                    cat_el = parent.query_selector('[class*="category"], [class*="genre"]')
                    if cat_el:
                        category = cat_el.inner_text().strip()
                
                channel = {
                    "id": channel_id,
                    "name": name,
                    "logo": logo,
                    "category": category,
                    "watch_url": watch_url,
                    "main_page_data": True
                }
                
                # Avoid duplicates
                if not any(c['watch_url'] == watch_url for c in channels):
                    channels.append(channel)
                    log(f"Found: {name} -> {watch_url}")
                    
            except Exception as e:
                continue
    
    log(f"\nTotal unique channels found: {len(channels)}")
    return channels


def scrape_watch_page(page, channel):
    """Visit individual watch page and extract all details"""
    watch_url = channel['watch_url']
    name = channel['name']
    
    log(f"\n--- Visiting watch page: {name} ---")
    log(f"URL: {watch_url}")
    
    try:
        # Navigate to watch page
        page.goto(watch_url, wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(4000)  # Wait for player to initialize
        
        # Get page title
        title = page.title()
        log(f"Page title: {title}")
        
        # Extract from page content
        content = page.content()
        
        # Try to get better quality logo from watch page
        logo_selectors = [
            'img[class*="logo"]',
            'img[class*="channel"]',
            'meta[property="og:image"]',
            '[class*="poster"] img',
            'img[alt*="logo"]'
        ]
        
        for sel in logo_selectors:
            try:
                img = page.query_selector(sel)
                if img:
                    better_logo = img.get_attribute('src') or img.get_attribute('content')
                    if better_logo and len(better_logo) > len(channel.get('logo', '')):
                        channel['logo'] = better_logo
                        break
            except:
                continue
        
        # Get description
        try:
            desc_el = page.query_selector('meta[name="description"], meta[property="og:description"]')
            if desc_el:
                channel['description'] = desc_el.get_attribute('content') or ""
        except:
            channel['description'] = ""
        
        # Extract M3U8 from various sources
        stream_url = None
        
        # 1. Check API response if we have token
        if channel.get('id') and TOKEN:
            entitlement = check_entitlement(channel['id'])
            if entitlement['has_access']:
                stream_url = entitlement['data'].get('url') or \
                            entitlement['data'].get('streamUrl') or \
                            entitlement['data'].get('playbackUrl') or \
                            entitlement['data'].get('manifestUrl')
                if stream_url:
                    log(f"Found stream from API")
                    channel['source'] = 'api'
        
        # 2. Search in page source
        if not stream_url:
            patterns = [
                r'(https?://[^\s"\']+\.m3u8[^\s"\']*)',
                r'"streamUrl"\s*:\s*"([^"]+)"',
                r'"playbackUrl"\s*:\s*"([^"]+)"',
                r'"url"\s*:\s*"(https?://[^"]+\.m3u8[^"]*)"',
                r'src:\s*["\']([^"\']+\.m3u8)["\']',
                r'var\s+source\s*=\s*["\']([^"\']+\.m3u8)["\']',
                r'file:\s*["\']([^"\']+\.m3u8)["\']',
            ]
            
            for pattern in patterns:
                match = re.search(pattern, content)
                if match:
                    stream_url = match.group(1)
                    log(f"Found stream in page source")
                    channel['source'] = 'page_source'
                    break
        
        # 3. Check browser storage and variables
        if not stream_url:
            try:
                storage = page.evaluate("""() => {
                    return {
                        local: localStorage.getItem('streamData') || localStorage.getItem('playerData'),
                        session: sessionStorage.getItem('streamData') || sessionStorage.getItem('playerData'),
                        window: window.playerConfig || window.streamData || window.videoData
                    };
                }""")
                
                for key, value in storage.items():
                    if value:
                        if isinstance(value, str):
                            match = re.search(r'(https?://[^\s"\']+\.m3u8)', value)
                            if match:
                                stream_url = match.group(1)
                                break
                        elif isinstance(value, dict):
                            for k, v in value.items():
                                if isinstance(v, str) and '.m3u8' in v:
                                    stream_url = v
                                    break
                    if stream_url:
                        log(f"Found stream from {key}")
                        channel['source'] = f'storage_{key}'
                        break
            except Exception as e:
                pass
        
        # 4. Network monitoring - check if any XHR fetched stream data
        if not stream_url:
            try:
                # Check if there's a network response with stream data
                responses = page.evaluate("""() => {
                    return window._networkResponses || [];
                }""")
                
                for resp in responses:
                    if '.m3u8' in str(resp):
                        stream_url = resp
                        break
            except:
                pass
        
        if stream_url:
            channel['stream_url'] = stream_url
            channel['has_stream'] = True
            log(f"✓ Stream URL: {stream_url[:80]}...")
        else:
            channel['has_stream'] = False
            log(f"✗ No stream URL found")
        
        # Get cookies for the domain
        try:
            cookies = page.context.cookies()
            channel['cookies'] = [
                {'name': c['name'], 'value': c['value'], 'domain': c['domain']}
                for c in cookies if 'toffeelive' in c.get('domain', '')
            ]
        except:
            channel['cookies'] = []
        
        # Get user agent
        channel['user_agent'] = page.evaluate("() => navigator.userAgent")
        
        return channel
        
    except Exception as e:
        log(f"Error scraping {name}: {e}")
        channel['error'] = str(e)
        return channel


def generate_playlists():
    """Generate M3U and JSON playlist files"""
    log(f"\nGenerating playlists...")
    
    # Filter channels with streams
    channels_with_stream = [c for c in accessible_channels if c.get('has_stream') and c.get('stream_url')]
    
    log(f"Channels with stream: {len(channels_with_stream)} / {len(accessible_channels)}")
    
    # Save detailed data
    with open("channels_data.json", "w", encoding="utf-8") as f:
        json.dump({
            "generated": datetime.now(timezone.utc).isoformat(),
            "total_found": len(all_channels),
            "accessible": len(accessible_channels),
            "with_stream": len(channels_with_stream),
            "channels": accessible_channels
        }, f, indent=2, ensure_ascii=False)
    
    # Generate M3U playlist
    with open("zoo_playlist.m3u", "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        f.write(f"# Playlist: Toffee Live Channels\n")
        f.write(f"# Generated: {datetime.now(timezone.utc).isoformat()}\n")
        f.write(f"# Total Channels: {len(channels_with_stream)}\n\n")
        
        for ch in channels_with_stream:
            name = ch.get('name', 'Unknown')
            logo = ch.get('logo', '')
            category = ch.get('category', 'Live TV')
            stream_url = ch.get('stream_url', '')
            ch_id = ch.get('id', '')
            
            # EXTINF line with all metadata
            f.write(f'#EXTINF:-1 ')
            f.write(f'tvg-id="{ch_id}" ')
            f.write(f'tvg-name="{name}" ')
            f.write(f'tvg-logo="{logo}" ')
            f.write(f'group-title="{category}" ')
            if ch.get('description'):
                f.write(f'tvg-description="{ch.get("description", "")}" ')
            f.write(f',{name}\n')
            
            # Stream URL with potential cookies hint
            f.write(f'#EXTVLCOPT:http-user-agent={ch.get("user_agent", "Mozilla/5.0")}\n')
            f.write(f'{stream_url}\n\n')
    
    # Generate JSON playlist
    playlist = {
        "playlist_name": "Toffee Live",
        "generated": datetime.now(timezone.utc).isoformat(),
        "total_channels": len(channels_with_stream),
        "channels": []
    }
    
    for ch in channels_with_stream:
        playlist["channels"].append({
            "id": ch.get("id", ""),
            "name": ch.get("name", ""),
            "logo": ch.get("logo", ""),
            "category": ch.get("category", ""),
            "description": ch.get("description", ""),
            "stream_url": ch.get("stream_url", ""),
            "watch_url": ch.get("watch_url", ""),
            "user_agent": ch.get("user_agent", ""),
            "cookies": ch.get("cookies", []),
            "source": ch.get("source", "unknown")
        })
    
    with open("zoo_playlist.json", "w", encoding="utf-8") as f:
        json.dump(playlist, f, indent=2, ensure_ascii=False)
    
    # Summary
    log(f"\n{'='*50}")
    log(f"✅ SCRAPING COMPLETE")
    log(f"{'='*50}")
    log(f"Total channels found: {len(all_channels)}")
    log(f"Accessible channels: {len(accessible_channels)}")
    log(f"Channels with stream: {len(channels_with_stream)}")
    log(f"\nFiles generated:")
    log(f"  - zoo_playlist.m3u ({len(channels_with_stream)} streams)")
    log(f"  - zoo_playlist.json")
    log(f"  - channels_data.json (full details)")


def main():
    log("="*50)
    log("TOFFEE LIVE SCRAPER")
    log("="*50)
    
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                '--disable-blink-features=AutomationControlled',
                '--disable-web-security',
                '--disable-features=IsolateOrigins,site-per-process'
            ]
        )
        
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.0.36",
            viewport={"width": 1920, "height": 1080},
            locale="en-US"
        )
        
        # Add auth if token available
        if TOKEN:
            context.add_cookies([{
                "name": "auth_token",
                "value": TOKEN,
                "domain": ".toffeelive.com",
                "path": "/"
            }])
        
        # Step 1: Get all channels from main page
        main_page = context.new_page()
        all_channels = extract_channels_from_main_page(main_page)
        main_page.close()
        
        if not all_channels:
            log("No channels found! Check selectors.")
            browser.close()
            return
        
        # Step 2: Visit each watch page and collect details
        log(f"\n{'='*50}")
        log(f"VISITING {len(all_channels)} WATCH PAGES")
        log(f"{'='*50}")
        
        for idx, channel in enumerate(all_channels, 1):
            log(f"\n[{idx}/{len(all_channels)}] Processing: {channel['name']}")
            
            # Check entitlement first (if we have token)
            if channel.get('id') and TOKEN:
                entitlement = check_entitlement(channel['id'])
                if not entitlement['has_access']:
                    log(f"  ⏭️  Skipped (no access)")
                    continue
            
            # Open new page for each watch URL
            watch_page = context.new_page()
            detailed_channel = scrape_watch_page(watch_page, channel)
            watch_page.close()
            
            accessible_channels.append(detailed_channel)
        
        browser.close()
    
    # Step 3: Generate playlists
    generate_playlists()


if __name__ == "__main__":
    main()
