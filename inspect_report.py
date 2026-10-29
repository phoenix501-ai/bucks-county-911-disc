"""Diagnostic only: inspect publicly visible county report; no Discord alerts."""
import asyncio
import json
import re
from pathlib import Path
from urllib.parse import urlparse
from playwright.async_api import async_playwright

START = 'https://www.buckscounty.gov/2150/911-Incident-Report'
OUT = Path('diagnostics')
OUT.mkdir(exist_ok=True)

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(viewport={'width': 1440, 'height': 1000}, device_scale_factor=1)
        await page.goto(START, wait_until='domcontentloaded', timeout=60000)
        await page.wait_for_timeout(6000)
        links = await page.locator('a[href]').evaluate_all('(els) => els.map(e => ({text: e.innerText.trim().slice(0,100), href: e.href}))')
        frames = await page.locator('iframe').evaluate_all('(els) => els.map(e => e.src)')
        candidates = [x['href'] for x in links if re.search(r'powerbi|app\.powerbigov|911.*incident', x['href'], re.I)]
        candidates += [x for x in frames if re.search(r'powerbi|powerbigov', x, re.I)]
        # Keep diagnostics limited to public URLs and visible text, never request headers/cookies/tokens.
        (OUT/'page-links.json').write_text(json.dumps({'candidate_links': list(dict.fromkeys(candidates)), 'iframe_count': len(frames)}, indent=2))
        await page.screenshot(path=str(OUT/'county-page.png'), full_page=True)
        (OUT/'county-visible-text.txt').write_text((await page.locator('body').inner_text())[:20000])
        if not candidates:
            print('No Power BI link identified automatically. Check screenshot and page-links.json.')
        else:
            target = candidates[0]
            print('Opening public dashboard host:', urlparse(target).hostname)
            try:
                await page.goto(target, wait_until='domcontentloaded', timeout=60000)
                await page.wait_for_timeout(20000)
                await page.screenshot(path=str(OUT/'dashboard.png'), full_page=True)
                (OUT/'dashboard-visible-text.txt').write_text((await page.locator('body').inner_text())[:30000])
                frame_summaries = []
                for f in page.frames:
                    try:
                        frame_summaries.append({'host': urlparse(f.url).hostname, 'visible_text_sample': (await f.locator('body').inner_text(timeout=5000))[:10000]})
                    except Exception as exc:
                        frame_summaries.append({'host': urlparse(f.url).hostname, 'error': type(exc).__name__})
                (OUT/'frames.json').write_text(json.dumps(frame_summaries, indent=2))
            except Exception as exc:
                (OUT/'dashboard-error.txt').write_text(f'{type(exc).__name__}: {exc}')
                print('Dashboard load failed:', type(exc).__name__)
        await browser.close()

asyncio.run(main())
