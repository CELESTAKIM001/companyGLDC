import os, re, json, base64, hashlib, secrets, smtplib, time
from datetime import datetime, timedelta, timezone
from functools import wraps
from email.message import EmailMessage
from io import BytesIO
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

import requests
import bcrypt
import jwt
from dotenv import load_dotenv
from flask import Flask, render_template, request, jsonify, redirect, url_for, session, flash, send_file, abort, Response
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument

load_dotenv()

def env(name, *aliases, default=None):
    """Read the canonical Python setting, then compatible hosted aliases."""
    for key in (name, *aliases):
        value = os.getenv(key)
        if value is not None and str(value).strip() != '':
            return value
    return default

def env_bool(name, *aliases, default=False):
    return str(env(name, *aliases, default=str(default))).strip().lower() in {'1','true','yes','on'}

app = Flask(__name__)
APP_ENV = str(env('APP_ENV', 'NODE_ENV', default='production')).lower()
APP_URL = str(env('APP_URL', default='http://localhost:5000')).rstrip('/')
ADMIN_PATH = str(env('ADMIN_PATH', default='/admin')).strip() or '/admin'
if not ADMIN_PATH.startswith('/'):
    ADMIN_PATH = '/' + ADMIN_PATH
ADMIN_PATH = ADMIN_PATH.rstrip('/') or '/admin'
AUTH_SECRET = str(env('AUTH_SECRET', 'JWT_ACCESS_SECRET', default=''))
# Never crash a serverless function during module import because environment variables are missing.
# Production readiness is reported by /api/ready, while configured deployments use the real secret.
app.secret_key = AUTH_SECRET or secrets.token_hex(32)
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = str(env('COOKIE_SAME_SITE', default='Lax')).capitalize()
app.config['SESSION_COOKIE_SECURE'] = os.getenv('COOKIE_SECURE', 'true' if APP_ENV == 'production' else 'false').lower() == 'true'
app.config['SESSION_COOKIE_NAME'] = os.getenv('SESSION_COOKIE_NAME', 'gldc_session')
app.config['SESSION_COOKIE_PATH'] = '/'
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=int(os.getenv('SESSION_LIFETIME_HOURS','8')))
app.config['MAX_CONTENT_LENGTH'] = int(env('MAX_CONTENT_LENGTH_MB', default=env('MAX_FILE_SIZE_MB', default='25'))) * 1024 * 1024

APP_TIMEZONE = str(env('APP_TIMEZONE', 'TIMEZONE', default='Africa/Nairobi'))
CURRENCY = str(env('CURRENCY', default='KES'))
PAYMENTS_ENABLED = env_bool('PAYMENTS_ENABLED', default=True)
MEMBERSHIP_ENABLED = env_bool('MEMBERSHIP_ENABLED', default=True)
EMAIL_NOTIFICATIONS_ENABLED = env_bool('EMAIL_NOTIFICATIONS_ENABLED', default=True)
ADMIN_NOTIFICATIONS_ENABLED = env_bool('ADMIN_NOTIFICATIONS_ENABLED', default=True)
DOCUMENT_STORAGE = str(env('DOCUMENT_STORAGE', default='google_drive'))
PDF_ENABLED = env_bool('PDF_ENABLED', default=True)
PDF_QR_ENABLED = env_bool('PDF_QR_ENABLED', default=False)
SIGNATURE_ENABLED = env_bool('SIGNATURE_ENABLED', default=False)
MONGO_URI = os.getenv('MONGODB_URI', '')
MONGO_DB = os.getenv('MONGODB_DB_NAME', 'gldc')

mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=10000, maxPoolSize=20, tz_aware=True) if MONGO_URI else None
db = mongo_client[MONGO_DB] if mongo_client is not None else None


def now(): return datetime.now(timezone.utc)
def oid(x):
    try:
        from bson import ObjectId
        return ObjectId(x)
    except Exception:
        return x


def json_error(message, status=400, code='ERROR'):
    return jsonify(ok=False, error={'code': code, 'message': message}), status


def rate_limit(key, maximum=None, window=None):
    if db is None: return
    window = int(window or os.getenv('RATE_LIMIT_WINDOW_SECONDS', '60'))
    maximum = int(maximum or os.getenv('RATE_LIMIT_MAX_REQUESTS', '60'))
    bucket = int(datetime.now(timezone.utc).timestamp()) // window
    k = f'{key}:{bucket}'
    r = db.rate_limits.find_one_and_update({'key': k}, {'$inc': {'count': 1}, '$setOnInsert': {'createdAt': now()}}, upsert=True, return_document=ReturnDocument.AFTER)
    if r and r.get('count', 1) > maximum: raise RuntimeError('RATE_LIMITED')


def hash_password(p): return bcrypt.hashpw(p.encode(), bcrypt.gensalt(12)).decode() if False else bcrypt.hashpw(p.encode(), bcrypt.gensalt(12)).decode('utf-8')
def verify_password(p, h): return bcrypt.checkpw(p.encode(), h.encode())


def current_user(): return session.get('user')

def login_required(f):
    @wraps(f)
    def w(*a, **kw):
        if not current_user():
            if request.path.startswith('/api/'): return json_error('Authentication required.', 401, 'UNAUTHORIZED')
            return redirect(url_for('admin'))
        return f(*a, **kw)
    return w

def admin_required(f):
    @wraps(f)
    def w(*a, **kw):
        u=current_user()
        if not u: return json_error('Authentication required.', 401, 'UNAUTHORIZED') if request.path.startswith('/api/') else redirect(url_for('admin'))
        if u.get('role') not in ['SUPER ADMIN / OWNER','ADMIN']:
            return json_error('Administrator access required.',403,'FORBIDDEN')
        return f(*a, **kw)
    return w


def bootstrap_admin():
    if db is None: return
    email=str(env('INITIAL_ADMIN_EMAIL','ADMIN_EMAIL', default='')).strip().lower(); password=str(env('INITIAL_ADMIN_PASSWORD','ADMIN_INITIAL_PASSWORD', default=''))
    if not email or not password: return
    if len(password) < 12: raise RuntimeError('INITIAL_ADMIN_PASSWORD must be at least 12 characters.')
    if db.users.count_documents({'role':'SUPER ADMIN / OWNER'}) == 0:
        db.users.update_one({'email':email},{'$setOnInsert':{'email':email,'name':str(env('ADMIN_NAME', default='GLDC Administrator')),'passwordHash':hash_password(password),'role':'SUPER ADMIN / OWNER','status':'ACTIVE','createdAt':now(),'updatedAt':now()}},upsert=True)


def send_email(to, subject, text, html=None, attachments=None):
    host=env('SMTP_HOST'); user=env('SMTP_USER','GMAIL_USER'); password=env('SMTP_PASSWORD','GMAIL_APP_PASSWORD')
    if not host: raise RuntimeError('SMTP_NOT_CONFIGURED')
    msg=EmailMessage();
    from_name=str(env('EMAIL_FROM_NAME', default='GLDC')); from_address=str(env('EMAIL_FROM_ADDRESS', default='')).strip()
    smtp_from=str(env('SMTP_FROM', default='')).strip()
    if not from_address and smtp_from:
        m=re.match(r'^\s*(.*?)\s*<([^<>]+)>\s*$', smtp_from)
        if m:
            from_name=(m.group(1).strip() or from_name); from_address=m.group(2).strip()
        else:
            from_address=smtp_from
    if not from_address: from_address=user or ''
    msg['From']=f'{from_name} <{from_address}>' if from_address else user; msg['To']=to; msg['Subject']=subject
    reply_to=env('EMAIL_REPLY_TO','SMTP_REPLY_TO');
    if reply_to: msg['Reply-To']=reply_to
    msg.set_content(text)
    if html: msg.add_alternative(html, subtype='html')
    for filename, data, mimetype in attachments or []:
        maintype, subtype = mimetype.split('/', 1)
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
    port=int(env('SMTP_PORT', default='587')); secure=env_bool('SMTP_SECURE', default=False)
    if secure:
        with smtplib.SMTP_SSL(host,port) as s: s.login(user,password); s.send_message(msg)
    else:
        with smtplib.SMTP(host,port) as s:
            s.ehlo(); s.starttls(); s.ehlo(); s.login(user,password); s.send_message(msg)


def build_invoice_pdf(invoice):
    buf=BytesIO(); c=canvas.Canvas(buf, pagesize=A4); w,h=A4
    company=os.getenv('COMPANY_NAME','Gavin Land & Design Consultants')
    contact=' • '.join(x for x in [os.getenv('SMTP_FROM',''), env('COMPANY_PHONE','PHONE', default='')] if x)
    c.setTitle(invoice['invoiceNumber'])
    c.setFont('Helvetica-Bold',20); c.drawString(25*mm,h-30*mm,company)
    c.setFont('Helvetica',9); c.drawString(25*mm,h-37*mm,'Land • Design • Development • Project Consultancy')
    c.setFont('Helvetica-Bold',16); c.drawRightString(w-25*mm,h-30*mm,'INVOICE')
    c.setFont('Helvetica',9); c.drawRightString(w-25*mm,h-37*mm,invoice['invoiceNumber'])
    y=h-60*mm
    c.setFont('Helvetica-Bold',10); c.drawString(25*mm,y,'BILL TO')
    c.setFont('Helvetica',10); c.drawString(25*mm,y-7*mm,invoice.get('recipientName') or invoice['recipientEmail'])
    c.drawString(25*mm,y-14*mm,invoice['recipientEmail'])
    c.setFont('Helvetica-Bold',10); c.drawRightString(w-25*mm,y,'ISSUED')
    c.setFont('Helvetica',10); c.drawRightString(w-25*mm,y-7*mm,str(invoice.get('issuedAt',''))[:10])
    c.setFont('Helvetica-Bold',10); c.drawRightString(w-25*mm,y-14*mm,'DUE')
    c.setFont('Helvetica',10); c.drawRightString(w-25*mm,y-21*mm,invoice.get('dueDate') or 'On receipt')
    y-=38*mm
    c.setFont('Helvetica-Bold',10); c.drawString(25*mm,y,'DESCRIPTION'); c.drawRightString(w-25*mm,y,'AMOUNT')
    c.line(25*mm,y-3*mm,w-25*mm,y-3*mm)
    c.setFont('Helvetica',10); c.drawString(25*mm,y-12*mm,invoice['description'][:90]); c.drawRightString(w-25*mm,y-12*mm,f"KES {float(invoice['amount']):,.2f}")
    c.line(25*mm,y-20*mm,w-25*mm,y-20*mm)
    c.setFont('Helvetica-Bold',12); c.drawRightString(w-25*mm,y-32*mm,f"TOTAL DUE: KES {float(invoice['amount']):,.2f}")
    c.setFont('Helvetica',9); c.drawString(25*mm,25*mm,'Thank you for choosing GLDC.')
    if contact: c.drawRightString(w-25*mm,25*mm,contact)
    c.save(); return buf.getvalue()

