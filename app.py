import os, re, json, base64, hashlib, secrets, smtplib
from datetime import datetime, timedelta, timezone
from functools import wraps
from email.message import EmailMessage

import requests
import bcrypt
import jwt
from dotenv import load_dotenv
from flask import Flask, render_template, request, jsonify, redirect, url_for, session, flash, send_file, abort, Response
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument

load_dotenv()

app = Flask(__name__)
APP_ENV = os.getenv('APP_ENV', 'production').lower()
AUTH_SECRET = os.getenv('AUTH_SECRET', '')
# Never crash a serverless function during module import because environment variables are missing.
# Production readiness is reported by /api/ready, while configured deployments use the real secret.
app.secret_key = AUTH_SECRET or secrets.token_hex(32)
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = os.getenv('COOKIE_SECURE', 'true' if APP_ENV == 'production' else 'false').lower() == 'true'
app.config['SESSION_COOKIE_NAME'] = os.getenv('SESSION_COOKIE_NAME', 'gldc_session')
app.config['SESSION_COOKIE_PATH'] = '/'
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=int(os.getenv('SESSION_LIFETIME_HOURS','8')))
app.config['MAX_CONTENT_LENGTH'] = int(os.getenv('MAX_CONTENT_LENGTH_MB','2')) * 1024 * 1024

APP_TIMEZONE = os.getenv('APP_TIMEZONE', 'Africa/Nairobi')
CURRENCY = os.getenv('CURRENCY', 'KES')
MONGO_URI = os.getenv('MONGODB_URI', '')
MONGO_DB = os.getenv('MONGODB_DB_NAME', 'gldc')

mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=10000, maxPoolSize=20) if MONGO_URI else None
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
    email=os.getenv('INITIAL_ADMIN_EMAIL','').strip().lower(); password=os.getenv('INITIAL_ADMIN_PASSWORD','')
    if not email or not password: return
    if len(password) < 12: raise RuntimeError('INITIAL_ADMIN_PASSWORD must be at least 12 characters.')
    if db.users.count_documents({'role':'SUPER ADMIN / OWNER'}) == 0:
        db.users.update_one({'email':email},{'$setOnInsert':{'email':email,'name':'GLDC Administrator','passwordHash':hash_password(password),'role':'SUPER ADMIN / OWNER','status':'ACTIVE','createdAt':now(),'updatedAt':now()}},upsert=True)


def send_email(to, subject, text, html=None):
    host=os.getenv('SMTP_HOST'); user=os.getenv('SMTP_USER'); password=os.getenv('SMTP_PASSWORD')
    if not host: raise RuntimeError('SMTP_NOT_CONFIGURED')
    msg=EmailMessage(); msg['From']=os.getenv('SMTP_FROM', user); msg['To']=to; msg['Subject']=subject
    if os.getenv('SMTP_REPLY_TO'): msg['Reply-To']=os.getenv('SMTP_REPLY_TO')
    msg.set_content(text)
    if html: msg.add_alternative(html, subtype='html')
    port=int(os.getenv('SMTP_PORT','587')); secure=os.getenv('SMTP_SECURE','false').lower()=='true'
    if secure:
        with smtplib.SMTP_SSL(host,port) as s: s.login(user,password); s.send_message(msg)
    else:
        with smtplib.SMTP(host,port) as s:
            s.ehlo(); s.starttls(); s.ehlo(); s.login(user,password); s.send_message(msg)


def request_otp(email):
    if db is None: raise RuntimeError('DATABASE_UNAVAILABLE')
    email=email.lower().strip(); cool=int(os.getenv('OTP_RESEND_COOLDOWN_SECONDS','60')); expiry=int(os.getenv('OTP_EXPIRY_MINUTES','10'))
    if db.otps.find_one({'email':email,'createdAt':{'$gt':now()-timedelta(seconds=cool)}}): raise RuntimeError('OTP_COOLDOWN')
    code=f'{secrets.randbelow(900000)+100000}'
    db.otps.insert_one({'email':email,'codeHash':hashlib.sha256(code.encode()).hexdigest(),'createdAt':now(),'expiresAt':now()+timedelta(minutes=expiry),'attempts':0})
    send_email(email,'GLDC verification code',f'Your GLDC verification code is {code}. It expires in {expiry} minutes.',f'<p>Your GLDC verification code is <strong>{code}</strong>.</p><p>It expires in {expiry} minutes.</p>')


