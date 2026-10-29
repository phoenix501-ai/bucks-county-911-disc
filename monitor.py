"""Monitor public Bucks County Power BI incident table; notify a Discord webhook.
Only public, visible report text is processed. No Power BI API or credentials used.
"""
import asyncio
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from playwright.async_api import async_playwright

SOURCE = 'https://www.buckscounty.gov/2150/911-Incident-Report'
STATE = Path('seen_incidents.json')
ID = re.compile(r'^(?:PD|ED|FD)\d{8,}$')
DATE = re.compile(r'^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d$')


def parse(text):
    rows = {}
    # The Power BI report exposes accessibility text for each visible table row.
    for block in text.split('Select Row')[1:]:
        lines = [s.strip() for s in block.splitlines() if s.strip()]
        if len(lines) < 4 or not ID.fullmatch(lines[0]) or not DATE.fullmatch(lines[2]):
            continue
        incident_id = lines[0]
        rows[incident_id] = {'id': incident_id, 'type': lines[1], 'time': lines[2], 'location': lines[3]}
    return rows


def post(webhook, item):
    # Avoid Discord @mentions and cap fields to safe sizes.
    def safe(s, n):
        return str(s).replace('@', '@\u200b')[:n]
    prefix = item['id'][:2].upper()
    category, color = {
        'FD': ('FIRE', 0xE53935),       # red
        'PD': ('POLICE', 0x1976D2),     # blue
        'ED': ('EMS', 0x8E44AD),        # purple
    }.get(prefix, ('INCIDENT', 0x808080))
    payload = json.dumps({
        'embeds': [{
            'title': f"🚨 BUCKS COUNTY 911 — {category} INCIDENT",
            'color': color,
            'fields': [
                {'name': 'Number', 'value': safe(item['id'], 60), 'inline': False},
                {'name': 'Type', 'value': safe(item['type'], 180), 'inline': False},
                {'name': 'Dispatch', 'value': safe(item['time'], 60), 'inline': False},
                {'name': 'Location', 'value': safe(item['location'], 250), 'inline': False},
            ],
        }],
        'allowed_mentions': {'parse': []},
    }).encode()
    req = Request(webhook, data=payload, headers={'Content-Type': 'application/json', 'User-Agent': 'BucksCountyPublicMonitor/1.0'}, method='POST')
    for attempt in range(3):
        try:
            with urlopen(req, timeout=20) as resp:
                if resp.status not in (200, 204):
                    raise RuntimeError(f'Discord HTTP {resp.status}')
            return
        except Exception as exc:
            if attempt == 2:
                raise RuntimeError(f'Discord notification failed for {item["id"]}: {type(exc).__name__}') from exc
            time.sleep(3 * (attempt + 1))


async def scrape():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(viewport={'width': 1440, 'height': 1000})
        try:
            await page.goto(SOURCE, wait_until='domcontentloaded', timeout=60000)
            await page.wait_for_timeout(5000)
            links = await page.locator('a[href]').evaluate_all('(els) => els.map(e => e.href)')
            frames = await page.locator('iframe').evaluate_all('(els) => els.map(e => e.src)')
            targets = []
            # Only follow URLs on the official Power BI Government host.
            for u in links + frames:
                try:
                    if (urlparse(u).hostname or '').lower() == 'app.powerbigov.us':
                        targets.append(u)
                except ValueError:
                    pass
            if not targets:
                raise RuntimeError('No public Power BI Government report link found')
            await page.goto(targets[0], wait_until='domcontentloaded', timeout=60000)
            best = {}
            for _ in range(12):
                await page.wait_for_timeout(2500)
                for frame in page.frames:
                    try:
                        text = await frame.locator('body').inner_text(timeout=3000)
                        parsed = parse(text)
                        if len(parsed) > len(best):
                            best = parsed
                    except Exception:
                        continue
                if best:
                    break
            return best
        finally:
            await browser.close()


def main():
    rows = asyncio.run(scrape())
    if not rows:
        raise RuntimeError('No incident rows parsed; preserving prior state and sending no alerts')
    print(f'Parsed {len(rows)} currently visible incident rows')
    if not STATE.exists():
        STATE.write_text(json.dumps({'seen': sorted(rows)}, indent=2) + '\n')
        print('First run: seeded existing incidents without alerts')
        return
    data = json.loads(STATE.read_text())
    seen = set(data.get('seen', []))
    new = [v for k, v in rows.items() if k not in seen]
    print(f'New incident IDs: {len(new)}')
    webhook = os.environ.get('DISCORD_WEBHOOK_URL', '')
    if new and not webhook:
        raise RuntimeError('Missing DISCORD_WEBHOOK_URL secret')
    for item in sorted(new, key=lambda x: x['time']):
        post(webhook, item)
        seen.add(item['id'])
        # Persist successful sends immediately, so partial runs do not re-send on rerun.
        STATE.write_text(json.dumps({'seen': sorted(seen)}, indent=2) + '\n')
    seen.update(rows)
    STATE.write_text(json.dumps({'seen': sorted(seen)}, indent=2) + '\n')

if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(f'MONITOR ERROR: {exc}', file=sys.stderr)
        sys.exit(1)