def request_otp(email):
    if db is None: raise RuntimeError('DATABASE_UNAVAILABLE')
    email=email.lower().strip(); cool=int(os.getenv('OTP_RESEND_COOLDOWN_SECONDS','60')); expiry=int(os.getenv('OTP_EXPIRY_MINUTES','10'))
    # Use a UTC datetime that MongoDB can compare consistently.
    cooldown_since = now() - timedelta(seconds=cool)
    if db.otps.find_one({'email':email,'createdAt':{'$gt':cooldown_since}}): raise RuntimeError('OTP_COOLDOWN')
    code=f'{secrets.randbelow(900000)+100000}'
    db.otps.insert_one({'email':email,'codeHash':hashlib.sha256(code.encode()).hexdigest(),'createdAt':now(),'expiresAt':now()+timedelta(minutes=expiry),'attempts':0})
    send_email(email,'GLDC verification code',f'Your GLDC verification code is {code}. It expires in {expiry} minutes.',f'<p>Your GLDC verification code is <strong>{code}</strong>.</p><p>It expires in {expiry} minutes.</p>')


def verify_otp(email, code):
    if db is None:
        raise RuntimeError('DATABASE_UNAVAILABLE')
    email = str(email or '').strip().lower()
    code = str(code or '').strip()
    if not email or '@' not in email:
        raise RuntimeError('OTP_INVALID')
    if not re.fullmatch(r'\d{6}', code):
        raise RuntimeError('OTP_INVALID')

    max_attempts = int(os.getenv('OTP_MAX_ATTEMPTS', '5'))
    try:
        x = db.otps.find_one({'email': email}, sort=[('createdAt', DESCENDING)])
    except Exception as exc:
        app.logger.exception('OTP lookup failed request=%s email=%s', getattr(request, 'request_id', 'unknown'), email)
        raise RuntimeError('DATABASE_UNAVAILABLE') from exc

    if not x:
        raise RuntimeError('OTP_EXPIRED')

    # Be defensive with records created by older versions of the application.
    expires_at = x.get('expiresAt')
    if not isinstance(expires_at, datetime):
        raise RuntimeError('OTP_EXPIRED')
    # PyMongo returns BSON UTC datetimes as naive datetimes unless tz_aware=True.
    # Normalize both sides to UTC-aware before comparing, otherwise Python raises
    # TypeError: can't compare offset-naive and offset-aware datetimes, which was
    # causing the verification endpoint to return HTTP 500 even though OTP sending
    # and MongoDB itself were working.
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    else:
        expires_at = expires_at.astimezone(timezone.utc)
    if expires_at < now():
        raise RuntimeError('OTP_EXPIRED')

    attempts = x.get('attempts', 0)
    try:
        attempts = int(attempts)
    except (TypeError, ValueError):
        attempts = 0
    if attempts >= max_attempts:
        raise RuntimeError('OTP_LOCKED')

    stored_hash = x.get('codeHash')
    expected_hash = hashlib.sha256(code.encode('utf-8')).hexdigest()
    if not isinstance(stored_hash, str) or not secrets.compare_digest(stored_hash, expected_hash):
        try:
            db.otps.update_one({'_id': x['_id']}, {'$inc': {'attempts': 1}})
        except Exception:
            app.logger.exception('OTP attempt update failed request=%s email=%s', getattr(request, 'request_id', 'unknown'), email)
        raise RuntimeError('OTP_INVALID')

    try:
        db.otps.delete_many({'email': email})
    except Exception as exc:
        app.logger.exception('OTP cleanup failed request=%s email=%s', getattr(request, 'request_id', 'unknown'), email)
        raise RuntimeError('DATABASE_UNAVAILABLE') from exc
    return True


def google_creds():
    # Accept the common Vercel formats safely: raw service-account JSON,
    # base64-encoded JSON, or the individual service-account env fields.
    raw = os.getenv('GOOGLE_SERVICE_ACCOUNT_JSON', '').strip()
    creds = None
    if raw:
        try:
            creds = json.loads(raw)
        except json.JSONDecodeError:
            try:
                creds = json.loads(base64.b64decode(raw).decode('utf-8'))
            except Exception as exc:
                raise RuntimeError('GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON or base64-encoded JSON.') from exc
    else:
        key = os.getenv('GOOGLE_PRIVATE_KEY', '')
        # Vercel env values may arrive with literal \n characters.
        key = key.replace('\\n', '\n').strip()
        creds = {
            'type': 'service_account',
            'client_email': os.getenv('GOOGLE_SERVICE_ACCOUNT_EMAIL', '').strip(),
            'private_key': key,
            'token_uri': 'https://oauth2.googleapis.com/token'
        }

    if not isinstance(creds, dict):
        raise RuntimeError('Google service-account credentials must be a JSON object.')

    # Some deployments paste the private key as a nested object. Google Auth
    # expects PEM text/bytes, so fail with a useful message instead of the
    # cryptography error: "a bytes-like object is required, not dict".
    private_key = creds.get('private_key')
    if isinstance(private_key, dict):
        private_key = private_key.get('value') or private_key.get('private_key')
    if private_key is not None:
        if not isinstance(private_key, str):
            raise RuntimeError('Google private key must be PEM text, not an object.')
        creds['private_key'] = private_key.replace('\\n', '\n').strip()
    if not creds.get('client_email') or not creds.get('private_key'):
        raise RuntimeError('Google service-account email/private key is not configured.')
    return creds


def google_service(service, scopes):
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    creds = service_account.Credentials.from_service_account_info(google_creds(), scopes=scopes)
    return build(service, 'v3', credentials=creds, cache_discovery=False)


def drive_files(minimum=40, max_files=200):
    """List real files from the configured Drive folder with pagination.

    The admin console should have at least 40 real records available for a
    useful integration test when the folder contains that many. We never
    fabricate files: if the folder contains fewer, the actual count is returned.
    Additional Drive pages are fetched up to max_files.
    """
    if os.getenv('GOOGLE_DRIVE_ENABLED','true').lower()!='true': return []
    d=google_service('drive',['https://www.googleapis.com/auth/drive.readonly'])
    folder=os.getenv('GOOGLE_DRIVE_FOLDER_ID','')
    if not folder: raise RuntimeError('GOOGLE_DRIVE_FOLDER_NOT_CONFIGURED')
    target=max(1, int(minimum or 40))
    limit=max(target, int(max_files or 200))
    fields='nextPageToken,files(id,name,mimeType,size,modifiedTime,webViewLink,webContentLink,shortcutDetails)'
    files=[]
    token=None
    while len(files) < limit:
        kwargs=dict(q=f"'{folder}' in parents and trashed=false", fields=fields,
                    orderBy='modifiedTime desc', pageSize=min(100, limit-len(files)),
                    supportsAllDrives=True, includeItemsFromAllDrives=True)
        if token:
            kwargs['pageToken']=token
        r=d.files().list(**kwargs).execute()
        batch=r.get('files',[]) or []
        files.extend(batch)
        token=r.get('nextPageToken')
        if not token or not batch:
            break
    return files[:limit]


def _drive_download_bytes(http_request, label='Drive download'):
    """Download Drive media/export content into bytes safely."""
    import io
    from googleapiclient.http import MediaIoBaseDownload

    buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(buffer, http_request, chunksize=1024 * 1024)
    done = False
    try:
        while not done:
            _, done = downloader.next_chunk(num_retries=2)
    except Exception as exc:
        raise RuntimeError(f'{label} failed: {exc}') from exc

    data = buffer.getvalue()
    if not isinstance(data, (bytes, bytearray)):
        raise RuntimeError(f'{label} returned an invalid response type: {type(data).__name__}')
    if not data:
        raise RuntimeError(f'{label} returned an empty file.')
    return bytes(data)


def _drive_download_direct(file_id, meta=None, label='Drive media download'):
    """Download a Drive binary file using an authenticated HTTPS request.

    This deliberately bypasses the googleapiclient media request wrapper. Some
    serverless/proxy combinations have returned the Drive *metadata JSON* even
    though the request was created with alt=media. An AuthorizedSession makes
    the actual media URL explicit and lets us validate the response before it
    reaches Flask's PDF response.
    """
    import requests as _requests
    from google.auth.transport.requests import AuthorizedSession
    from google.oauth2 import service_account

    creds = service_account.Credentials.from_service_account_info(
        google_creds(), scopes=['https://www.googleapis.com/auth/drive.readonly']
    )
    session = AuthorizedSession(creds)
    url = f'https://www.googleapis.com/drive/v3/files/{file_id}'
    try:
        r = session.get(url, params={'alt':'media','supportsAllDrives':'true'}, timeout=60)
    except Exception as exc:
        raise RuntimeError(f'{label} request failed: {exc}') from exc

    content = r.content or b''
    if r.status_code >= 400:
        try:
            detail = r.json()
        except Exception:
            detail = (content[:500].decode('utf-8', errors='replace') if content else r.text[:500])
        raise RuntimeError(f'{label} HTTP {r.status_code}: {detail}')

    ctype = (r.headers.get('Content-Type') or '').lower()
    if content.lstrip().startswith(b'{'):
        # The exact symptom previously seen: Drive metadata JSON was returned
        # instead of the binary PDF. Surface it as a distinct diagnostic.
        try:
            detail = r.json()
            kind = detail.get('kind') if isinstance(detail, dict) else None
            name = detail.get('name') if isinstance(detail, dict) else None
            returned_mime = detail.get('mimeType') if isinstance(detail, dict) else None
            raise RuntimeError(
                f'{label} returned JSON instead of binary media '
                f'(kind={kind!r}, name={name!r}, mimeType={returned_mime!r}, contentType={ctype!r}).'
            )
        except RuntimeError:
            raise
        except Exception:
            raise RuntimeError(f'{label} returned JSON instead of binary media (contentType={ctype!r}).')

    return content


def drive_metadata(file_id):
    d=google_service('drive',['https://www.googleapis.com/auth/drive.readonly'])
    fields='id,name,mimeType,size,modifiedTime,webViewLink,webContentLink,shortcutDetails,capabilities'
    meta=d.files().get(fileId=file_id,fields=fields,supportsAllDrives=True).execute()
    shortcut=meta.get('shortcutDetails') or {}
    if meta.get('mimeType') == 'application/vnd.google-apps.shortcut' and shortcut.get('targetId'):
        file_id=shortcut['targetId']
        meta=d.files().get(fileId=file_id,fields=fields,supportsAllDrives=True).execute()
    return d, file_id, meta

def drive_read(file_id):
    d,file_id,meta=drive_metadata(file_id)
    mt=meta.get('mimeType','')
    if mt=='application/vnd.google-apps.document':
        data=_drive_download_bytes(d.files().export(fileId=file_id,mimeType='text/plain'), 'Google Docs export')
        return meta,data.decode('utf-8', errors='replace'),None
    if mt=='application/vnd.google-apps.spreadsheet':
        data=_drive_download_bytes(d.files().export(fileId=file_id,mimeType='text/csv'), 'Google Sheets export')
        return meta,data.decode('utf-8', errors='replace'),None
    if mt.startswith('text/'):
        data=_drive_download_bytes(d.files().get(fileId=file_id,alt='media'), 'Text file download')
        return meta,data.decode('utf-8', errors='replace'),None
    if mt=='application/pdf':
        data=_drive_download_direct(file_id, meta, 'PDF download')
        return meta,None,data
    return meta,None,None

