"""
WeMov Pro — Backend API
Deploy: Render.com (free tier)

Endpoints:
  GET  /health
  POST /api/find-leads        → inicia busca em background
  GET  /api/job-status        → progresso da busca
  POST /api/send-emails       → envia emails para todos em stage='lead'
  POST /api/send-email/<id>   → envia email para um lead específico
  POST /api/send-followup/<id>→ envia follow-up para um lead específico
"""

import os, re, csv, time, json, uuid, threading, smtplib
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import requests
from flask import Flask, jsonify, request
from flask_cors import CORS
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
CORS(app)

# ─────────────────────────────────────────────────────────
# ENV
# ─────────────────────────────────────────────────────────
SB_URL     = os.getenv('SUPABASE_URL',     'https://jegccnxuzzyqmhfenhni.supabase.co')
SB_KEY     = os.getenv('SUPABASE_ANON_KEY')
GMAIL_ADDR = os.getenv('GMAIL_ADDRESS',    'wemovpro@gmail.com')
GMAIL_PASS = os.getenv('GMAIL_APP_PASSWORD')
SENDER     = os.getenv('SENDER_NAME',      'João')
SUBS_MIN   = int(os.getenv('SUBS_MIN',    '15000'))
SUBS_MAX   = int(os.getenv('SUBS_MAX',    '100000'))
FU_DAYS    = int(os.getenv('FOLLOWUP_DAYS','3'))

YT_KEYS = [k.strip() for k in os.getenv('YOUTUBE_API_KEYS', '').split(',') if k.strip()]

SB_HEADERS = {
    'apikey':        SB_KEY,
    'Authorization': f'Bearer {SB_KEY}',
    'Content-Type':  'application/json',
    'Prefer':        'return=minimal',
}

# ─────────────────────────────────────────────────────────
# JOB STATE
# ─────────────────────────────────────────────────────────
job = {'running': False, 'progress': 0, 'message': 'Idle', 'found': 0, 'error': None}

# ─────────────────────────────────────────────────────────
# SUPABASE HELPERS
# ─────────────────────────────────────────────────────────
def sb_get(table, params=''):
    r = requests.get(f'{SB_URL}/rest/v1/{table}?{params}', headers=SB_HEADERS, timeout=10)
    return r.json() if r.status_code == 200 else []

def sb_insert(table, row):
    r = requests.post(f'{SB_URL}/rest/v1/{table}', headers=SB_HEADERS, json=row, timeout=10)
    return r.status_code in (200, 201)

def sb_patch(table, row_id, changes):
    r = requests.patch(f'{SB_URL}/rest/v1/{table}?id=eq.{row_id}', headers=SB_HEADERS, json=changes, timeout=10)
    return r.status_code in (200, 201, 204)

def get_settings():
    rows = sb_get('app_settings')
    return {r['key']: r['value'] for r in rows}

def log_activity(type_, message, lead_id=None):
    try:
        sb_insert('activity_log', {
            'id':         str(uuid.uuid4()),
            'created_at': datetime.now(timezone.utc).isoformat(),
            'type':       type_,
            'message':    message,
            'lead_id':    lead_id,
        })
    except Exception:
        pass

def get_existing_emails():
    rows = sb_get('channels', 'select=email')
    return {r['email'] for r in rows if r.get('email')}

# ─────────────────────────────────────────────────────────
# YOUTUBE API
# ─────────────────────────────────────────────────────────
YT_BASE = 'https://www.googleapis.com/youtube/v3'
_ki = [0]
EMAIL_RE = re.compile(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}')
BLOCKED_DOMAINS = {'sentry.io','wix.com','wixpress.com','example.com','google.com',
                   'youtube.com','googleapis.com','googlemail.com','2x.png','1x.png',
                   'yt3.ggpht.com','ytimg.com','schema.org','noreply.com'}

HEADERS_WEB = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
               'Accept-Language': 'en-US,en;q=0.9'}

