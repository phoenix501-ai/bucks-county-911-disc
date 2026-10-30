"""Monitor Montgomery County PA public WebCAD RSS feed and send new entries to Discord."""
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

FEED = 'https://webapp07.montcopa.org/eoc/cadinfo/livecadrss.asp'
STATE = Path('seen_montgomery_incidents.json')
WEBHOOK = os.environ.get('MONTGOMERY_DISCORD_WEBHOOK_URL', '').strip()
COLORS = {'FIRE': 0xE74C3C, 'EMS': 0x9B59B6, 'TRAFFIC': 0x3498DB}


def request(url, payload=None):
    headers = {'User-Agent': 'CountyPublicIncidentMonitor/1.0', 'Accept': 'application/rss+xml, application/xml, text/xml, */*'}
    if payload is not None:
        headers['Content-Type'] = 'application/json'
    req = urllib.request.Request(url, data=payload, headers=headers)
    with urllib.request.urlopen(req, timeout=35) as resp:
        return resp.read()


def normalize(s):
    return re.sub(r'\s+', ' ', s or '').strip()


def parse_feed(xml):
    root = ET.fromstring(xml)
    items = root.findall('.//channel/item')
    if not items:
        raise RuntimeError('RSS contained no incident items; refusing to change saved state')
    parsed = []
    for item in items:
        title = normalize(item.findtext('title'))
        desc = normalize(item.findtext('description'))
        # Example description: ROAD & ROAD; MUNICIPALITY; 2026-10-29 @ 14:09:15-Station:STA3;
        # RSS pubDate is the feed refresh time, not the dispatch time; do not include it in identity.
        category, _, incident_type = title.partition(':')
        category = category.upper().strip()
        incident_type = incident_type.strip() or title
        if category not in COLORS:
            category = 'TRAFFIC' if 'TRAFFIC' in title.upper() else 'FIRE' if 'FIRE' in title.upper() else 'EMS' if 'EMS' in title.upper() else 'OTHER'
        segments = [normalize(s) for s in desc.split(';') if normalize(s)]
        location = segments[0] if segments else 'Not listed'
        municipality = segments[1] if len(segments) > 1 else 'Not listed'
        dispatch_match = re.search(r'\d{4}-\d{2}-\d{2}\s*@\s*\d{2}:\d{2}:\d{2}', desc)
        dispatch = dispatch_match.group(0).replace(' @ ', ' ') if dispatch_match else 'Not listed'
        # Do not use pubDate, station assignment, or changing status to identify incidents.
        stable = '|'.join([category, incident_type.upper(), location.upper(), municipality.upper(), dispatch])
        key = hashlib.sha256(stable.encode('utf-8')).hexdigest()
        parsed.append({'id': key, 'category': category, 'type': incident_type, 'location': location,
                       'municipality': municipality, 'dispatch': dispatch})
    return list({p['id']: p for p in parsed}.values())


def send_discord(item):
    if not WEBHOOK:
        raise RuntimeError('Missing MONTGOMERY_DISCORD_WEBHOOK_URL repository secret')
    fields = [
        {'name': 'Type', 'value': item['type'][:1024], 'inline': False},
        {'name': 'Dispatch', 'value': item['dispatch'][:1024], 'inline': True},
        {'name': 'Municipality', 'value': item['municipality'][:1024], 'inline': True},
        {'name': 'Location', 'value': item['location'][:1024], 'inline': False},
    ]
    body = json.dumps({'embeds': [{'title': f"🚨 MONTGOMERY COUNTY 911 — {item['category']}",
                                   'color': COLORS.get(item['category'], 0x95A5A6),
                                   'fields': fields}], 'allowed_mentions': {'parse': []}}).encode()
    for attempt in range(4):
        try:
            request(WEBHOOK, body)
            return
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt < 3:
                try:
                    wait = float(json.loads(exc.read()).get('retry_after', 2))
                except Exception:
                    wait = 2
                time.sleep(min(max(wait, 1), 30))
                continue
            raise


def main():
    incidents = parse_feed(request(FEED))
    print(f'Parsed {len(incidents)} Montgomery County RSS incidents', flush=True)
    if not STATE.exists():
        STATE.write_text(json.dumps(sorted(x['id'] for x in incidents), indent=2) + '\n')
        print('First run: seeded existing incidents without Discord alerts', flush=True)
        return
    seen = set(json.loads(STATE.read_text()))
    if not isinstance(json.loads(STATE.read_text()), list):
        raise RuntimeError('Invalid incident state file')
    new = [x for x in incidents if x['id'] not in seen]
    print(f'New incidents: {len(new)}', flush=True)
    # Send oldest first. Persist after EACH successful alert to avoid duplicates after partial failures.
    for incident in reversed(new):
        send_discord(incident)
        seen.add(incident['id'])
        STATE.write_text(json.dumps(sorted(seen), indent=2) + '\n')
        print(f"Alert sent: {incident['category']} / {incident['type']}", flush=True)
        time.sleep(0.4)
    # Bound the state file while retaining recent IDs. RSS only shows active incidents.
    # Do not prune by set order; retaining IDs is safer than accidental duplicate alerts.
    print('Monitoring check complete', flush=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(f'Monitor failed: {exc}', file=sys.stderr, flush=True)
        sys.exit(1)