def drive_download(file_id):
    """Return downloadable bytes for ordinary Drive files.

    Google Workspace-native files are exported to a useful standard format;
    ordinary binary files (ZIP, DOCX, XLSX, images, etc.) are downloaded as
    their original Drive media.
    """
    d,file_id,meta=drive_metadata(file_id)
    mt=meta.get('mimeType','')
    name=meta.get('name') or 'download'
    if mt=='application/vnd.google-apps.document':
        data=_drive_download_bytes(d.files().export(fileId=file_id,mimeType='application/pdf'), 'Google Docs PDF export')
        if not name.lower().endswith('.pdf'): name += '.pdf'
        return meta,name,'application/pdf',data
    if mt=='application/vnd.google-apps.spreadsheet':
        data=_drive_download_bytes(d.files().export(fileId=file_id,mimeType='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'), 'Google Sheets XLSX export')
        if not name.lower().endswith('.xlsx'): name += '.xlsx'
        return meta,name,'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',data
    if mt=='application/vnd.google-apps.presentation':
        data=_drive_download_bytes(d.files().export(fileId=file_id,mimeType='application/pdf'), 'Google Slides PDF export')
        if not name.lower().endswith('.pdf'): name += '.pdf'
        return meta,name,'application/pdf',data
    data=_drive_download_direct(file_id, meta, 'Drive file download')
    return meta,name,mt or 'application/octet-stream',data

def daraja_token():
    base='https://api.safaricom.co.ke' if os.getenv('DARAJA_ENV','production')=='production' else 'https://sandbox.safaricom.co.ke'
    raw=f"{os.getenv('DARAJA_CONSUMER_KEY','')}:{os.getenv('DARAJA_CONSUMER_SECRET','')}".encode()
    auth=base64.b64encode(raw).decode()
    r=requests.get(base+'/oauth/v1/generate?grant_type=client_credentials',headers={'Authorization':'Basic '+auth},timeout=20); r.raise_for_status(); return base,r.json()['access_token']

def daraja_stk(phone,amount,reference,description):
    base,access=daraja_token(); ts=datetime.now().strftime('%Y%m%d%H%M%S'); short=str(env('DARAJA_SHORTCODE','DARAJA_PARTY_A_SHORTCODE', default='')); passkey=str(env('DARAJA_PASSKEY', default=''))
    password=base64.b64encode(f'{short}{passkey}{ts}'.encode()).decode()
    callback = str(env('DARAJA_CALLBACK_URL', default=APP_URL + '/api/payments/callback')).rstrip('/')
    if not callback.endswith('/api/payments/callback'):
        callback += '/api/payments/callback'
    body={'BusinessShortCode':str(env('DARAJA_SHORTCODE','DARAJA_PARTY_A_SHORTCODE', default='')),'Password':password,'Timestamp':ts,'TransactionType':str(env('DARAJA_TRANSACTION_TYPE','MPESA_TRANSACTION_TYPE', default='CustomerBuyGoodsOnline')),'Amount':round(amount),'PartyA':str(env('DARAJA_PARTY_A_SHORTCODE','DARAJA_SHORTCODE', default='')),'PartyB':str(env('DARAJA_TILL_NUMBER','DARAJA_PARTY_B_BUYGOODS_TILL', default='')),'PhoneNumber':phone,'CallBackURL':callback,'AccountReference':reference,'TransactionDesc':description}
    r=requests.post(base+'/mpesa/stkpush/v1/processrequest',headers={'Authorization':'Bearer '+access,'Content-Type':'application/json'},json=body,timeout=30); data=r.json()
    if not r.ok or data.get('ResponseCode')!='0': raise RuntimeError('DARAJA_STK_FAILED:'+str(data.get('ResponseDescription') or data.get('errorMessage') or 'Unknown error'))
    return data

def client_ip():
    # Only trust X-Forwarded-For when the deployment explicitly enables proxy trust.
    if os.getenv('TRUST_PROXY', 'false').lower() == 'true':
        forwarded = request.headers.get('X-Forwarded-For', '')
        if forwarded: return forwarded.split(',')[0].strip()
    return request.remote_addr or 'unknown'

def validate_production_config():
    if APP_ENV != 'production':
        return []
    required_groups = [
        ('MONGODB_URI',),
        ('MONGODB_DB_NAME',),
        ('AUTH_SECRET', 'JWT_ACCESS_SECRET'),
        ('SMTP_HOST',),
        ('SMTP_USER', 'GMAIL_USER'),
        ('SMTP_PASSWORD', 'GMAIL_APP_PASSWORD'),
        ('SMTP_FROM', 'EMAIL_FROM_ADDRESS'),
    ]
    missing = []
    for group in required_groups:
        if not any(env(k) for k in group):
            missing.append('/'.join(group))
    admin_email = env('INITIAL_ADMIN_EMAIL', 'ADMIN_EMAIL')
    admin_password = env('INITIAL_ADMIN_PASSWORD', 'ADMIN_INITIAL_PASSWORD')
    if admin_email and not admin_password:
        missing.append('INITIAL_ADMIN_PASSWORD/ADMIN_INITIAL_PASSWORD')

    if env_bool('DARAJA_ENABLED', default=True):
        daraja_groups = [
            ('DARAJA_CONSUMER_KEY',), ('DARAJA_CONSUMER_SECRET',),
            ('DARAJA_SHORTCODE', 'DARAJA_PARTY_A_SHORTCODE'),
            ('DARAJA_TILL_NUMBER', 'DARAJA_PARTY_B_BUYGOODS_TILL', 'DARAJA_BUYGOODS_TILL'),
            ('DARAJA_PASSKEY',),
            ('DARAJA_CALLBACK_URL',),
        ]
        for group in daraja_groups:
            if not any(env(k) for k in group):
                missing.append('/'.join(group))

    if env_bool('GOOGLE_DRIVE_ENABLED', default=True):
        if not env('GOOGLE_DRIVE_FOLDER_ID'):
            missing.append('GOOGLE_DRIVE_FOLDER_ID')
        if not env('GOOGLE_SERVICE_ACCOUNT_JSON') and not (env('GOOGLE_SERVICE_ACCOUNT_EMAIL') and env('GOOGLE_PRIVATE_KEY')):
            missing.append('GOOGLE_SERVICE_ACCOUNT_EMAIL/GOOGLE_PRIVATE_KEY or GOOGLE_SERVICE_ACCOUNT_JSON')
    if env_bool('GOOGLE_SHEETS_ENABLED', default=False) and not env('GOOGLE_SPREADSHEET_ID'):
        missing.append('GOOGLE_SPREADSHEET_ID')
    return list(dict.fromkeys(missing))

CONFIG_MISSING = [] if APP_ENV != 'production' else validate_production_config()

def csrf_token():
    token=session.get('_csrf')
    if not token:
        token=secrets.token_urlsafe(32); session['_csrf']=token
    return token

@app.before_request
def request_context():
    request.request_id = request.headers.get('X-Request-ID') or secrets.token_hex(12)

@app.before_request
def security_middleware():
    if request.method in {'POST','PUT','PATCH','DELETE'} and request.path != '/api/payments/callback':
        token=request.headers.get('X-CSRF-Token')
        if not token or not secrets.compare_digest(token, session.get('_csrf','')):
            return json_error('Security token missing or invalid.', 403, 'CSRF_FAILED') if request.path.startswith('/api/') else abort(403)

@app.after_request
def security_headers(response):
    response.headers.setdefault('X-Request-ID', getattr(request, 'request_id', 'unknown'))
    response.headers.setdefault('X-Content-Type-Options','nosniff')
    response.headers.setdefault('X-Frame-Options','DENY')
    response.headers.setdefault('Referrer-Policy','strict-origin-when-cross-origin')
    response.headers.setdefault('Permissions-Policy','camera=(), microphone=(), geolocation=()')
    response.headers.setdefault('Cross-Origin-Opener-Policy','same-origin')
    response.headers.setdefault('Content-Security-Policy', "default-src 'self'; img-src 'self' data: https:; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; script-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")
    if request.is_secure or app.config['SESSION_COOKIE_SECURE']:
        response.headers.setdefault('Strict-Transport-Security','max-age=31536000; includeSubDomains')
    return response

@app.context_processor
def inject_security():
    # Expose the CSRF token helper itself because templates call csrf_token().
    # Returning the generated string here would shadow the callable and cause
    # Jinja to raise: TypeError: 'str' object is not callable.
    return {'csrf_token': csrf_token}

@app.context_processor
def globals_(): return {'current_user':current_user(),'currency':CURRENCY,'year':datetime.now().year,'admin_path':ADMIN_PATH,'app_url':APP_URL}

@app.get('/api/health')
def api_health():
    # Liveness + real MongoDB connectivity check for the admin dashboard.
    # This endpoint is intentionally exempt from lazy DB initialization so it
    # can diagnose configuration/connectivity without causing an import crash.
    started = time.perf_counter()
    if db is None:
        return jsonify(ok=False, service='gldc', environment=APP_ENV, status='DEGRADED',
                       database='NOT_CONFIGURED', configurationMissing=CONFIG_MISSING,
                       latencyMs=round((time.perf_counter()-started)*1000, 2),
                       requestId=request.request_id), 503
    try:
        db.client.admin.command('ping')
        latency = round((time.perf_counter()-started)*1000, 2)
        return jsonify(ok=True, service='gldc', environment=APP_ENV, status='LIVE',
                       database='CONNECTED', latencyMs=latency, requestId=request.request_id), 200
    except Exception:
        latency = round((time.perf_counter()-started)*1000, 2)
        return jsonify(ok=False, service='gldc', environment=APP_ENV, status='DEGRADED',
                       database='UNREACHABLE', latencyMs=latency, requestId=request.request_id), 503

@app.get('/api/ready')
def api_ready():
    missing = CONFIG_MISSING
    database_ok = ensure_database_initialized() if not missing else False
    if missing:
        return jsonify(ok=False, ready=False, error={'code':'CONFIGURATION_INCOMPLETE','message':'Required production configuration is missing.','missing':missing}), 503
    if not database_ok:
        return jsonify(ok=False, ready=False, error={'code':'DATABASE_UNAVAILABLE','message':'Database is unavailable.','detail':_database_init_error}), 503
    return jsonify(ok=True, ready=True, database=True, integrations={'smtp': bool(env('SMTP_HOST')), 'googleDrive': env_bool('GOOGLE_DRIVE_ENABLED', default=True), 'googleSheets': env_bool('GOOGLE_SHEETS_ENABLED', default=False), 'daraja': env_bool('DARAJA_ENABLED', default=True)}, requestId=request.request_id), 200