TERMOS = [
    'Dr. psychologist narcissism youtube weekly USA UK',
    'Dr. therapist anxiety weekly channel USA',
    'obesity medicine doctor weekly youtube USA UK',
    'registered dietitian weekly tips USA UK',
    'narcissistic abuse recovery therapist weekly USA UK',
    'certified financial planner weekly youtube USA UK',
    'personal finance advisor weekly USA UK',
    'mental health podcast youtube weekly USA UK',
    'wellness podcast host weekly channel USA',
    'business entrepreneur podcast youtube weekly USA',
    'physical therapist weekly youtube USA',
    'chiropractor weekly youtube USA UK tips',
    'real estate agent channel weekly tips USA',
    'immigration lawyer youtube weekly USA',
    'track and field coach youtube channel USA',
    'sports coach youtube weekly USA UK',
    'life coach weekly youtube USA UK',
    'weight loss doctor MD weekly youtube',
    'grief counselor weekly youtube USA UK',
    'relationship podcast youtube weekly USA UK',
]

def yt_key():
    if not YT_KEYS:
        return None
    return YT_KEYS[_ki[0] % len(YT_KEYS)]

def yt_next_key():
    _ki[0] += 1

def yt_search(query, region='US'):
    key = yt_key()
    if not key:
        return []
    try:
        r = requests.get(f'{YT_BASE}/search', params=dict(
            part='snippet', q=query, type='channel',
            regionCode=region, relevanceLanguage='en',
            maxResults=15, order='relevance', key=key
        ), timeout=10)
        data = r.json()
        if 'error' in data:
            yt_next_key()
            return []
        return [(it['id'].get('channelId') if isinstance(it['id'], dict) else it.get('id'))
                for it in data.get('items', [])]
    except Exception:
        return []

def yt_stats(channel_ids):
    key = yt_key()
    if not key or not channel_ids:
        return []
    try:
        r = requests.get(f'{YT_BASE}/channels', params=dict(
            part='snippet,statistics,contentDetails',
            id=','.join(channel_ids), key=key
        ), timeout=10)
        data = r.json()
        if 'error' in data:
            yt_next_key()
            return []
        return data.get('items', [])
    except Exception:
        return []

def yt_freq(playlist_id):
    key = yt_key()
    if not key or not playlist_id:
        return '?', None
    try:
        r = requests.get(f'{YT_BASE}/playlistItems', params=dict(
            part='snippet', playlistId=playlist_id, maxResults=6, key=key
        ), timeout=10)
        data = r.json()
        if 'error' in data:
            return '?', None
        items = data.get('items', [])
        dates = []
        for it in items:
            pub = it['snippet'].get('publishedAt', '')
            if pub:
                dates.append(datetime.fromisoformat(pub.replace('Z', '+00:00')))
        if not dates:
            return 'sem videos', None
        now  = datetime.now(timezone.utc)
        last = (now - dates[0]).days
        if last > 60:
            return f'parado {last}d', last
        if len(dates) < 2:
            return 'ativo', last
        diffs = [(dates[i] - dates[i+1]).days for i in range(len(dates)-1)]
        avg = sum(diffs) / len(diffs)
        if avg <= 9:   return 'semanal', last
        if avg <= 18:  return 'bi-semanal', last
        return f'mensal ({avg:.0f}d)', last
    except Exception:
        return '?', None

def valid_email(e):
    domain = e.split('@')[-1].lower()
    if domain in BLOCKED_DOMAINS: return False
    if any(w in domain for w in ['sentry','gravatar','noreply','no-reply']): return False
    if domain.endswith(('.png','.jpg','.gif','.webp')): return False
    if '.' not in domain or len(domain) < 5: return False
    return True

def scrape_email(url):
    try:
        r = requests.get(url, headers=HEADERS_WEB, timeout=9, allow_redirects=True)
        if r.status_code != 200: return ''
        for em in EMAIL_RE.findall(r.text):
            if valid_email(em): return em
    except Exception:
        pass
    return ''

def get_channel_sites(handle):
    key = yt_key()
    if not key: return []
    h = handle.lstrip('@')
    try:
        r = requests.get(f'{YT_BASE}/channels', params=dict(
            part='snippet', forHandle=h, key=key
        ), timeout=8)
        desc = r.json().get('items', [{}])[0].get('snippet', {}).get('description', '')
        skip = r'www\.youtube|youtu\.be|instagram|twitter|facebook|tiktok|linktr|bit\.ly'
        urls = re.findall(rf'https?://(?!{skip})[^\s\'\"<>]{{8,}}', desc)
        return [u.rstrip('/.') for u in urls[:3]]
    except Exception:
        return []