def verify_otp(email, code):
    email=email.lower().strip(); x=db.otps.find_one({'email':email}, sort=[('createdAt',DESCENDING)])
    max_attempts=int(os.getenv('OTP_MAX_ATTEMPTS','5'))
    if not x or x['expiresAt'] < now(): raise RuntimeError('OTP_EXPIRED')
    if x.get('attempts',0)>=max_attempts: raise RuntimeError('OTP_LOCKED')
    if x['codeHash'] != hashlib.sha256(code.strip().encode()).hexdigest():
        db.otps.update_one({'_id':x['_id']},{'$inc':{'attempts':1}}); raise RuntimeError('OTP_INVALID')
    db.otps.delete_many({'email':email}); return True


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


def drive_files():
    if os.getenv('GOOGLE_DRIVE_ENABLED','true').lower()!='true': return []
    d=google_service('drive',['https://www.googleapis.com/auth/drive.readonly'])
    folder=os.getenv('GOOGLE_DRIVE_FOLDER_ID','')
    if not folder: raise RuntimeError('GOOGLE_DRIVE_FOLDER_NOT_CONFIGURED')
    r=d.files().list(q=f"'{folder}' in parents and trashed=false",fields='files(id,name,mimeType,size,modifiedTime,webViewLink,webContentLink)',orderBy='modifiedTime desc',pageSize=100,supportsAllDrives=True,includeItemsFromAllDrives=True).execute()
    return r.get('files',[])


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


def drive_read(file_id):
    d=google_service('drive',['https://www.googleapis.com/auth/drive.readonly'])
    fields='id,name,mimeType,size,modifiedTime,webViewLink,webContentLink,shortcutDetails'
    meta=d.files().get(fileId=file_id,fields=fields,supportsAllDrives=True).execute()

    # Resolve Drive shortcuts before attempting the media request.
    shortcut=meta.get('shortcutDetails') or {}
    if meta.get('mimeType') == 'application/vnd.google-apps.shortcut' and shortcut.get('targetId'):
        file_id=shortcut['targetId']
        meta=d.files().get(fileId=file_id,fields=fields,supportsAllDrives=True).execute()

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
    return meta,'This file type is available in Drive but is not directly extractable by the server.',None

def daraja_token():
    base='https://api.safaricom.co.ke' if os.getenv('DARAJA_ENV','production')=='production' else 'https://sandbox.safaricom.co.ke'
    raw=f"{os.getenv('DARAJA_CONSUMER_KEY','')}:{os.getenv('DARAJA_CONSUMER_SECRET','')}".encode()
    auth=base64.b64encode(raw).decode()
    r=requests.get(base+'/oauth/v1/generate?grant_type=client_credentials',headers={'Authorization':'Basic '+auth},timeout=20); r.raise_for_status(); return base,r.json()['access_token']

def daraja_stk(phone,amount,reference,description):
    base,access=daraja_token(); ts=datetime.now().strftime('%Y%m%d%H%M%S'); short=os.getenv('DARAJA_SHORTCODE'); passkey=os.getenv('DARAJA_PASSKEY')
    password=base64.b64encode(f'{short}{passkey}{ts}'.encode()).decode()
    body={'BusinessShortCode':short,'Password':password,'Timestamp':ts,'TransactionType':os.getenv('DARAJA_TRANSACTION_TYPE','CustomerBuyGoodsOnline'),'Amount':round(amount),'PartyA':phone,'PartyB':os.getenv('DARAJA_TILL_NUMBER'),'PhoneNumber':phone,'CallBackURL':os.getenv('DARAJA_CALLBACK_URL'),'AccountReference':reference,'TransactionDesc':description}
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
    if APP_ENV != 'production': return []
    required = ['MONGODB_URI','MONGODB_DB_NAME','AUTH_SECRET','SMTP_HOST','SMTP_USER','SMTP_PASSWORD','SMTP_FROM']
    missing = [k for k in required if not os.getenv(k)]
    if os.getenv('INITIAL_ADMIN_EMAIL') and not os.getenv('INITIAL_ADMIN_PASSWORD'):
        missing.append('INITIAL_ADMIN_PASSWORD')
    if os.getenv('DARAJA_ENABLED','true').lower() == 'true':
        missing += [k for k in ['DARAJA_CONSUMER_KEY','DARAJA_CONSUMER_SECRET','DARAJA_SHORTCODE','DARAJA_TILL_NUMBER','DARAJA_PASSKEY','DARAJA_CALLBACK_URL'] if not os.getenv(k)]
    if os.getenv('GOOGLE_DRIVE_ENABLED','true').lower() == 'true':
        if not os.getenv('GOOGLE_DRIVE_FOLDER_ID'): missing.append('GOOGLE_DRIVE_FOLDER_ID')
        if not os.getenv('GOOGLE_SERVICE_ACCOUNT_JSON') and not (os.getenv('GOOGLE_SERVICE_ACCOUNT_EMAIL') and os.getenv('GOOGLE_PRIVATE_KEY')):
            missing.append('GOOGLE_SERVICE_ACCOUNT_EMAIL/GOOGLE_PRIVATE_KEY or GOOGLE_SERVICE_ACCOUNT_JSON')
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
def globals_(): return {'current_user':current_user(),'currency':CURRENCY,'year':datetime.now().year}