@app.route('/')
def home(): return render_template('home.html', title='Land. Design. Development. Done Right.')
@app.route('/about')
def about(): return render_template('about.html', title='About GLDC')
@app.route('/services')
def services(): return render_template('services.html', title='Services')
@app.route('/service-detail')
def service_detail(): return render_template('service_detail.html', title='Consultancy Service')
@app.route('/projects')
def projects(): return render_template('projects.html', title='Projects')
@app.route('/project-detail')
def project_detail(): return render_template('project_detail.html', title='Project Details')
@app.route('/team')
def team(): return render_template('team.html', title='Team')
@app.route('/testimonials')
def testimonials(): return render_template('testimonials.html', title='Testimonials')
@app.route('/service-areas')
def service_areas(): return render_template('service_areas.html', title='Service Areas')
@app.route('/contact')
def contact(): return render_template('contact.html', title='Contact')
@app.route('/quote')
def quote(): return render_template('quote.html', title='Request a Quote')
@app.route('/member')
def member(): return render_template('member.html', title='Member Area')
@app.route('/verify')
def verify_page(): return render_template('verify.html', title='Verification')
@app.route('/document')
@login_required
def document_page(): return render_template('document.html', title='Documents')
@app.route('/faq')
def faq(): return render_template('faq.html', title='Frequently Asked Questions')
@app.route('/insights')
def insights():
    posts=[]
    if db is not None:
        try: posts=[clean_doc(x) for x in db.posts.find({'status':'PUBLISHED'}).sort('publishedAt',DESCENDING).limit(50)]
        except Exception: pass
    return render_template('insights.html', title='Insights & Resources', posts=posts)
@app.route('/consultation')
def consultation(): return render_template('consultation.html', title='Request a Consultation')
@app.route('/privacy')
def privacy(): return render_template('privacy.html', title='Privacy Policy')
@app.route('/terms')
def terms(): return render_template('terms.html', title='Terms & Conditions')

@app.route('/admin')
@app.route(ADMIN_PATH)
def admin(): return render_template('admin.html', title='Management Console')

@app.post('/api/auth/login')
def api_login():
    try:
        rate_limit('login:'+client_ip(), maximum=int(os.getenv('LOGIN_RATE_LIMIT_MAX','10'))); bootstrap_admin(); b=request.get_json(force=True); email=str(b.get('email','')).strip().lower(); password=str(b.get('password',''))
        u=db.users.find_one({'email':email,'status':'ACTIVE'}) if db is not None else None
        if not u or not u.get('passwordHash') or not verify_password(password,u['passwordHash']): return json_error('Invalid email or password.',401,'INVALID_CREDENTIALS')
        session.clear(); session.permanent=True; csrf_token(); session['user']={'id':str(u['_id']),'email':u['email'],'name':u.get('name',''),'role':u['role']}; db.audit.insert_one({'action':'LOGIN','user':u['email'],'createdAt':now()}); return jsonify(ok=True,user=session['user'])
    except RuntimeError as e:
        return json_error('Too many requests. Please try again later.', 429, 'RATE_LIMITED') if str(e)=='RATE_LIMITED' else json_error('Unable to sign in.', 500)
    except Exception:
        return json_error('Unable to sign in.',500)

@app.post('/api/auth/logout')
def api_logout():
    session.clear(); return jsonify(ok=True)
@app.get('/api/auth/me')
def api_me(): return jsonify(ok=True,user=current_user())

@app.post('/api/auth/request-otp')
def api_request_otp():
    try:
        rate_limit('otp:'+client_ip(), maximum=int(os.getenv('OTP_RATE_LIMIT_MAX','5')))
        email=str((request.get_json(force=True) or {}).get('email','')).strip().lower()
    except RuntimeError as e:
        if str(e)=='RATE_LIMITED': return json_error('Too many verification requests. Please try again later.',429,'RATE_LIMITED')
        return json_error('A valid email address is required.',400,'VALIDATION_ERROR')
    except Exception:
        return json_error('A valid email address is required.',400,'VALIDATION_ERROR')
    if '@' not in email: return json_error('A valid email address is required.',422,'VALIDATION_ERROR')
    try: request_otp(email); return jsonify(ok=True,message='Verification code sent.')
    except RuntimeError as e:
        if str(e)=='OTP_COOLDOWN': return json_error('Please wait before requesting another code.',429,'OTP_COOLDOWN')
        return json_error('Unable to send verification code.',500,'OTP_SEND_FAILED')

@app.post('/api/auth/verify-otp')
def api_verify_otp():
    request_id = getattr(request, 'request_id', secrets.token_hex(12))
    try:
        b = request.get_json(silent=True) or {}
        email = str(b.get('email', '')).strip().lower()
        code = str(b.get('code', '')).strip()
        if not email or '@' not in email or not re.fullmatch(r'\d{6}', code):
            return json_error('Enter a valid email address and 6-digit verification code.', 422, 'VALIDATION_ERROR')

        verify_otp(email, code)
        return jsonify(ok=True, verified=True, message='Email verified successfully.', requestId=request_id), 200
    except RuntimeError as e:
        reason = str(e)
        if reason in {'OTP_EXPIRED', 'OTP_LOCKED', 'OTP_INVALID'}:
            return json_error('The verification code is invalid or expired.', 400, reason)
        if reason == 'DATABASE_UNAVAILABLE':
            return jsonify(ok=False, error={'code':'DATABASE_UNAVAILABLE','message':'Verification service is temporarily unavailable. Please try again.','requestId':request_id}), 503
        app.logger.exception('OTP verification runtime failure request=%s email=%s', request_id, email)
        return jsonify(ok=False, error={'code':'OTP_VERIFY_FAILED','message':'Unable to verify the code right now.','requestId':request_id}), 500
    except Exception as exc:
        app.logger.exception('OTP verification unexpected failure request=%s email=%s error=%r', request_id, email if 'email' in locals() else '', exc)
        return jsonify(ok=False, error={'code':'OTP_VERIFY_FAILED','message':'Unable to verify the code right now.','requestId':request_id}), 500

@app.post('/api/leads')
def api_leads():
    try:
        rate_limit('lead:'+client_ip(), maximum=int(os.getenv('LEAD_RATE_LIMIT_MAX','10'))); b=request.get_json(force=True) or {}
        required=['name','phone','email','service','county','town','description','consent']
        if any(not str(b.get(k,'')).strip() for k in required) or b.get('consent')!='yes' or '@' not in str(b.get('email','')): return json_error('Please check the enquiry form and correct the highlighted information.',422,'VALIDATION_ERROR')
        if len(str(b['description']).strip())<10: return json_error('Please check the enquiry form and correct the highlighted information.',422,'VALIDATION_ERROR')
        id='GLDC-LEAD-'+secrets.token_hex(5).upper(); t=now(); doc={**b,'id':id,'status':'NEW','emailVerified':False,'createdAt':t,'updatedAt':t}
        db.leads.insert_one(doc); db.notifications.insert_one({'type':'NEW ENQUIRY','message':f'New enquiry {id}','createdAt':t,'read':False}); db.audit.insert_one({'action':'LEAD_CREATED','entity':id,'createdAt':t}); request_otp(str(b['email']).lower())
        return jsonify(ok=True,id=id,verificationRequired=True),201
    except RuntimeError as e: return json_error(str(e),500)
    except Exception: return json_error('Please check the enquiry form and correct the highlighted information.',422,'VALIDATION_ERROR')

@app.post('/api/members/register')
def api_member_register():
    if not MEMBERSHIP_ENABLED: return json_error('Membership is currently disabled.', 503, 'MEMBERSHIP_DISABLED')
    b=request.get_json(force=True) or {}; name=str(b.get('name','')).strip(); email=str(b.get('email','')).strip().lower(); phone=str(b.get('phone','')).strip()
    if len(name)<2 or '@' not in email or len(phone)<7: return json_error('Invalid member information.',422,'VALIDATION_ERROR')
    try:
        id='GLDC-MEM-'+secrets.token_hex(5).upper(); db.members.update_one({'email':email},{'$set':{'name':name,'phone':phone,'email':email,'updatedAt':now()},'$setOnInsert':{'id':id,'status':'PENDING','emailVerified':False,'createdAt':now()}},upsert=True); request_otp(email); return jsonify(ok=True,message='Verification code sent to your email.')
    except Exception as e: return json_error(str(e),500)

@app.post('/api/members/verify')
def api_member_verify():
    b=request.get_json(force=True) or {}; email=str(b.get('email','')).strip().lower(); code=str(b.get('code','')).strip()
    try:
        verify_otp(email,code); m=db.members.find_one({'email':email})
        if not m: return json_error('Member account not found.',404)
        db.members.update_one({'_id':m['_id']},{'$set':{'emailVerified':True,'status':'ACTIVE' if m.get('status')=='PENDING' else m.get('status'),'updatedAt':now()}}); session['user']={'id':str(m['_id']),'email':email,'name':m.get('name',''),'role':'MEMBER'}; return jsonify(ok=True,user=session['user'])
    except RuntimeError as e:
        if str(e) in ['OTP_EXPIRED','OTP_LOCKED','OTP_INVALID']: return json_error('The verification code is invalid or expired.',400,str(e))
        return json_error(str(e),500)

@app.get('/api/drive/files')
@admin_required
def api_drive_files():
    try: return jsonify(ok=True,files=drive_files())
    except Exception as e: return json_error(str(e),500)

@app.get('/api/drive/read')
@admin_required
def api_drive_read():
    fid=request.args.get('id','').strip()
    if not fid: return json_error('File id is required.',400,'DRIVE_FILE_ID_REQUIRED')
    try:
        meta,text,data=drive_read(fid)
        if data is not None:
            # Do not use Flask send_file/conditional responses for Drive PDFs on Vercel.
            # Serverless adapters can mishandle range/conditional file responses, which
            # makes Chrome report "Failed to load PDF document" even when the bytes
            # downloaded from Google Drive are valid. Return the already-downloaded bytes
            # as a normal HTTP response with an explicit PDF content length.
            if not isinstance(data, (bytes, bytearray)):
                raise RuntimeError(f'DRIVE_PDF_INVALID_BYTES:{type(data).__name__}')
            pdf_bytes = bytes(data)
            if not pdf_bytes.startswith(b'%PDF-'):
                preview = pdf_bytes[:32].hex()
                raise RuntimeError(f'DRIVE_PDF_NOT_PDF:downloaded content does not start with PDF signature ({preview})')
            filename = meta.get('name') or 'document.pdf'
            if not filename.lower().endswith('.pdf'):
                filename += '.pdf'
            response = Response(pdf_bytes, status=200, mimetype='application/pdf')
            response.headers['Content-Type'] = 'application/pdf'
            response.headers['Content-Length'] = str(len(pdf_bytes))
            response.headers['Content-Disposition'] = f'inline; filename="{filename.replace(chr(34), "")}';
            response.headers['Accept-Ranges'] = 'bytes'
            response.headers['Cache-Control'] = 'private, no-store, max-age=0'
            response.headers['X-Drive-File-Id'] = meta.get('id','')
            response.headers['X-Drive-Mime-Type'] = meta.get('mimeType','')
            response.headers['X-Drive-Bytes'] = str(len(pdf_bytes))
            return response
        return jsonify(ok=True,meta=meta,text=text)
    except Exception as e:
        app.logger.exception('Google Drive read failed for file %s', fid)
        return json_error(str(e),500,'DRIVE_READ_FAILED')