def find_email(handle):
    email = scrape_email(f'https://www.youtube.com/{handle}/about')
    if email: return email
    time.sleep(0.3)
    for site in get_channel_sites(handle):
        time.sleep(0.2)
        for url in [site, site+'/contact', site+'/contact-us', site+'/about']:
            email = scrape_email(url)
            time.sleep(0.3)
            if email: return email
    return ''

# ─────────────────────────────────────────────────────────
# SCRIPT GENERATION (template-based, no API cost)
# ─────────────────────────────────────────────────────────
def detect_niche(name, description=''):
    text = (name + ' ' + description).lower()
    if any(w in text for w in ['therapist','psychologist','mental health','narcis','anxiety','trauma','ptsd','adhd']):
        return 'mental health', 'mental health and therapy'
    if any(w in text for w in ['weight','obesity','diet','nutrition','dietitian','metabolic']):
        return 'health and nutrition', 'health and nutrition'
    if any(w in text for w in ['finance','financial','investing','wealth','tax','money','budget']):
        return 'personal finance', 'personal finance'
    if any(w in text for w in ['real estate','property','realtor','mortgage']):
        return 'real estate', 'real estate'
    if any(w in text for w in ['coach','fitness','track','field','sport','athlete','training']):
        return 'coaching and fitness', 'coaching and fitness'
    if any(w in text for w in ['doctor','dr.','md','physician','medical','health']):
        return 'health and medicine', 'health and medicine'
    if any(w in text for w in ['lawyer','attorney','legal','immigration']):
        return 'legal advice', 'legal advice'
    if any(w in text for w in ['business','entrepreneur','startup','marketing']):
        return 'business', 'entrepreneurship'
    return 'content creation', 'content creation'

def gen_script(name, niche_label, settings):
    tpl = settings.get('tpl1') or f"""Subject: I already edited a video for your channel

Hey {{first_name}},

I came across your channel while going through {{niche}} content on YouTube and your approach really stood out. What you are doing in the space is genuinely different from most creators.

I went ahead and put together a short edited version of one of your recent videos. I wanted to show you something real rather than just sending a pitch.

If you want to take a look, just reply here and I will send it right over.

{{sender}}
Video Editor"""
    first = name.split()[0] if name else 'there'
    parts = name.split()
    if parts and parts[0].lower() in ('dr.','dr','prof.','prof','mr.','mrs.','ms.'):
        first = parts[1] if len(parts) > 1 else name
    return (tpl
        .replace('{first_name}',    first)
        .replace('{channel_name}',  name)
        .replace('{niche}',         niche_label)
        .replace('{sender}',        SENDER))