@app.get('/api/health')
def api_health():
    # Liveness + real MongoDB connectivity check for the admin dashboard.
    # This endpoint is intentionally exempt from lazy DB initialization so it
    # can diagnose configuration/connectivity without causing an import crash.
    started = time.perf_counter()
    if db is None:
        return jsonify(ok=False, service='gldc', environment=APP_ENV, status='DEGRADED',
                       database='NOT_CONFIGURED', latencyMs=round((time.perf_counter()-started)*1000, 2),
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
    return jsonify(ok=True, ready=True, database=True), 200

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
@app.route('/privacy')
def privacy(): return render_template('privacy.html', title='Privacy Policy')
@app.route('/terms')
def terms(): return render_template('terms.html', title='Terms & Conditions')

@app.route('/admin')
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
    b=request.get_json(force=True) or {}; email=str(b.get('email','')).strip().lower(); code=str(b.get('code','')).strip()
    try: verify_otp(email,code); return jsonify(ok=True,verified=True)
    except RuntimeError as e:
        if str(e) in ['OTP_EXPIRED','OTP_LOCKED','OTP_INVALID']: return json_error('The verification code is invalid or expired.',400,str(e))
        return json_error(str(e),500)

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
    return jsonify(ok=True,settings={'currency':CURRENCY,'timezone':APP_TIMEZONE,'darajaEnvironment':os.getenv('DARAJA_ENV','production'),'smtpConfigured':bool(os.getenv('SMTP_HOST')),'googleDriveConfigured':os.getenv('GOOGLE_DRIVE_ENABLED','true').lower()=='true' and bool(os.getenv('GOOGLE_DRIVE_FOLDER_ID')),'googleDriveAuth':'service-account','googleSheetsConfigured':os.getenv('GOOGLE_SHEETS_ENABLED','false').lower()=='true' and bool(os.getenv('GOOGLE_SPREADSHEET_ID')),'mongodbConfigured':bool(MONGO_URI),'public':{k:s.get(k,'') for k in ['company','phone','email','location','hours','tagline']}})

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

@app.post('/api/payments/stk')
@login_required
def api_stk():
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

@app.get('/api/public/content')
def api_public_content():
    try:
        s=db.settings.find_one({'key':'public'}) if db is not None else None; content=list(db.content.find({'public':True},{'_id':0,'key':1,'value':1})) if db is not None else []
        return jsonify(ok=True,settings={k:s.get(k) for k in ['company','phone','email','location','hours','tagline']} if s else None,content=content)
    except Exception: return jsonify(ok=False,error={'code':'CONTENT_UNAVAILABLE','message':'Public content is temporarily unavailable.'}),503

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
    db.payments.create_index([('status',ASCENDING),('createdAt',DESCENDING)])
    db.content.create_index([('key',ASCENDING)], unique=True)
    db.settings.create_index([('key',ASCENDING)], unique=True)
    db.rate_limits.create_index([('createdAt',ASCENDING)], expireAfterSeconds=7200)
    db.audit.create_index([('createdAt',DESCENDING)])

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