@app.get('/api/drive/download')
@admin_required
def api_drive_download():
    """Download any supported Drive file; PDFs are downloaded too."""
    fid=request.args.get('id','').strip()
    if not fid: return json_error('File id is required.',400,'DRIVE_FILE_ID_REQUIRED')
    try:
        meta,filename,mimetype,data=drive_download(fid)
        if not isinstance(data,(bytes,bytearray)) or not data:
            raise RuntimeError(f'DRIVE_DOWNLOAD_INVALID_BYTES:{type(data).__name__}')
        safe_name=(filename or meta.get('name') or 'download').replace('\"','')
        response=Response(bytes(data),status=200,mimetype=mimetype or 'application/octet-stream')
        response.headers['Content-Type']=mimetype or 'application/octet-stream'
        response.headers['Content-Length']=str(len(data))
        response.headers['Content-Disposition']=f'attachment; filename=\"{safe_name}\"'
        response.headers['Cache-Control']='private, no-store, max-age=0'
        response.headers['X-Drive-File-Id']=meta.get('id','')
        response.headers['X-Drive-Mime-Type']=meta.get('mimeType','')
        response.headers['X-Drive-Bytes']=str(len(data))
        return response
    except Exception as e:
        app.logger.exception('Google Drive download failed for file %s', fid)
        return json_error(str(e),500,'DRIVE_DOWNLOAD_FAILED')

@app.get('/api/drive/debug')
@admin_required
def api_drive_debug():
    """Return safe metadata for diagnosing one Drive file without downloading it."""
    fid=request.args.get('id','').strip()
    if not fid: return json_error('File id is required.',400,'DRIVE_FILE_ID_REQUIRED')
    try:
        d=google_service('drive',['https://www.googleapis.com/auth/drive.readonly'])
        meta=d.files().get(fileId=fid,fields='id,name,mimeType,size,modifiedTime,webViewLink,webContentLink,shortcutDetails,capabilities',supportsAllDrives=True).execute()
        return jsonify(ok=True, file={
            'id':meta.get('id'),
            'name':meta.get('name'),
            'mimeType':meta.get('mimeType'),
            'size':meta.get('size'),
            'modifiedTime':meta.get('modifiedTime'),
            'webViewLink':meta.get('webViewLink'),
            'webContentLink':meta.get('webContentLink'),
            'shortcutDetails':meta.get('shortcutDetails'),
            'canDownload':(meta.get('capabilities') or {}).get('canDownload'),
        })
    except Exception as e:
        app.logger.exception('Google Drive debug failed for file %s', fid)
        return json_error(str(e),500,'DRIVE_DEBUG_FAILED')

@app.get('/api/drive/sheet')
@admin_required
def api_drive_sheet():
    if os.getenv('GOOGLE_SHEETS_ENABLED','false').lower()!='true' or not os.getenv('GOOGLE_SPREADSHEET_ID'): return json_error('Google Sheets is not configured.',503,'GOOGLE_SHEETS_DISABLED')
    try:
        d=google_service('drive',['https://www.googleapis.com/auth/drive.readonly']); fid=os.getenv('GOOGLE_SPREADSHEET_ID'); meta=d.files().get(fileId=fid,fields='id,name,mimeType,modifiedTime,webViewLink',supportsAllDrives=True).execute()
        if meta.get('mimeType')!='application/vnd.google-apps.spreadsheet': return json_error('Configured file is not a spreadsheet.',422)
        csv=d.files().export(fileId=fid,mimeType='text/csv').execute().decode('utf-8'); return jsonify(ok=True,meta=meta,csv=csv)
    except Exception as e: return json_error(str(e),500)

@app.get('/api/admin/leads')
@admin_required
def api_admin_leads():
    try:
        rows=list(db.leads.find({}, {'_id':0}).sort('createdAt', DESCENDING).limit(100))
        return jsonify(ok=True, leads=rows)
    except Exception as e: return json_error(str(e),500)

@app.get('/api/admin/stats')
@admin_required
def api_stats():
    try:
        vals={c:db[c].count_documents({}) for c in ['leads','payments','members','projects','users']}; return jsonify(ok=True,**vals)
    except Exception as e: return json_error(str(e),500)

@app.get('/api/admin/settings')
@admin_required
def api_settings():
    s=db.settings.find_one({'key':'public'}) or {}
    return jsonify(ok=True,settings={'currency':CURRENCY,'timezone':APP_TIMEZONE,'appUrl':APP_URL,'adminPath':ADMIN_PATH,'darajaEnvironment':str(env('DARAJA_ENV','DARAJA_ENVIRONMENT', default='production')),'smtpConfigured':bool(env('SMTP_HOST') and env('SMTP_USER','GMAIL_USER') and env('SMTP_PASSWORD','GMAIL_APP_PASSWORD') and env('SMTP_FROM','EMAIL_FROM_ADDRESS')),'googleDriveConfigured':env_bool('GOOGLE_DRIVE_ENABLED', default=True) and bool(env('GOOGLE_DRIVE_FOLDER_ID')),'googleDriveAuth':'service-account','googleSheetsConfigured':env_bool('GOOGLE_SHEETS_ENABLED', default=False) and bool(env('GOOGLE_SPREADSHEET_ID')),'mongodbConfigured':bool(MONGO_URI),'paymentsEnabled':PAYMENTS_ENABLED,'membershipEnabled':MEMBERSHIP_ENABLED,'documentStorage':DOCUMENT_STORAGE,'pdfEnabled':PDF_ENABLED,'public':{k:s.get(k,'') for k in ['company','phone','email','location','hours','tagline']}})

@app.post('/api/admin/settings')
@admin_required
def api_settings_save():
    b=request.get_json(force=True) or {}; allowed={k:str(b.get(k,'')).strip()[:200] for k in ['company','phone','email','location','hours','tagline']}; db.settings.update_one({'key':'public'},{'$set':{**allowed,'key':'public','updatedAt':now()}},upsert=True); return jsonify(ok=True)

@app.get('/api/admin/content')
@admin_required
def api_content(): return jsonify(ok=True,content=list(db.content.find({}, {'_id':0}).sort('key',ASCENDING)))
@app.post('/api/admin/content')
@admin_required
def api_content_save():
    b=request.get_json(force=True) or {}; key=str(b.get('key','')).strip(); value=str(b.get('value','')); 
    if not key or len(key)>160: return json_error('Invalid key',422,'VALIDATION_ERROR')
    db.content.update_one({'key':key},{'$set':{'key':key,'value':value,'updatedAt':now()}},upsert=True); return jsonify(ok=True)

@app.get('/api/admin/invoices')
@admin_required
def api_admin_invoices():
    try:
        return jsonify(ok=True, invoices=list(db.invoices.find({}, {'_id':0}).sort('createdAt', DESCENDING).limit(200)))
    except Exception:
        app.logger.exception('Invoice listing failed request=%s', request.request_id)
        return json_error('Unable to load invoices.',500,'INVOICE_LIST_FAILED')

@app.post('/api/admin/invoices')
@admin_required
def api_admin_create_invoice():
    try:
        b=request.get_json(silent=True) or {}; lead_id=str(b.get('leadId','')).strip()
        recipient_email=str(b.get('recipientEmail','')).strip().lower(); recipient_name=str(b.get('recipientName','')).strip()
        description=str(b.get('description','')).strip(); due_date=str(b.get('dueDate','')).strip()[:30]
        try: amount=float(b.get('amount',0))
        except (TypeError,ValueError): amount=0
        if lead_id:
            lead=db.leads.find_one({'id':lead_id},{'_id':0})
            if not lead: return json_error('Lead not found.',404,'LEAD_NOT_FOUND')
            recipient_email=str(lead.get('email') or recipient_email).strip().lower(); recipient_name=recipient_name or str(lead.get('name',''))
        if '@' not in recipient_email or amount<=0 or not description: return json_error('Recipient email, positive amount and description are required.',422,'VALIDATION_ERROR')
        invoice_number='GLDC-INV-'+datetime.now(timezone.utc).strftime('%Y%m%d')+'-'+secrets.token_hex(3).upper()
        inv={'invoiceNumber':invoice_number,'leadId':lead_id or None,'recipientEmail':recipient_email,'recipientName':recipient_name,'description':description,'amount':round(amount,2),'currency':'KES','dueDate':due_date,'status':'CREATED','issuedAt':now().isoformat(),'createdAt':now(),'createdBy':current_user().get('email'),'sentAt':None}
        db.invoices.insert_one(inv)
        send_now=bool(b.get('send',True))
        if send_now:
            pdf=build_invoice_pdf(inv); subject=f'GLDC Invoice {invoice_number}'
            text=f'Dear {recipient_name or recipient_email},\n\nPlease find attached your GLDC invoice {invoice_number} for KES {amount:,.2f}.\n\nDescription: {description}\nDue: {due_date or "On receipt"}\n\nRegards,\nGavin Land & Design Consultants'
            html=f'<p>Dear {recipient_name or recipient_email},</p><p>Please find attached your <strong>GLDC invoice {invoice_number}</strong> for <strong>KES {amount:,.2f}</strong>.</p><p><strong>Description:</strong> {description}<br><strong>Due:</strong> {due_date or "On receipt"}</p><p>Regards,<br>Gavin Land &amp; Design Consultants</p>'
            send_email(recipient_email,subject,text,html,[('GLDC-'+invoice_number+'.pdf',pdf,'application/pdf')])
            db.invoices.update_one({'invoiceNumber':invoice_number},{'$set':{'status':'SENT','sentAt':now()}})
        return jsonify(ok=True, invoice={'invoiceNumber':invoice_number,'recipientEmail':recipient_email,'recipientName':recipient_name,'amount':round(amount,2),'description':description,'dueDate':due_date,'status':'SENT' if send_now else 'CREATED'})
    except Exception:
        app.logger.exception('Invoice creation/send failed request=%s', request.request_id)
        return json_error('Unable to create or send invoice.',500,'INVOICE_CREATE_FAILED')

@app.get('/api/admin/invoices/<invoice_number>/download')
@admin_required
def api_admin_invoice_download(invoice_number):
    try:
        inv=db.invoices.find_one({'invoiceNumber':invoice_number},{'_id':0})
        if not inv: return json_error('Invoice not found.',404,'INVOICE_NOT_FOUND')
        pdf=build_invoice_pdf(inv); resp=Response(pdf,status=200,mimetype='application/pdf')
        resp.headers['Content-Disposition']=f'attachment; filename="{invoice_number}.pdf"'; resp.headers['Content-Length']=str(len(pdf)); resp.headers['Cache-Control']='private, no-store'
        return resp
    except Exception:
        app.logger.exception('Invoice download failed request=%s', request.request_id); return json_error('Unable to download invoice.',500,'INVOICE_DOWNLOAD_FAILED')

@app.post('/api/admin/invoices/<invoice_number>/resend')
@admin_required
def api_admin_invoice_resend(invoice_number):
    try:
        inv=db.invoices.find_one({'invoiceNumber':invoice_number},{'_id':0})
        if not inv: return json_error('Invoice not found.',404,'INVOICE_NOT_FOUND')
        pdf=build_invoice_pdf(inv); send_email(inv['recipientEmail'],f"GLDC Invoice {invoice_number}",f"Please find attached your GLDC invoice {invoice_number}.",f"<p>Please find attached your <strong>GLDC invoice {invoice_number}</strong>.</p>",[('GLDC-'+invoice_number+'.pdf',pdf,'application/pdf')])
        db.invoices.update_one({'invoiceNumber':invoice_number},{'$set':{'status':'SENT','sentAt':now()}})
        return jsonify(ok=True,message='Invoice sent.',invoiceNumber=invoice_number)
    except Exception:
        app.logger.exception('Invoice resend failed request=%s', request.request_id); return json_error('Unable to resend invoice.',500,'INVOICE_SEND_FAILED')