# ─────────────────────────────────────────────────────────
# FIND LEADS JOB (background thread)
# ─────────────────────────────────────────────────────────
def run_find_leads():
    global job
    job = {'running': True, 'progress': 0, 'message': 'Coletando IDs...', 'found': 0, 'error': None}

    try:
        cfg = get_settings()
        subs_min = int(cfg.get('subs_min', SUBS_MIN))
        subs_max = int(cfg.get('subs_max', SUBS_MAX))
        existing = get_existing_emails()

        # FASE 1: Coleta de IDs
        seen, all_ids = set(), []
        total_terms = len(TERMOS)
        for i, term in enumerate(TERMOS):
            job['message']  = f'Buscando: {term[:40]}...'
            job['progress'] = int((i / total_terms) * 25)
            for region in ['US', 'GB']:
                for cid in yt_search(term, region):
                    if cid and cid not in seen:
                        all_ids.append(cid)
                        seen.add(cid)
                time.sleep(0.15)
            yt_next_key()

        job['message'] = f'{len(all_ids)} IDs coletados. Filtrando...'
        job['progress'] = 25

        # FASE 2: Filtro de subs
        candidates = []
        for i in range(0, len(all_ids), 50):
            batch = all_ids[i:i+50]
            for ch in yt_stats(batch):
                subs   = int(ch['statistics'].get('subscriberCount', 0))
                title  = ch['snippet']['title']
                cid    = ch['id']
                custom = ch['snippet'].get('customUrl', '')
                url    = f'https://www.youtube.com/{custom}' if custom else f'https://www.youtube.com/channel/{cid}'
                uploads = ch.get('contentDetails', {}).get('relatedPlaylists', {}).get('uploads', '')
                country = ch['snippet'].get('country', '?')
                if subs_min <= subs <= subs_max:
                    candidates.append({'name': title, 'subs': subs, 'country': country,
                                       'url': url, 'uploads': uploads, 'handle': custom})
            time.sleep(0.1)
            job['progress'] = 25 + int(((i/max(len(all_ids),1)) * 25))

        # FASE 3: Frequência
        job['message'] = f'{len(candidates)} canais. Verificando frequência...'
        job['progress'] = 50
        active = []
        for c in candidates:
            freq, last = yt_freq(c['uploads'])
            ok = 'semanal' in freq or 'bi-semanal' in freq or freq == 'ativo'
            if ok:
                c['freq'] = freq
                active.append(c)
            time.sleep(0.1)

        # FASE 4: Emails + inserção
        job['message'] = f'{len(active)} canais ativos. Buscando emails...'
        inserted = 0
        for i, c in enumerate(active):
            job['progress'] = 75 + int((i / max(len(active), 1)) * 24)
            job['message']  = f'Extraindo email: {c["name"][:30]}...'

            email = find_email(c['handle'] or c['url'].split('/')[-1])
            c['email'] = email
            time.sleep(0.7)

            if not email or email in existing:
                continue

            niche_short, niche_label = detect_niche(c['name'])
            script = gen_script(c['name'], niche_label, cfg)

            row = {
                'id':      str(uuid.uuid4()),
                'name':    c['name'],
                'email':   email,
                'niche':   niche_short,
                'subs':    str(c['subs']),
                'stage':   'pendente',
                'script':  script,
                'url':     c['url'],
                'created': datetime.now(timezone.utc).isoformat(),
            }
            if sb_insert('channels', row):
                existing.add(email)
                inserted += 1
                job['found'] = inserted

        job['message']  = f'Concluído! {inserted} novos leads adicionados.'
        job['progress'] = 100
        log_activity('find_done', f'Busca concluída: {inserted} novos leads adicionados')

    except Exception as e:
        job['error']   = str(e)
        job['message'] = f'Erro: {str(e)}'
        log_activity('error', f'Erro na busca: {str(e)}')
    finally:
        job['running'] = False

# ─────────────────────────────────────────────────────────
# EMAIL SENDING
# ─────────────────────────────────────────────────────────
def send_gmail(to, subject, body):
    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From']    = f'{SENDER} <{GMAIL_ADDR}>'
    msg['To']      = to
    msg.attach(MIMEText(body, 'plain', 'utf-8'))
    with smtplib.SMTP('smtp.gmail.com', 587, timeout=25) as s:
        s.ehlo(); s.starttls(); s.login(GMAIL_ADDR, GMAIL_PASS)
        s.sendmail(GMAIL_ADDR, to, msg.as_string())

def parse_script(script):
    lines = script.strip().split('\n')
    subject, body_lines, past = '', [], False
    for line in lines:
        if not past and line.lower().startswith('subject:'):
            subject = line.split(':', 1)[1].strip()
            past = True
        elif past:
            body_lines.append(line)
    while body_lines and not body_lines[0].strip():
        body_lines.pop(0)
    return subject, '\n'.join(body_lines).strip()

def do_send_one(lead_id):
    rows = sb_get('channels', f'id=eq.{lead_id}&select=*')
    if not rows: return False, 'Lead não encontrado'
    l = rows[0]
    if not l.get('email'): return False, 'Sem email'
    if not l.get('script'): return False, 'Sem script'
    subject, body = parse_script(l['script'])
    if not subject or not body: return False, 'Script malformado'
    send_gmail(l['email'], subject, body)
    sb_patch('channels', lead_id, {'stage': 'email_enviado', 'sent_at': datetime.now(timezone.utc).isoformat()})
    log_activity('email_sent', f'Email enviado para "{l["name"]}" ({l["email"]})', lead_id)
    return True, 'ok'