@app.post('/api/payments/stk')
@login_required
def api_stk():
    if not PAYMENTS_ENABLED: return json_error('Payments are currently disabled.', 503, 'PAYMENTS_DISABLED')
    b=request.get_json(force=True) or {}; phone=str(b.get('phone','')).strip(); amount=int(float(b.get('amount',0))); reference=str(b.get('reference','')).strip(); desc=str(b.get('description','')).strip(); lead_id=b.get('leadId')
    if len(phone)<7 or amount<=0 or amount>100000000 or len(reference)<2 or len(desc)<2: return json_error('Invalid payment request.',422,'VALIDATION_ERROR')
    u=current_user()
    if u['role']=='MEMBER':
        if not lead_id: return json_error('An approved lead is required for a member payment.')
        lead=db.leads.find_one({'id':lead_id,'email':u['email'],'status':'APPROVED'})
        if not lead: return json_error('This lead is not approved for payment.',403,'PAYMENT_NOT_ALLOWED')
    pid='GLDC-PAY-'+secrets.token_hex(5).upper(); db.payments.insert_one({'id':pid,'amount':amount,'phone':phone,'reference':reference,'description':desc,'status':'PENDING','currency':'KES','createdAt':now(),'updatedAt':now()})
    try:
        r=daraja_stk(phone,amount,reference,desc); db.payments.update_one({'id':pid},{'$set':{'merchantRequestId':r.get('MerchantRequestID'),'checkoutRequestId':r.get('CheckoutRequestID'),'responseDescription':r.get('ResponseDescription'),'updatedAt':now()}}); return jsonify(ok=True,paymentId=pid,status='PENDING',message=r.get('CustomerMessage',''))
    except Exception as e:
        db.payments.update_one({'id':pid},{'$set':{'status':'FAILED','error':'Daraja request failed','updatedAt':now()}}); return json_error(str(e),500)

@app.post('/api/payments/callback')
def api_callback():
    try:
        body=request.get_json(force=True) or {}; cb=body.get('Body',{}).get('stkCallback')
        if not cb: return jsonify(ResultCode=0,ResultDesc='Accepted')
        items=cb.get('CallbackMetadata',{}).get('Item',[]); vals={i.get('Name'):i.get('Value') for i in items}; checkout=cb.get('CheckoutRequestID'); p=db.payments.find_one({'checkoutRequestId':checkout,'status':'PENDING'})
        if not p: return jsonify(ResultCode=0,ResultDesc='Accepted')
        success=int(cb.get('ResultCode',1))==0; update={'status':'SUCCESSFUL' if success else 'FAILED','resultCode':cb.get('ResultCode'),'resultDescription':cb.get('ResultDesc'),'updatedAt':now()}
        if success: update.update({'mpesaReceiptNumber':vals.get('MpesaReceiptNumber'),'transactionDate':vals.get('TransactionDate'),'phoneNumber':vals.get('PhoneNumber'),'amount':vals.get('Amount')})
        db.payments.update_one({'_id':p['_id']},{'$set':update}); db.audit.insert_one({'action':'PAYMENT_SUCCESS' if success else 'PAYMENT_FAILED','entity':p['id'],'createdAt':now(),'result':cb.get('ResultDesc')})
        return jsonify(ResultCode=0,ResultDesc='Accepted')
    except Exception: return jsonify(ResultCode=0,ResultDesc='Accepted')

@app.post('/api/consultations')
def public_consultation():
    b=request.get_json(silent=True) or {}; name=str(b.get('name','')).strip(); email=str(b.get('email','')).strip().lower(); preferred=str(b.get('preferredDate','')).strip();
    if not name or '@' not in email: return json_error('Name and valid email are required.',422,'VALIDATION_ERROR')
    cid=make_id('CON'); doc={'id':cid,'name':name,'email':email,'phone':str(b.get('phone','')).strip(),'service':str(b.get('service','')).strip(),'preferredDate':preferred,'message':str(b.get('message','')).strip(),'status':'REQUESTED','createdAt':now()}
    db.consultations.insert_one(doc); db.leads.insert_one({'id':make_id('LED'),'name':name,'email':email,'phone':doc['phone'],'service':doc['service'],'message':doc['message'],'status':'NEW','source':'CONSULTATION','createdAt':now(),'updatedAt':now()}); audit('CONSULTATION_REQUESTED','consultation',cid); return jsonify(ok=True,id=cid,message='Consultation request received.')

@app.get('/api/admin/consultations')
@admin_required
def admin_consultations(): return jsonify(ok=True,consultations=list_collection('consultations'))

@app.get('/api/public/content')
def api_public_content():
    try:
        s=db.settings.find_one({'key':'public'}) if db is not None else None; content=list(db.content.find({'public':True},{'_id':0,'key':1,'value':1})) if db is not None else []
        return jsonify(ok=True,settings={k:s.get(k) for k in ['company','phone','email','location','hours','tagline']} if s else None,content=content)
    except Exception: return jsonify(ok=False,error={'code':'CONTENT_UNAVAILABLE','message':'Public content is temporarily unavailable.'}),503



@app.patch('/api/admin/leads/<lead_id>')
@admin_required
def admin_lead_update(lead_id):
    b=request.get_json(silent=True) or {}; allowed=['status','assignedTo','notes','followUpAt','priority','source']; update={k:b[k] for k in allowed if k in b}; update['updatedAt']=now(); db.leads.update_one({'id':lead_id},{'$set':update}); audit('LEAD_UPDATED','lead',lead_id,update); return jsonify(ok=True)

@app.post('/api/admin/leads/<lead_id>/convert')
@admin_required
def admin_lead_convert(lead_id):
    lead=db.leads.find_one({'id':lead_id},{'_id':0});
    if not lead: return json_error('Lead not found.',404,'LEAD_NOT_FOUND')
    existing=db.clients.find_one({'email':str(lead.get('email','')).lower()});
    if existing: cid=existing.get('id')
    else:
        cid=make_id('CLI'); db.clients.insert_one({'id':cid,'name':lead.get('name',''),'email':str(lead.get('email','')).lower(),'phone':lead.get('phone',''),'company':lead.get('company',''),'address':lead.get('town',''),'notes':lead.get('message',''),'status':'ACTIVE','sourceLeadId':lead_id,'createdAt':now(),'updatedAt':now(),'createdBy':current_user().get('email')})
    db.leads.update_one({'id':lead_id},{'$set':{'status':'CONVERTED','clientId':cid,'updatedAt':now()}}); audit('LEAD_CONVERTED','lead',lead_id,{'clientId':cid}); return jsonify(ok=True,clientId=cid)

@app.post('/api/admin/quotations/<quote_number>/decision')
@admin_required
def admin_quote_decision(quote_number):
    b=request.get_json(silent=True) or {}; status=str(b.get('status','')).upper();
    if status not in {'ACCEPTED','REJECTED','CHANGES REQUESTED'}: return json_error('Invalid quotation decision.',422,'VALIDATION_ERROR')
    q=db.quotations.find_one({'quoteNumber':quote_number});
    if not q: return json_error('Quotation not found.',404,'QUOTE_NOT_FOUND')
    db.quotations.update_one({'_id':q['_id']},{'$set':{'status':status,'decisionAt':now(),'decisionNote':str(b.get('note',''))},'$push':{'history':{'status':status,'at':now(),'by':current_user().get('email')}}}); audit('QUOTE_DECISION','quotation',quote_number,{'status':status}); return jsonify(ok=True,status=status)

# ================= GLDC FULL PLATFORM MODULES =================
# Connected CRM -> Client -> Project -> Tasks -> Quote -> Invoice -> Payment -> Documents workflow.
def audit(action, entity, entity_id=None, details=None):
    try:
        db.audit.insert_one({'action':action,'entity':entity,'entityId':entity_id,'details':details or {},'actor':(current_user() or {}).get('email'),'createdAt':now()})
    except Exception:
        app.logger.exception('Audit write failed')

def clean_doc(d):
    d=dict(d or {})
    d.pop('_id',None)
    for k,v in list(d.items()):
        if hasattr(v,'isoformat'): d[k]=v.isoformat()
        elif k=='_id': d[k]=str(v)
    return d

def make_id(prefix): return prefix+'-'+datetime.now(timezone.utc).strftime('%Y%m%d')+'-'+secrets.token_hex(4).upper()

def quote_pdf(q):
    buf=BytesIO(); c=canvas.Canvas(buf,pagesize=A4); w,h=A4
    c.setFont('Helvetica-Bold',20); c.drawString(25*mm,h-28*mm,'Gavin Land & Design Consultants')
    c.setFont('Helvetica',9); c.drawString(25*mm,h-35*mm,'LAND • DESIGN • DEVELOPMENT • CONSULTANCY')
    c.setFont('Helvetica-Bold',16); c.drawRightString(w-25*mm,h-28*mm,'QUOTATION')
    c.setFont('Helvetica',9); c.drawRightString(w-25*mm,h-35*mm,q.get('quoteNumber',''))
    y=h-58*mm; c.setFont('Helvetica-Bold',10); c.drawString(25*mm,y,'CLIENT')
    c.setFont('Helvetica',10); c.drawString(25*mm,y-7*mm,q.get('clientName','')); c.drawString(25*mm,y-14*mm,q.get('clientEmail',''))
    c.setFont('Helvetica-Bold',10); c.drawRightString(w-25*mm,y,'DATE'); c.setFont('Helvetica',10); c.drawRightString(w-25*mm,y-7*mm,str(q.get('createdAt',''))[:10])
    y-=35*mm; c.setFont('Helvetica-Bold',10); c.drawString(25*mm,y,'SCOPE / DESCRIPTION'); c.drawRightString(w-25*mm,y,'AMOUNT')
    c.line(25*mm,y-3*mm,w-25*mm,y-3*mm); c.setFont('Helvetica',10); c.drawString(25*mm,y-12*mm,str(q.get('description',''))[:100]); c.drawRightString(w-25*mm,y-12*mm,f"KES {float(q.get('amount',0)):,.2f}")
    c.line(25*mm,y-20*mm,w-25*mm,y-20*mm); c.setFont('Helvetica-Bold',12); c.drawRightString(w-25*mm,y-32*mm,f"TOTAL: KES {float(q.get('amount',0)):,.2f}")
    c.setFont('Helvetica',9); c.drawString(25*mm,25*mm,'This quotation is subject to GLDC approval and the terms stated in the proposal.')
    c.save(); return buf.getvalue()

def list_collection(name, limit=200): return [clean_doc(x) for x in db[name].find({}).sort('createdAt',DESCENDING).limit(limit)]

@app.get('/api/admin/clients')
@admin_required
def admin_clients(): return jsonify(ok=True,clients=list_collection('clients'))

@app.post('/api/admin/clients')
@admin_required
def admin_client_create():
    b=request.get_json(silent=True) or {}; email=str(b.get('email','')).strip().lower(); name=str(b.get('name','')).strip()
    if not name or '@' not in email: return json_error('Client name and valid email are required.',422,'VALIDATION_ERROR')
    cid=make_id('CLI'); doc={'id':cid,'name':name,'email':email,'phone':str(b.get('phone','')).strip(),'company':str(b.get('company','')).strip(),'address':str(b.get('address','')).strip(),'notes':str(b.get('notes','')).strip(),'status':'ACTIVE','createdAt':now(),'updatedAt':now(),'createdBy':current_user().get('email')}
    db.clients.insert_one(doc); audit('CLIENT_CREATED','client',cid); return jsonify(ok=True,client=clean_doc(doc)),201

@app.patch('/api/admin/clients/<client_id>')
@admin_required
def admin_client_update(client_id):
    b=request.get_json(silent=True) or {}; allowed=['name','email','phone','company','address','notes','status']
    update={k:b[k] for k in allowed if k in b}; update['updatedAt']=now(); db.clients.update_one({'id':client_id},{'$set':update}); audit('CLIENT_UPDATED','client',client_id,update); return jsonify(ok=True)

@app.get('/api/admin/projects')
@admin_required
def admin_projects(): return jsonify(ok=True,projects=list_collection('projects'))

@app.post('/api/admin/projects')
@admin_required
def admin_project_create():
    b=request.get_json(silent=True) or {}; name=str(b.get('name','')).strip(); client_id=str(b.get('clientId','')).strip()
    if not name or not client_id: return json_error('Project name and client are required.',422,'VALIDATION_ERROR')
    client=db.clients.find_one({'id':client_id},{'_id':0})
    if not client: return json_error('Client not found.',404,'CLIENT_NOT_FOUND')
    pid=make_id('PRJ'); doc={'id':pid,'name':name,'clientId':client_id,'clientName':client.get('name'),'location':str(b.get('location','')).strip(),'service':str(b.get('service','')).strip(),'description':str(b.get('description','')).strip(),'status':str(b.get('status','PLANNING')).upper(),'priority':str(b.get('priority','NORMAL')).upper(),'startDate':str(b.get('startDate','')).strip(),'dueDate':str(b.get('dueDate','')).strip(),'manager':str(b.get('manager','')).strip(),'budget':float(b.get('budget') or 0),'progress':int(b.get('progress') or 0),'createdAt':now(),'updatedAt':now(),'createdBy':current_user().get('email')}
    db.projects.insert_one(doc); audit('PROJECT_CREATED','project',pid); return jsonify(ok=True,project=clean_doc(doc)),201

@app.patch('/api/admin/projects/<project_id>')
@admin_required
def admin_project_update(project_id):
    b=request.get_json(silent=True) or {}; allowed=['name','location','service','description','status','priority','startDate','dueDate','manager','budget','progress','clientId']
    update={k:b[k] for k in allowed if k in b}; update['updatedAt']=now(); db.projects.update_one({'id':project_id},{'$set':update}); audit('PROJECT_UPDATED','project',project_id,update); return jsonify(ok=True)

@app.get('/api/admin/tasks')
@admin_required
def admin_tasks():
    project=request.args.get('projectId'); q={'projectId':project} if project else {}; return jsonify(ok=True,tasks=list_collection('tasks') if not project else [clean_doc(x) for x in db.tasks.find(q).sort('createdAt',DESCENDING).limit(500)])

@app.post('/api/admin/tasks')
@admin_required
def admin_task_create():
    b=request.get_json(silent=True) or {}; title=str(b.get('title','')).strip(); project_id=str(b.get('projectId','')).strip()
    if not title or not project_id: return json_error('Task title and project are required.',422,'VALIDATION_ERROR')
    tid=make_id('TSK'); doc={'id':tid,'title':title,'projectId':project_id,'assignee':str(b.get('assignee','')).strip(),'description':str(b.get('description','')).strip(),'status':str(b.get('status','TODO')).upper(),'priority':str(b.get('priority','NORMAL')).upper(),'dueDate':str(b.get('dueDate','')).strip(),'createdAt':now(),'updatedAt':now(),'createdBy':current_user().get('email')}
    db.tasks.insert_one(doc); audit('TASK_CREATED','task',tid); return jsonify(ok=True,task=clean_doc(doc)),201

@app.patch('/api/admin/tasks/<task_id>')
@admin_required
def admin_task_update(task_id):
    b=request.get_json(silent=True) or {}; allowed=['title','assignee','description','status','priority','dueDate']; update={k:b[k] for k in allowed if k in b}; update['updatedAt']=now(); db.tasks.update_one({'id':task_id},{'$set':update}); audit('TASK_UPDATED','task',task_id,update); return jsonify(ok=True)

@app.get('/api/admin/quotations')
@admin_required
def admin_quotes(): return jsonify(ok=True,quotations=list_collection('quotations'))

@app.post('/api/admin/quotations')
@admin_required
def admin_quote_create():
    b=request.get_json(silent=True) or {}; client_id=str(b.get('clientId','')).strip(); amount=float(b.get('amount') or 0); desc=str(b.get('description','')).strip()
    client=db.clients.find_one({'id':client_id},{'_id':0}) if client_id else None
    if not client or amount<=0 or not desc: return json_error('Client, positive amount and description are required.',422,'VALIDATION_ERROR')
    qn='GLDC-QT-'+datetime.now(timezone.utc).strftime('%Y%m%d')+'-'+secrets.token_hex(3).upper(); existing=db.quotations.count_documents({'clientId':client_id}); doc={'quoteNumber':qn,'version':existing+1,'clientId':client_id,'clientName':client.get('name'),'clientEmail':client.get('email'),'projectId':str(b.get('projectId','')).strip() or None,'description':desc,'amount':round(amount,2),'currency':CURRENCY,'status':'DRAFT','validUntil':str(b.get('validUntil','')).strip(),'createdAt':now(),'updatedAt':now(),'createdBy':current_user().get('email'),'history':[]}
    db.quotations.insert_one(doc); audit('QUOTE_CREATED','quotation',qn); return jsonify(ok=True,quotation=clean_doc(doc)),201

@app.post('/api/admin/quotations/<quote_number>/issue')
@admin_required
def admin_quote_issue(quote_number):
    q=db.quotations.find_one({'quoteNumber':quote_number});
    if not q: return json_error('Quotation not found.',404,'QUOTE_NOT_FOUND')
    db.quotations.update_one({'_id':q['_id']},{'$set':{'status':'ISSUED','issuedAt':now()},'$push':{'history':{'status':'ISSUED','at':now(),'by':current_user().get('email')}}}); audit('QUOTE_ISSUED','quotation',quote_number); return jsonify(ok=True,status='ISSUED')

@app.get('/api/admin/quotations/<quote_number>/download')
@admin_required
def admin_quote_download(quote_number):
    q=db.quotations.find_one({'quoteNumber':quote_number},{'_id':0});
    if not q: return json_error('Quotation not found.',404,'QUOTE_NOT_FOUND')
    data=quote_pdf(q); r=Response(data,mimetype='application/pdf'); r.headers['Content-Disposition']=f'attachment; filename="{quote_number}.pdf"'; r.headers['Content-Length']=str(len(data)); r.headers['Cache-Control']='private, no-store'; return r

@app.post('/api/admin/quotations/<quote_number>/send')
@admin_required
def admin_quote_send(quote_number):
    q=db.quotations.find_one({'quoteNumber':quote_number},{'_id':0});
    if not q: return json_error('Quotation not found.',404,'QUOTE_NOT_FOUND')
    data=quote_pdf(q); send_email(q['clientEmail'],f'GLDC Quotation {quote_number}',f'Please find attached quotation {quote_number}.',f'<p>Please find attached your GLDC quotation <strong>{quote_number}</strong>.</p>',[(quote_number+'.pdf',data,'application/pdf')]); db.quotations.update_one({'quoteNumber':quote_number},{'$set':{'status':'SENT','sentAt':now()},'$push':{'history':{'status':'SENT','at':now(),'by':current_user().get('email')}}}); audit('QUOTE_SENT','quotation',quote_number); return jsonify(ok=True)

@app.get('/api/admin/payments')
@admin_required
def admin_payments(): return jsonify(ok=True,payments=list_collection('payments'))

@app.post('/api/admin/payments/manual')
@admin_required
def admin_manual_payment():
    b=request.get_json(silent=True) or {}; amount=float(b.get('amount') or 0); invoice=str(b.get('invoiceNumber','')).strip(); ref=str(b.get('reference','')).strip();
    if amount<=0 or len(ref)<3: return json_error('Amount and reference are required.',422,'VALIDATION_ERROR')
    pid=make_id('PAY'); doc={'id':pid,'invoiceNumber':invoice or None,'clientId':str(b.get('clientId','')).strip() or None,'projectId':str(b.get('projectId','')).strip() or None,'amount':round(amount,2),'currency':CURRENCY,'method':str(b.get('method','BANK_TRANSFER')).upper(),'reference':ref,'status':'MANUALLY VERIFIED','source':'MANUAL','recordedBy':current_user().get('email'),'createdAt':now(),'updatedAt':now(),'notes':str(b.get('notes','')).strip()}
    db.payments.insert_one(doc); audit('PAYMENT_MANUALLY_VERIFIED','payment',pid,{'amount':amount,'reference':ref}); return jsonify(ok=True,payment=clean_doc(doc)),201

@app.post('/api/admin/invoices/<invoice_number>/status')
@admin_required
def admin_invoice_status(invoice_number):
    b=request.get_json(silent=True) or {}; status=str(b.get('status','')).upper(); allowed={'DRAFT','ISSUED','PARTIALLY PAID','PAID','OVERDUE','VOID','CANCELLED','SENT'}
    if status not in allowed: return json_error('Invalid invoice status.',422,'VALIDATION_ERROR')
    inv=db.invoices.find_one({'invoiceNumber':invoice_number});
    if not inv: return json_error('Invoice not found.',404,'INVOICE_NOT_FOUND')
    db.invoices.update_one({'_id':inv['_id']},{'$set':{'status':status,'updatedAt':now()},'$push':{'history':{'status':status,'at':now(),'by':current_user().get('email')}}}); audit('INVOICE_STATUS_CHANGED','invoice',invoice_number,{'status':status}); return jsonify(ok=True,status=status)

@app.get('/api/admin/documents')
@admin_required
def admin_documents(): return jsonify(ok=True,documents=list_collection('documents'))

@app.post('/api/admin/documents')
@admin_required
def admin_document_create():
    b=request.get_json(silent=True) or {}; name=str(b.get('name','')).strip();
    if not name: return json_error('Document name is required.',422,'VALIDATION_ERROR')
    did=make_id('DOC'); doc={'id':did,'name':name,'category':str(b.get('category','OTHER')).upper(),'projectId':str(b.get('projectId','')).strip() or None,'clientId':str(b.get('clientId','')).strip() or None,'driveFileId':str(b.get('driveFileId','')).strip() or None,'version':int(b.get('version') or 1),'fileType':str(b.get('fileType','')).strip(),'fileSize':int(b.get('fileSize') or 0),'accessLevel':str(b.get('accessLevel','PRIVATE')).upper(),'description':str(b.get('description','')).strip(),'status':'ACTIVE','uploadedBy':current_user().get('email'),'createdAt':now(),'updatedAt':now()}
    db.documents.insert_one(doc); audit('DOCUMENT_CREATED','document',did); return jsonify(ok=True,document=clean_doc(doc)),201