# ─────────────────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────────────────
@app.route('/health')
def health():
    return jsonify({'status': 'ok', 'time': datetime.now(timezone.utc).isoformat()})

@app.route('/api/find-leads', methods=['POST'])
def api_find_leads():
    if job.get('running'):
        return jsonify({'error': 'running'}), 409
    if not YT_KEYS:
        return jsonify({'error': 'YOUTUBE_API_KEYS not configured'}), 500
    t = threading.Thread(target=run_find_leads, daemon=True)
    t.start()
    log_activity('find_start', 'Busca por novos leads iniciada')
    return jsonify({'status': 'started'})

@app.route('/api/job-status')
def api_job_status():
    return jsonify(job)

@app.route('/api/send-emails', methods=['POST'])
def api_send_emails():
    if not GMAIL_PASS:
        return jsonify({'error': 'GMAIL_APP_PASSWORD not configured'}), 500
    leads = sb_get('channels', 'stage=eq.lead&email=neq.&script=neq.&select=*')
    sent, errors = 0, 0
    for l in leads:
        try:
            ok, msg = do_send_one(l['id'])
            if ok: sent += 1
            else: errors += 1
            time.sleep(2)
        except Exception as e:
            errors += 1
    log_activity('batch_send', f'{sent} emails enviados em lote')
    return jsonify({'sent': sent, 'errors': errors})

@app.route('/api/send-email/<lead_id>', methods=['POST'])
def api_send_one(lead_id):
    if not GMAIL_PASS:
        return jsonify({'ok': False, 'msg': 'GMAIL_APP_PASSWORD não configurado no Render'}), 500
    try:
        ok, msg = do_send_one(lead_id)
        return jsonify({'ok': ok, 'msg': msg})
    except smtplib.SMTPAuthenticationError:
        return jsonify({'ok': False, 'msg': 'Senha do Gmail inválida — verifique GMAIL_APP_PASSWORD no Render'}), 500
    except smtplib.SMTPException as e:
        return jsonify({'ok': False, 'msg': f'Erro SMTP: {str(e)}'}), 500
    except Exception as e:
        return jsonify({'ok': False, 'msg': str(e)}), 500

@app.route('/api/send-followup/<lead_id>', methods=['POST'])
def api_send_followup(lead_id):
    if not GMAIL_PASS:
        return jsonify({'error': 'GMAIL_APP_PASSWORD not configured'}), 500
    rows = sb_get('channels', f'id=eq.{lead_id}&select=*')
    if not rows: return jsonify({'error': 'not found'}), 404
    l = rows[0]
    cfg = get_settings()
    tpl = cfg.get('tpl2') or f"""Subject: Re: sample edit for your channel

Hey {{first_name}},

Just wanted to make sure my last email did not get buried in your inbox.

I have a sample edit ready for your channel and would love to show you what it looks like.

If you are curious, just reply here and I will send it right away.

{SENDER}
Video Editor"""
    first = l['name'].split()[0] if l.get('name') else 'there'
    body = tpl.replace('{first_name}', first).replace('{sender}', SENDER)
    subject = 'Re: sample edit for your channel'
    try:
        send_gmail(l['email'], subject, body)
        sb_patch('channels', lead_id, {
            'stage':              'follow_up',
            'followup_sent_at':   datetime.now(timezone.utc).isoformat()
        })
        log_activity('followup_sent', f'Follow-up enviado para "{l["name"]}"', lead_id)
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'ok': False, 'msg': str(e)}), 500

# ─────────────────────────────────────────────────────────
# AUTO FOLLOW-UP (scheduled check — call via cron or Render cron job)
# ─────────────────────────────────────────────────────────
@app.route('/api/run-followups', methods=['POST'])
def api_run_followups():
    """Verifica leads com followup_enabled=true que não responderam em FU_DAYS dias."""
    cfg = get_settings()
    days = int(cfg.get('followup_days', FU_DAYS))
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    leads = sb_get('channels',
        f'stage=eq.email_enviado&followup_enabled=eq.true&sent_at=lt.{cutoff}&followup_sent_at=is.null&select=*')
    sent = 0
    for l in leads:
        try:
            api_send_followup(l['id'])
            sent += 1
            time.sleep(2)
        except Exception:
            pass
    return jsonify({'sent': sent})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