@app.patch('/api/admin/documents/<doc_id>')
@admin_required
def admin_document_update(doc_id):
    b=request.get_json(silent=True) or {}; allowed=['category','projectId','clientId','version','accessLevel','description','status']; update={k:b[k] for k in allowed if k in b}; update['updatedAt']=now(); db.documents.update_one({'id':doc_id},{'$set':update}); audit('DOCUMENT_UPDATED','document',doc_id,update); return jsonify(ok=True)

@app.get('/api/admin/site/<collection>')
@admin_required
def admin_site_list(collection):
    if collection not in {'services','site_projects','team','testimonials','posts','service_areas','faqs','pages'}: return json_error('Unsupported CMS collection.',404,'CMS_COLLECTION_NOT_FOUND')
    return jsonify(ok=True,items=list_collection(collection,500))

@app.post('/api/admin/site/<collection>')
@admin_required
def admin_site_create(collection):
    if collection not in {'services','site_projects','team','testimonials','posts','service_areas','faqs','pages'}: return json_error('Unsupported CMS collection.',404,'CMS_COLLECTION_NOT_FOUND')
    b=request.get_json(silent=True) or {}; title=str(b.get('title') or b.get('name') or '').strip()
    if not title: return json_error('Title/name is required.',422,'VALIDATION_ERROR')
    sid=make_id(collection[:3].upper()); doc=dict(b); doc.update({'id':sid,'title':title,'status':str(b.get('status','DRAFT')).upper(),'createdAt':now(),'updatedAt':now(),'createdBy':current_user().get('email')}); db[collection].insert_one(doc); audit('CMS_CREATED',collection,sid); return jsonify(ok=True,item=clean_doc(doc)),201

@app.patch('/api/admin/site/<collection>/<item_id>')
@admin_required
def admin_site_update(collection,item_id):
    if collection not in {'services','site_projects','team','testimonials','posts','service_areas','faqs','pages'}: return json_error('Unsupported CMS collection.',404,'CMS_COLLECTION_NOT_FOUND')
    b=request.get_json(silent=True) or {}; b['updatedAt']=now(); db[collection].update_one({'id':item_id},{'$set':b}); audit('CMS_UPDATED',collection,item_id,b); return jsonify(ok=True)

@app.get('/api/admin/notifications')
@admin_required
def admin_notifications(): return jsonify(ok=True,notifications=list_collection('notifications'))

@app.post('/api/admin/notifications')
@admin_required
def admin_notification_create():
    b=request.get_json(silent=True) or {}; title=str(b.get('title','')).strip(); message=str(b.get('message','')).strip();
    if not title or not message: return json_error('Title and message are required.',422,'VALIDATION_ERROR')
    nid=make_id('NTF'); doc={'id':nid,'title':title,'message':message,'type':str(b.get('type','INFO')).upper(),'audience':str(b.get('audience','ADMIN')).upper(),'read':False,'createdAt':now(),'createdBy':current_user().get('email')}; db.notifications.insert_one(doc); audit('NOTIFICATION_CREATED','notification',nid); return jsonify(ok=True,notification=clean_doc(doc)),201

@app.get('/api/admin/audit')
@admin_required
def admin_audit(): return jsonify(ok=True,audit=list_collection('audit',500))

@app.get('/api/admin/users')
@admin_required
def admin_users(): return jsonify(ok=True,users=[clean_doc(x) for x in db.users.find({}, {'passwordHash':0}).sort('createdAt',DESCENDING).limit(200)])

@app.post('/api/admin/users')
@admin_required
def admin_user_create():
    b=request.get_json(silent=True) or {}; email=str(b.get('email','')).strip().lower(); name=str(b.get('name','')).strip(); password=str(b.get('password','')); role=str(b.get('role','STAFF')).upper()
    if '@' not in email or not name or len(password)<12: return json_error('Name, valid email and 12+ character password are required.',422,'VALIDATION_ERROR')
    if db.users.find_one({'email':email}): return json_error('User already exists.',409,'USER_EXISTS')
    uid=make_id('USR'); doc={'id':uid,'email':email,'name':name,'passwordHash':hash_password(password),'role':role,'status':'ACTIVE','createdAt':now(),'updatedAt':now()}; db.users.insert_one(doc); audit('USER_CREATED','user',uid,{'email':email,'role':role}); doc.pop('passwordHash',None); return jsonify(ok=True,user=clean_doc(doc)),201

@app.get('/api/admin/reports')
@admin_required
def admin_reports():
    def total(col, query={}): return db[col].count_documents(query)
    paid=db.payments.aggregate([{'$match':{'status':{'$in':['SUCCESSFUL','MANUALLY VERIFIED']}}},{'$group':{'_id':None,'total':{'$sum':'$amount'}}}]); paid=list(paid); revenue=float(paid[0]['total']) if paid else 0
    outstanding=sum(float(x.get('amount',0)) for x in db.invoices.find({'status':{'$in':['ISSUED','SENT','PARTIALLY PAID','OVERDUE']}}))
    return jsonify(ok=True,metrics={'leads':total('leads'),'clients':total('clients'),'projects':total('projects'),'tasks':total('tasks'),'quotations':total('quotations'),'invoices':total('invoices'),'documents':total('documents'),'payments':total('payments'),'revenue':revenue,'outstanding':outstanding})

@app.get('/api/admin/calendar')
@admin_required
def admin_calendar():
    tasks=[clean_doc(x) for x in db.tasks.find({'dueDate':{'$ne':''}}).limit(500)]; return jsonify(ok=True,events=[{'id':x['id'],'title':x['title'],'date':x.get('dueDate'),'type':'TASK','status':x.get('status')} for x in tasks])


@app.errorhandler(400)
def bad_request(e):
    return json_error('Bad request.', 400, 'BAD_REQUEST') if request.path.startswith('/api/') else render_template('404.html', title='Bad request'), 400

@app.errorhandler(403)
def forbidden(e):
    return json_error('Forbidden.', 403, 'FORBIDDEN') if request.path.startswith('/api/') else render_template('404.html', title='Forbidden'), 403

@app.errorhandler(404)
def not_found(e): return render_template('404.html',title='Page not found'),404

@app.errorhandler(413)
def too_large(e): return json_error('Request is too large.', 413, 'PAYLOAD_TOO_LARGE') if request.path.startswith('/api/') else render_template('404.html', title='Request too large'), 413

@app.errorhandler(429)
def too_many(e): return json_error('Too many requests. Please try again later.', 429, 'RATE_LIMITED')

@app.errorhandler(Exception)
def unhandled(e):
    app.logger.exception('Unhandled application error')
    return json_error('An internal server error occurred.', 500, 'INTERNAL_ERROR') if request.path.startswith('/api/') else render_template('404.html', title='Server error'), 500

def init_database():
    if db is None: return
    db.command('ping')
    db.leads.create_index([('email',ASCENDING),('createdAt',DESCENDING)])
    db.leads.create_index([('status',ASCENDING),('createdAt',DESCENDING)])
    db.otps.create_index([('expiresAt',ASCENDING)], expireAfterSeconds=0)
    db.otps.create_index([('email',ASCENDING),('createdAt',DESCENDING)])
    db.members.create_index([('email',ASCENDING)], unique=True)
    db.users.create_index([('email',ASCENDING)], unique=True)
    db.payments.create_index([('checkoutRequestId',ASCENDING)], unique=True, sparse=True)
    db.invoices.create_index([('invoiceNumber',ASCENDING)], unique=True)
    db.invoices.create_index([('recipientEmail',ASCENDING),('createdAt',DESCENDING)])
    db.payments.create_index([('status',ASCENDING),('createdAt',DESCENDING)])
    db.content.create_index([('key',ASCENDING)], unique=True)
    db.settings.create_index([('key',ASCENDING)], unique=True)
    db.rate_limits.create_index([('createdAt',ASCENDING)], expireAfterSeconds=7200)
    db.audit.create_index([('createdAt',DESCENDING)])
    for collection in ['clients','projects','tasks','quotations','documents','notifications']:
        db[collection].create_index([('createdAt',DESCENDING)])
    db.quotations.create_index([('quoteNumber',ASCENDING)], unique=True)
    db.documents.create_index([('projectId',ASCENDING),('category',ASCENDING)])
    db.projects.create_index([('clientId',ASCENDING),('status',ASCENDING)])
    db.posts.create_index([('status',ASCENDING),('publishedAt',DESCENDING)])
    db.consultations.create_index([('scheduledAt',ASCENDING),('status',ASCENDING)])
    defaults={'home.heroTitle':'Land. Design. Development. Done Right.','home.heroText':'Professional land, planning, design and development consultancy for clients who need practical outcomes.','home.primaryCta':'REQUEST A QUOTE','home.secondaryCta':'VIEW PROJECTS','site.company':'Gavin Land & Design Consultants','site.tagline':'Land • Design • Development • Consultancy','site.phone':'+254','site.email':'info@yourdomain.co.ke','site.location':'Kenya'}
    for key,value in defaults.items(): db.content.update_one({'key':key},{'$setOnInsert':{'key':key,'value':value,'public':True,'createdAt':now()}},upsert=True)
    db.tasks.create_index([('projectId',ASCENDING),('status',ASCENDING)])
    for collection in ['services','site_projects','team','testimonials','posts','service_areas','faqs','pages']:
        db[collection].create_index([('status',ASCENDING),('updatedAt',DESCENDING)])

_database_initialized = False
_database_init_error = None

def ensure_database_initialized():
    global _database_initialized, _database_init_error
    if _database_initialized:
        return True
    if CONFIG_MISSING:
        return False
    if db is None:
        _database_init_error = 'MONGODB_URI is not configured.'
        return False
    try:
        init_database()
        bootstrap_admin()
        if APP_ENV == 'production' and db.users.count_documents({}) == 0 and os.getenv('REQUIRE_INITIAL_ADMIN','false').lower() == 'true':
            raise RuntimeError('No administrator account exists. Set INITIAL_ADMIN_EMAIL and INITIAL_ADMIN_PASSWORD.')
        _database_initialized = True
        _database_init_error = None
        return True
    except Exception as startup_error:
        _database_init_error = str(startup_error)
        app.logger.error('Database initialization failed: %s', startup_error)
        return False

@app.before_request
def lazy_database_initialization():
    # Vercel imports the module during function initialization. Do not connect to MongoDB
    # or fail deployment at import time; initialize lazily on the first request instead.
    if request.path.startswith('/api/') and request.path not in {'/api/health', '/api/ready', '/api/payments/callback'}:
        if not ensure_database_initialized():
            return json_error('Service configuration or database is temporarily unavailable.', 503, 'SERVICE_UNAVAILABLE')

if __name__=='__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT','5000')), debug=False)
