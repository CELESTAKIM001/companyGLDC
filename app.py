import os, re, json, base64, hashlib, secrets, smtplib, time
from datetime import datetime, timedelta, timezone, date
from functools import wraps
from email.message import EmailMessage
from io import BytesIO
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas
from reportlab.graphics.barcode import qr
from reportlab.graphics.shapes import Drawing
from reportlab.graphics import renderPDF
from reportlab.lib.utils import ImageReader

import requests
import bcrypt
import jwt
from dotenv import load_dotenv
from flask import Flask, render_template, request, jsonify, redirect, url_for, session, flash, send_file, abort, Response
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError

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
# How long a member can sit unverified / unpaid before we treat the application as abandoned
# and alert GLDC staff so they can follow up. Configurable per-deployment via env vars.
MEMBERSHIP_ABANDON_EMAIL_HOURS = float(env('MEMBERSHIP_ABANDON_EMAIL_HOURS', default='24'))
MEMBERSHIP_ABANDON_PAYMENT_HOURS = float(env('MEMBERSHIP_ABANDON_PAYMENT_HOURS', default='48'))
MEMBERSHIP_ABANDON_RENOTIFY_HOURS = float(env('MEMBERSHIP_ABANDON_RENOTIFY_HOURS', default='72'))
CRON_SECRET = str(env('CRON_SECRET', default=''))
DOCUMENT_STORAGE = str(env('DOCUMENT_STORAGE', default='google_drive'))
PDF_ENABLED = env_bool('PDF_ENABLED', default=True)
PDF_QR_ENABLED = env_bool('PDF_QR_ENABLED', default=False)
SIGNATURE_ENABLED = env_bool('SIGNATURE_ENABLED', default=False)
MONGO_URI = os.getenv('MONGODB_URI', '')
MONGO_DB = os.getenv('MONGODB_DB_NAME', 'gldc')

mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=10000, maxPoolSize=20, tz_aware=True) if MONGO_URI else None
db = mongo_client[MONGO_DB] if mongo_client is not None else None


def now(): return datetime.now(timezone.utc)

def add_calendar_months(value, months):
    if isinstance(value, datetime): base_date=value.date()
    else: base_date=value
    total=base_date.year*12 + base_date.month-1 + int(months)
    year, month0=divmod(total,12); month=month0+1
    import calendar
    return datetime(year,month,min(base_date.day,calendar.monthrange(year,month)[1]),tzinfo=timezone.utc)

def membership_period(valid_from, months):
    start=valid_from if isinstance(valid_from,datetime) else datetime.combine(valid_from,datetime.min.time(),tzinfo=timezone.utc)
    return start, add_calendar_months(start,int(months))-timedelta(days=1)

def renewal_window_days():
    try:
        x=db.settings.find_one({'key':'membership_policy'}) if db is not None else None
        return int((x or {}).get('renewalWindowDays', env('MEMBERSHIP_RENEWAL_WINDOW_DAYS',default='30')))
    except Exception:
        return int(env('MEMBERSHIP_RENEWAL_WINDOW_DAYS',default='30'))

def membership_state(member):
    status=str(member.get('status','')).upper(); until=member.get('validUntil')
    if status in {'ACTIVE','EXPIRING_SOON','EXPIRED'} and until:
        try:
            if until.tzinfo is None: until=until.replace(tzinfo=timezone.utc)
            days=(until.date()-now().date()).days; window=renewal_window_days()
            if days < 0: return 'EXPIRED'
            if days <= window: return 'EXPIRING_SOON'
            return 'ACTIVE'
        except Exception: pass
    return status

def sync_membership_state(member):
    state=membership_state(member)
    if state in {'ACTIVE','EXPIRING_SOON','EXPIRED'} and state != member.get('status'):
        try: db.members.update_one({'_id':member['_id']},{'$set':{'status':state,'updatedAt':now()}}); member['status']=state
        except Exception: pass
    return member
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


def _company_profile():
    vals={}
    if db is not None:
        try:
            vals={x.get('key'):x.get('value') for x in db.content.find({'key':{'$regex':r'^site\.'}})}
        except Exception: pass
    return {
        'name': vals.get('site.company') or os.getenv('COMPANY_NAME','Gavin Land & Design Consultants'),
        'tagline': vals.get('site.tagline') or 'LAND • DESIGN • DEVELOPMENT • CONSULTANCY',
        'phone': vals.get('site.phone') or env('COMPANY_PHONE','PHONE',default=''),
        'email': vals.get('site.email') or env('SMTP_REPLY_TO','EMAIL_REPLY_TO',default=''),
        'location': vals.get('site.location') or '',
        'logo_drive_id': vals.get('site.logoDriveId') or '',
    }

def _draw_qr(c, value, x, y, size=24*mm):
    if not value: return
    try:
        widget=qr.QrCodeWidget(value); bounds=widget.getBounds(); w=bounds[2]-bounds[0]; h=bounds[3]-bounds[1]
        d=Drawing(size,size,transform=[size/w,0,0,size/h,0,0]); d.add(widget); renderPDF.draw(d,c,x,y)
    except Exception: pass

def build_invoice_pdf(invoice):
    buf=BytesIO(); c=canvas.Canvas(buf,pagesize=A4); w,h=A4; company=_company_profile()
    c.setTitle(invoice['invoiceNumber'])
    # compact one-page professional invoice
    logo_id=company.get('logo_drive_id')
    logo_path=os.path.join(os.path.dirname(__file__),'static','assets','gldc-logo.png')
    try: c.drawImage(ImageReader(logo_path),20*mm,h-35*mm,width=30*mm,height=15*mm,mask='auto')
    except Exception: pass
    c.setFont('Helvetica-Bold',16); c.drawString(53*mm,h-23*mm,company['name'])
    c.setFont('Helvetica',8.5); c.drawString(53*mm,h-29*mm,company['tagline'])
    c.setFont('Helvetica',8); c.drawString(20*mm,h-35*mm,' • '.join(x for x in [company['phone'],company['email'],company['location']] if x))
    c.setFont('Helvetica-Bold',17); c.drawRightString(w-20*mm,h-23*mm,'INVOICE')
    c.setFont('Helvetica',9); c.drawRightString(w-20*mm,h-30*mm,invoice['invoiceNumber'])
    c.setStrokeColorRGB(.55,.29,.09); c.line(20*mm,h-39*mm,w-20*mm,h-39*mm)
    y=h-52*mm
    c.setFont('Helvetica-Bold',9); c.drawString(20*mm,y,'BILL TO'); c.drawRightString(w-20*mm,y,'ISSUED / DUE')
    c.setFont('Helvetica',9); c.drawString(20*mm,y-6*mm,invoice.get('recipientName') or invoice.get('recipientEmail','')); c.drawString(20*mm,y-12*mm,invoice.get('recipientEmail',''))
    c.drawRightString(w-20*mm,y-6*mm,str(invoice.get('issuedAt',''))[:10]+'  /  '+(invoice.get('dueDate') or 'On receipt'))
    y-=28*mm
    c.setFont('Helvetica-Bold',9); c.drawString(20*mm,y,'DESCRIPTION'); c.drawRightString(w-20*mm,y,'AMOUNT')
    c.line(20*mm,y-3*mm,w-20*mm,y-3*mm)
    c.setFont('Helvetica',9); desc=str(invoice.get('description','')); c.drawString(20*mm,y-11*mm,desc[:105]); c.drawRightString(w-20*mm,y-11*mm,f"KES {float(invoice.get('amount',0)):,.2f}")
    c.line(20*mm,y-18*mm,w-20*mm,y-18*mm)
    paid=float(invoice.get('amountPaid',0) or 0); total=float(invoice.get('amount',0) or 0); balance=max(total-paid,0)
    c.setFont('Helvetica',9); c.drawRightString(w-20*mm,y-27*mm,f'Amount paid: KES {paid:,.2f}')
    c.setFont('Helvetica-Bold',13); c.drawRightString(w-20*mm,y-36*mm,f'BALANCE: KES {balance:,.2f}')
    qr_value=invoice.get('verificationUrl') or f"{APP_URL}/invoice/{invoice['invoiceNumber']}"
    _draw_qr(c,qr_value,20*mm,28*mm,25*mm)
    c.setFont('Helvetica-Bold',8); c.drawString(49*mm,45*mm,'VERIFY / PAY')
    c.setFont('Helvetica',7.5); c.drawString(49*mm,40*mm,'Scan the QR code to verify this invoice or continue to payment.')
    c.setFont('Helvetica',7.5); c.drawString(49*mm,35*mm,'Thank you for choosing GLDC.')
    c.setFont('Helvetica',7.5); c.drawRightString(w-20*mm,25*mm,'Generated electronically • Keep this invoice for your records')
    c.save(); return buf.getvalue()

def build_receipt_pdf(payment, membership=None):
    buf=BytesIO(); c=canvas.Canvas(buf,pagesize=A4); w,h=A4; company=_company_profile()
    c.setTitle(payment.get('receiptCode') or payment.get('id','Receipt'))
    c.setFont('Helvetica-Bold',20); c.drawString(22*mm,h-28*mm,company['name']); c.setFont('Helvetica-Bold',16); c.drawRightString(w-22*mm,h-28*mm,'PAYMENT RECEIPT')
    c.setFont('Helvetica',9); c.drawString(22*mm,h-35*mm,company['tagline']); c.line(22*mm,h-41*mm,w-22*mm,h-41*mm)
    y=h-60*mm; rows=[('Receipt code',payment.get('receiptCode') or payment.get('mpesaReceiptNumber') or payment.get('id')),('Member', (membership or {}).get('name') or payment.get('memberName','')),('Email',(membership or {}).get('email') or payment.get('email','')),('Plan',payment.get('planName','')),('Amount',f"KES {float(payment.get('amount',0)):,.2f}"),('Method',payment.get('method','M-PESA')),('Transaction',payment.get('mpesaReceiptNumber') or payment.get('reference') or ''),('Date',str(payment.get('createdAt',''))[:19].replace('T',' '))]
    for k,v in rows:
        c.setFont('Helvetica-Bold',9); c.drawString(22*mm,y,k.upper()); c.setFont('Helvetica',10); c.drawString(65*mm,y,str(v)[:90]); y-=11*mm
    c.setFont('Helvetica',9); c.drawString(22*mm,35*mm,'This receipt confirms payment was recorded. Membership remains subject to GLDC review and approval.')
    c.save(); return buf.getvalue()

def build_membership_certificate(member, plan, certificate_no, valid_from=None, valid_until=None, issue_date=None):
    buf=BytesIO(); c=canvas.Canvas(buf,pagesize=A4); w,h=A4; c.setTitle(certificate_no)
    valid_from=valid_from or member.get('validFrom'); valid_until=valid_until or member.get('validUntil'); issue_date=issue_date or now()
    def d(v): return str(v)[:10] if v else ''
    c.setLineWidth(2); c.rect(14*mm,14*mm,w-28*mm,h-28*mm); c.setLineWidth(.6); c.rect(19*mm,19*mm,w-38*mm,h-38*mm)
    logo_path=os.path.join(os.path.dirname(__file__),'static','assets','gldc-logo.png')
    try: c.drawImage(ImageReader(logo_path),w/2-25*mm,h-38*mm,width=50*mm,height=25*mm,mask='auto')
    except Exception: pass
    c.setFont('Helvetica-Bold',12); c.drawCentredString(w/2,h-48*mm,'GAVIN LAND & DESIGN CONSULTANTS')
    c.setFont('Helvetica-Bold',24); c.drawCentredString(w/2,h-62*mm,'MEMBERSHIP CERTIFICATE')
    c.setFont('Helvetica',11); c.drawCentredString(w/2,h-79*mm,'This certifies that')
    c.setFont('Helvetica-Bold',20); c.drawCentredString(w/2,h-96*mm,str(member.get('name',''))[:70])
    c.setFont('Helvetica',11); c.drawCentredString(w/2,h-113*mm,'is an approved member of GLDC under the following membership plan:')
    c.setFont('Helvetica-Bold',14); c.drawCentredString(w/2,h-130*mm,str(plan.get('name',''))[:60])
    c.setFont('Helvetica',10); c.drawCentredString(w/2,h-145*mm,f"Membership No: {member.get('membershipNumber',member.get('id',''))}")
    c.drawCentredString(w/2,h-153*mm,f"Valid: {d(valid_from)} to {d(valid_until)}")
    c.drawCentredString(w/2,h-161*mm,f"Certificate: {certificate_no}   •   Issued: {d(issue_date)}")
    _draw_qr(c,f"{APP_URL}/membership/certificate/{certificate_no}",w/2-15*mm,31*mm,30*mm)
    c.setFont('Helvetica',8); c.drawCentredString(w/2,27*mm,'Scan to verify this certificate and its membership period.')
    c.save(); return buf.getvalue()

def email_template_render(name, variables):
    if db is None: return None
    t=db.email_templates.find_one({'name':name},{'_id':0})
    if not t: return None
    subject=str(t.get('subject','')); html=str(t.get('html','')); text=str(t.get('text',''))
    for k,v in variables.items():
        token='{{'+k+'}}'; subject=subject.replace(token,str(v)); html=html.replace(token,str(v)); text=text.replace(token,str(v))
    return subject, text, html

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


def drive_upload_bytes(filename, data, mimetype, folder=None):
    if not env_bool('GOOGLE_DRIVE_ENABLED', default=True):
        raise RuntimeError('GOOGLE_DRIVE_DISABLED')
    from googleapiclient.http import MediaIoBaseUpload
    from googleapiclient.errors import HttpError
    d=google_service('drive',['https://www.googleapis.com/auth/drive'])
    parent=folder or os.getenv('GOOGLE_DRIVE_FOLDER_ID','')
    if not parent:
        raise RuntimeError('GOOGLE_DRIVE_FOLDER_NOT_CONFIGURED')
    media=MediaIoBaseUpload(BytesIO(data),mimetype=mimetype,resumable=False)
    meta={'name':filename,'parents':[parent]}
    try:
        return d.files().create(body=meta,media_body=media,
            fields='id,name,mimeType,size,webViewLink,webContentLink',
            supportsAllDrives=True,
            includeItemsFromAllDrives=True).execute()
    except HttpError as exc:
        status=getattr(exc.resp,'status',None)
        raw=getattr(exc,'content',b'')
        try:
            detail=json.loads(raw.decode('utf-8','ignore')).get('error',{}).get('message','')
        except Exception:
            detail=''
        if status in (403,404):
            raise RuntimeError('GOOGLE_DRIVE_FOLDER_ACCESS_DENIED: The configured Google Drive folder is not writable by the service account. Share the folder with the Google service-account email as Editor/Content manager, or set the correct GOOGLE_DRIVE_FOLDER_ID.') from exc
        raise RuntimeError(f'GOOGLE_DRIVE_UPLOAD_FAILED: {detail or str(exc)}') from exc

def drive_download_bytes_by_id(file_id):
    meta=drive_metadata(file_id)[2]
    return _drive_download_direct(file_id,meta,'Drive file download')

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

def normalize_mpesa_phone(phone):
    # Daraja expects PartyA/PhoneNumber in international MSISDN format (2547XXXXXXXX / 2541XXXXXXXX).
    raw=re.sub(r'[^0-9+]', '', str(phone or '').strip())
    if raw.startswith('+'): raw=raw[1:]
    if raw.startswith('0') and len(raw)==10:
        raw='254'+raw[1:]
    elif raw.startswith('7') or raw.startswith('1'):
        if len(raw)==9: raw='254'+raw
    if not re.fullmatch(r'254[17]\d{8}', raw):
        raise RuntimeError('INVALID_MPESA_PHONE:Use a Kenyan mobile number such as 0712345678 or +254712345678.')
    return raw

def daraja_token():
    base='https://api.safaricom.co.ke' if str(env('DARAJA_ENV','DARAJA_ENVIRONMENT', default='production')).lower()=='production' else 'https://sandbox.safaricom.co.ke'
    key=str(env('DARAJA_CONSUMER_KEY', default=''))
    secret=str(env('DARAJA_CONSUMER_SECRET', default=''))
    if not key or not secret:
        raise RuntimeError('DARAJA_CONFIG_MISSING:Configure DARAJA_CONSUMER_KEY and DARAJA_CONSUMER_SECRET.')
    raw=f'{key}:{secret}'.encode()
    auth=base64.b64encode(raw).decode()
    r=requests.get(base+'/oauth/v1/generate?grant_type=client_credentials',headers={'Authorization':'Basic '+auth},timeout=20)
    try: data=r.json()
    except Exception: data={}
    if not r.ok or not data.get('access_token'):
        msg=data.get('errorMessage') or data.get('error_description') or data.get('errorCode') or f'HTTP {r.status_code}'
        raise RuntimeError('DARAJA_TOKEN_FAILED:'+str(msg))
    return base,data['access_token']

def daraja_stk(phone,amount,reference,description):
    customer=normalize_mpesa_phone(phone)
    amount=int(round(float(amount)))
    if amount < 1: raise RuntimeError('INVALID_PAYMENT_AMOUNT:Payment amount must be at least KES 1.')
    transaction_type=str(env('DARAJA_TRANSACTION_TYPE','MPESA_TRANSACTION_TYPE', default='CustomerBuyGoodsOnline'))
    short=str(env('DARAJA_SHORTCODE','DARAJA_PARTY_A_SHORTCODE', default='')).strip()
    till=str(env('DARAJA_TILL_NUMBER','DARAJA_PARTY_B_BUYGOODS_TILL','DARAJA_BUYGOODS_TILL', default='')).strip()
    passkey=str(env('DARAJA_PASSKEY', default='')).strip()
    if not short or not passkey: raise RuntimeError('DARAJA_CONFIG_MISSING:Configure DARAJA_SHORTCODE and DARAJA_PASSKEY.')
    if transaction_type=='CustomerBuyGoodsOnline' and not till:
        raise RuntimeError('DARAJA_CONFIG_MISSING:Configure DARAJA_TILL_NUMBER for CustomerBuyGoodsOnline.')
    base,access=daraja_token(); ts=datetime.now().strftime('%Y%m%d%H%M%S')
    password=base64.b64encode(f'{short}{passkey}{ts}'.encode()).decode()
    callback=str(env('DARAJA_CALLBACK_URL', default=APP_URL + '/api/payments/callback')).strip().rstrip('/')
    if not callback.endswith('/api/payments/callback'): callback += '/api/payments/callback'
    # IMPORTANT: PartyA is the customer's phone, not the business shortcode.
    body={'BusinessShortCode':short,'Password':password,'Timestamp':ts,'TransactionType':transaction_type,'Amount':amount,'PartyA':customer,'PartyB':till or short,'PhoneNumber':customer,'CallBackURL':callback,'AccountReference':str(reference)[:12],'TransactionDesc':str(description)[:13]}
    r=requests.post(base+'/mpesa/stkpush/v1/processrequest',headers={'Authorization':'Bearer '+access,'Content-Type':'application/json'},json=body,timeout=30)
    try: data=r.json()
    except Exception: data={}
    if not r.ok or data.get('ResponseCode')!='0':
        msg=data.get('ResponseDescription') or data.get('errorMessage') or data.get('error_description') or data.get('errorCode') or f'HTTP {r.status_code}'
        raise RuntimeError('DARAJA_STK_FAILED:'+str(msg))
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
    response.headers.setdefault('Content-Security-Policy', "default-src 'self'; img-src 'self' data: https:; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://unpkg.com; font-src 'self' https://fonts.gstatic.com; script-src 'self' 'unsafe-inline' https://unpkg.com; connect-src 'self' https://nominatim.openstreetmap.org https://unpkg.com; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")
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
def globals_():
    whatsapp_url=''; hero_image_url=''
    if db is not None:
        try:
            w=db.settings.find_one({'key':'whatsapp'}) or {}; whatsapp_url=w.get('url','') if w.get('enabled') else ''
            m=db.media.find_one({'slot':'home.hero','status':'PUBLISHED'}) or {}; hero_image_url=f'/api/public/media/{m.get("driveFileId")}' if m.get('driveFileId') else ''
        except Exception: pass
    return {'current_user':current_user(),'currency':CURRENCY,'year':datetime.now().year,'admin_path':ADMIN_PATH,'app_url':APP_URL,'whatsapp_url':whatsapp_url,'hero_image_url':hero_image_url}

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
def member(): return render_template('member.html', title='Become a GLDC Member')
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

@app.route('/admin/setup')
def admin_setup():
    if db is None or not ensure_database_initialized(): return render_template('404.html',title='Page not found'),404
    bootstrap_admin()
    if db.users.count_documents({'role':'SUPER ADMIN / OWNER'})>0: return render_template('404.html',title='Page not found'),404
    return render_template('admin_setup.html',title='Initialize GLDC Administration')

@app.route('/admin')
@app.route(ADMIN_PATH)
def admin():
    if db is None or not ensure_database_initialized(): return render_template('404.html',title='Page not found'),404
    if db.users.count_documents({'role':'SUPER ADMIN / OWNER'})==0: return render_template('404.html',title='Page not found'),404
    return render_template('admin.html', title='Management Console')

@app.post('/api/admin/setup')
def admin_setup_create():
    if db is None or not ensure_database_initialized(): return json_error('Service unavailable.',503,'SERVICE_UNAVAILABLE')
    if db.users.count_documents({'role':'SUPER ADMIN / OWNER'})>0: return json_error('Administration is already initialized.',409,'ADMIN_ALREADY_INITIALIZED')
    b=request.get_json(silent=True) or {}; email=str(b.get('email','')).strip().lower(); name=str(b.get('name','')).strip(); phone=str(b.get('phone','')).strip(); password=str(b.get('password',''))
    if '@' not in email or len(name)<2 or len(password)<12: return json_error('Name, valid email and 12+ character password are required.',422,'VALIDATION_ERROR')
    doc={'id':make_id('USR'),'email':email,'name':name,'phone':phone,'passwordHash':hash_password(password),'role':'SUPER ADMIN / OWNER','status':'ACTIVE','createdAt':now(),'updatedAt':now()}
    db.users.insert_one(doc); audit('FIRST_ADMIN_INITIALIZED','user',doc['id']); return jsonify(ok=True,message='Administration initialized. You can now sign in.')

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
    # CRM lead creation is deliberately restricted to authenticated members.
    if not _member_session():
        return json_error('Only authenticated GLDC members can create CRM leads. Please use phone, email or WhatsApp to contact GLDC.',403,'MEMBER_ONLY')
    return _create_member_lead(request.get_json(force=True) or {})

def _create_member_lead(b):
    m=_member_doc()
    if not m: return json_error('Member account not found.',404,'MEMBER_NOT_FOUND')
    required=['name','phone','email','service','county','town','description']
    if any(not str(b.get(k,'')).strip() for k in required) or '@' not in str(b.get('email','')): return json_error('Please complete all required lead fields.',422,'VALIDATION_ERROR')
    if len(str(b['description']).strip())<10: return json_error('Lead description must be at least 10 characters.',422,'VALIDATION_ERROR')
    rate_limit('member-lead:'+str(m.get('id')), maximum=int(os.getenv('LEAD_RATE_LIMIT_MAX','10')))
    lead_id='GLDC-LEAD-'+secrets.token_hex(5).upper(); t=now()
    doc={**b,'id':lead_id,'status':'NEW','source':'MEMBER PORTAL','memberId':m['id'],'memberEmail':m['email'],'createdAt':t,'updatedAt':t}
    db.leads.insert_one(doc)
    db.notifications.insert_one({'type':'NEW MEMBER LEAD','message':f'Member {m.get("name","")} created lead {lead_id}.','memberId':m['id'],'createdAt':t,'read':False})
    db.notifications.insert_one({'type':'LEAD CREATED','message':f'Lead {lead_id} was submitted successfully.','memberId':m['id'],'createdAt':t,'read':False})
    audit('MEMBER_LEAD_CREATED','lead',lead_id,{'memberId':m['id']})
    return jsonify(ok=True,id=lead_id,memberId=m['id']),201

@app.post('/api/member/leads')
def api_member_lead_create():
    if not _member_session(): return json_error('Member authentication required.',401,'UNAUTHORIZED')
    try: return _create_member_lead(request.get_json(force=True) or {})
    except RuntimeError as e: return json_error('Too many lead submissions. Please try again later.',429,'RATE_LIMITED') if str(e)=='RATE_LIMITED' else json_error('Unable to create lead.',500)
    except Exception: return json_error('Unable to create lead.',500,'LEAD_CREATE_FAILED')

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
    policy=db.settings.find_one({'key':'membership_policy'}) or {}
    abandon=abandonment_settings()
    return jsonify(ok=True,settings={'currency':CURRENCY,'timezone':APP_TIMEZONE,'appUrl':APP_URL,'adminPath':ADMIN_PATH,'darajaEnvironment':str(env('DARAJA_ENV','DARAJA_ENVIRONMENT', default='production')),'smtpConfigured':bool(env('SMTP_HOST') and env('SMTP_USER','GMAIL_USER') and env('SMTP_PASSWORD','GMAIL_APP_PASSWORD') and env('SMTP_FROM','EMAIL_FROM_ADDRESS')),'googleDriveConfigured':env_bool('GOOGLE_DRIVE_ENABLED', default=True) and bool(env('GOOGLE_DRIVE_FOLDER_ID')),'googleDriveAuth':'service-account','googleSheetsConfigured':env_bool('GOOGLE_SHEETS_ENABLED', default=False) and bool(env('GOOGLE_SPREADSHEET_ID')),'mongodbConfigured':bool(MONGO_URI),'paymentsEnabled':PAYMENTS_ENABLED,'membershipEnabled':MEMBERSHIP_ENABLED,'documentStorage':DOCUMENT_STORAGE,'pdfEnabled':PDF_ENABLED,'renewalWindowDays':int(policy.get('renewalWindowDays',renewal_window_days())),'abandonEmailHours':abandon['emailHours'],'abandonPaymentHours':abandon['paymentHours'],'abandonRenotifyHours':abandon['renotifyHours'],'adminNotificationsEnabled':ADMIN_NOTIFICATIONS_ENABLED,'emailNotificationsEnabled':EMAIL_NOTIFICATIONS_ENABLED,'cronSecretConfigured':bool(CRON_SECRET),'public':{k:s.get(k,'') for k in ['company','phone','email','location','hours','tagline']}})

@app.post('/api/admin/settings')
@admin_required
def api_settings_save():
    b=request.get_json(force=True) or {}
    allowed={k:str(b.get(k,'')).strip()[:200] for k in ['company','phone','email','location','hours','tagline'] if k in b}
    if allowed: db.settings.update_one({'key':'public'},{'$set':{**allowed,'key':'public','updatedAt':now()}},upsert=True)
    policy_update={}
    if 'renewalWindowDays' in b:
        try: days=int(b.get('renewalWindowDays',30) or 30)
        except Exception: return json_error('Renewal window must be a whole number of days.',422,'VALIDATION_ERROR')
        if days<0 or days>365: return json_error('Renewal window must be between 0 and 365 days.',422,'VALIDATION_ERROR')
        policy_update['renewalWindowDays']=days
    abandon_fields={'abandonEmailHours':(1,720),'abandonPaymentHours':(1,720),'abandonRenotifyHours':(1,720)}
    for key,(lo,hi) in abandon_fields.items():
        if key in b:
            try: hours=float(b.get(key))
            except Exception: return json_error(f'{key} must be a number of hours.',422,'VALIDATION_ERROR')
            if hours<lo or hours>hi: return json_error(f'{key} must be between {lo} and {hi} hours.',422,'VALIDATION_ERROR')
            policy_update[key]=hours
    if policy_update:
        policy_update['updatedAt']=now(); policy_update['updatedBy']=current_user().get('email')
        db.settings.update_one({'key':'membership_policy'},{'$set':policy_update},upsert=True)
    audit('SYSTEM_SETTINGS_UPDATED','settings','public',{'renewalWindowDays':b.get('renewalWindowDays'),**{k:b.get(k) for k in abandon_fields if k in b}})
    abandon=abandonment_settings()
    return jsonify(ok=True,renewalWindowDays=renewal_window_days(),abandonEmailHours=abandon['emailHours'],abandonPaymentHours=abandon['paymentHours'],abandonRenotifyHours=abandon['renotifyHours'])

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
        if success and p.get('memberId'):
            m=db.members.find_one({'id':p.get('memberId')}); plan=db.membership_plans.find_one({'id':p.get('planId')})
            if m:
                db.members.update_one({'_id':m['_id']},{'$set':{'status':'PENDING_REVIEW','paymentId':p['id'],'paymentReceiptCode':p.get('receiptCode'),'updatedAt':now()}})
                if p.get('renewalId'): db.membership_renewals.update_one({'id':p['renewalId']},{'$set':{'status':'PENDING_REVIEW','paidAt':now(),'updatedAt':now()}})
                p.update(update)
                try:
                    receipt=build_receipt_pdf(p,m); _send_member_email('membership_payment_receipt',m,{'name':m.get('name',''),'plan':p.get('planName',''),'amount':p.get('amount'),'receiptCode':p.get('receiptCode'),'subject':'GLDC Membership Payment Receipt','text':f'We received your membership payment. Receipt: {p.get("receiptCode")}.','html':f'<div style="font-family:Arial;max-width:640px;margin:auto"><h2 style="color:#8B4A18">Payment received</h2><p>Dear {m.get("name","")},</p><p>Your payment for <b>{p.get("planName","")}</b> has been received and your application is now awaiting GLDC review.</p><p><b>Receipt:</b> {p.get("receiptCode")}<br><b>Amount:</b> KES {float(p.get("amount",0)):,.2f}</p><p>Your payment receipt is attached.</p></div>'},[('GLDC-Receipt-'+str(p.get('receiptCode'))+'.pdf',receipt,'application/pdf')])
                except Exception: app.logger.exception('Membership receipt email failed')
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

# ================= MEMBERSHIP PLATFORM V14 =================
def _slugify(v):
    x=re.sub(r'[^a-z0-9]+','-',str(v).lower()).strip('-'); return x or secrets.token_hex(4)

def _membership_plan(name):
    return db.membership_plans.find_one({'id':name,'status':'ACTIVE'}) or db.membership_plans.find_one({'slug':name,'status':'ACTIVE'})

def _member_session():
    u=current_user(); return u if u and u.get('role')=='MEMBER' else None

def _member_doc():
    u=_member_session()
    return db.members.find_one({'id':u.get('memberId')}) if u and u.get('memberId') else (db.members.find_one({'email':u.get('email')}) if u else None)

def _member_public(m):
    if not m: return None
    return {k:clean_doc(m).get(k) for k in ['id','membershipNumber','name','profileSlug','bio','profession','company','location','phone','email','status','membershipPlan','validFrom','validUntil','photoDriveId']}

def _send_member_email(kind, member, variables, attachments=None):
    rendered=email_template_render(kind,variables)
    if rendered: subject,text,html=rendered
    else:
        subject=variables.get('subject','GLDC Membership Update'); text=variables.get('text',''); html=variables.get('html','<p>'+text+'</p>')
    send_email(member['email'],subject,text,html,attachments)

def _admin_emails():
    try:
        return [u['email'] for u in db.users.find({'role':{'$in':['SUPER ADMIN / OWNER','ADMIN']},'status':'ACTIVE'},{'email':1}) if u.get('email')]
    except Exception:
        app.logger.exception('Could not load admin recipient list')
        return []

# Statuses that count as "registration in progress" for abandonment tracking, mapped to the
# staleness threshold (hours of no activity) that marks that stage as abandoned.
ABANDONMENT_STAGE_THRESHOLDS = {
    'EMAIL_PENDING': MEMBERSHIP_ABANDON_EMAIL_HOURS,
    'PENDING_PAYMENT': MEMBERSHIP_ABANDON_PAYMENT_HOURS,
    'PAYMENT_FAILED': MEMBERSHIP_ABANDON_PAYMENT_HOURS,
    'PAYMENT_PENDING': MEMBERSHIP_ABANDON_PAYMENT_HOURS,
    'RENEWAL_PENDING': MEMBERSHIP_ABANDON_PAYMENT_HOURS,
}
ABANDONMENT_STAGE_LABELS = {
    'EMAIL_PENDING': 'has not verified their email',
    'PENDING_PAYMENT': 'has not completed membership payment',
    'PAYMENT_FAILED': 'had a failed M-Pesa payment and has not retried',
    'PAYMENT_PENDING': 'has an M-Pesa prompt outstanding',
    'RENEWAL_PENDING': 'started a renewal but has not completed payment',
}

def abandonment_settings():
    policy = db.settings.find_one({'key':'membership_policy'}) or {} if db is not None else {}
    return {
        'emailHours': float(policy.get('abandonEmailHours', MEMBERSHIP_ABANDON_EMAIL_HOURS)),
        'paymentHours': float(policy.get('abandonPaymentHours', MEMBERSHIP_ABANDON_PAYMENT_HOURS)),
        'renotifyHours': float(policy.get('abandonRenotifyHours', MEMBERSHIP_ABANDON_RENOTIFY_HOURS)),
    }

def _find_abandoned_members():
    """Members stuck at a registration/payment stage past the configured threshold,
    excluding ones already flagged within the re-notify window."""
    if db is None: return []
    out=[]
    t=now()
    settings=abandonment_settings()
    stage_hours = {
        'EMAIL_PENDING': settings['emailHours'],
        'PENDING_PAYMENT': settings['paymentHours'],
        'PAYMENT_FAILED': settings['paymentHours'],
        'PAYMENT_PENDING': settings['paymentHours'],
        'RENEWAL_PENDING': settings['paymentHours'],
    }
    renotify_cutoff = t - timedelta(hours=settings['renotifyHours'])
    for status, hours in stage_hours.items():
        cutoff = t - timedelta(hours=hours)
        query = {
            'status': status,
            'updatedAt': {'$lte': cutoff},
            '$or': [
                {'abandonedNotifiedAt': {'$exists': False}},
                {'abandonedNotifiedAt': None},
                {'abandonedNotifiedAt': {'$lte': renotify_cutoff}},
            ],
        }
        for m in db.members.find(query):
            updated = m.get('updatedAt') or m.get('createdAt') or t
            if getattr(updated, 'tzinfo', None) is None: updated = updated.replace(tzinfo=timezone.utc)
            hours_stalled = round((t - updated).total_seconds() / 3600, 1)
            out.append({'member': m, 'status': status, 'hoursStalled': hours_stalled, 'reason': ABANDONMENT_STAGE_LABELS.get(status, 'has not completed registration')})
    out.sort(key=lambda x: x['hoursStalled'], reverse=True)
    return out

def _notify_admin_member_abandoned(entry):
    m=entry['member']; reason=entry['reason']; hours=entry['hoursStalled']
    title='Membership application stalled'
    message=f"{m.get('name','A prospective member')} ({m.get('email','')}) {reason}. No activity for about {int(hours)} hours."
    try:
        db.notifications.insert_one({'id':make_id('NOT'),'title':title,'message':message,'type':'MEMBERSHIP_ABANDONED','audience':'ADMIN','memberId':m.get('id'),'read':False,'createdAt':now()})
    except Exception:
        app.logger.exception('Failed to write admin abandonment notification')
    if ADMIN_NOTIFICATIONS_ENABLED and EMAIL_NOTIFICATIONS_ENABLED:
        recipients=_admin_emails()
        for to in recipients:
            try:
                send_email(to, f'[GLDC] {title}', message, f'<p>{message}</p><p><a href="{APP_URL}{ADMIN_PATH}">Open Admin Portal</a></p>')
            except Exception:
                app.logger.exception('Failed to email admin about abandoned registration')
    try:
        db.members.update_one({'_id':m['_id']},{'$set':{'abandonedNotifiedAt':now(),'abandonedStage':entry['status']},'$inc':{'abandonedNotifyCount':1}})
    except Exception:
        app.logger.exception('Failed to mark member as abandonment-notified')

@app.get('/api/cron/check-abandoned-registrations')
def cron_check_abandoned_registrations():
    """Invoked by Vercel Cron (or any scheduler) on a recurring basis. Vercel automatically
    sends 'Authorization: Bearer <CRON_SECRET>' for configured Cron Jobs; we require that
    secret to match so the endpoint cannot be triggered by the public internet."""
    if not CRON_SECRET:
        return json_error('CRON_SECRET is not configured.',503,'CRON_NOT_CONFIGURED')
    supplied=request.headers.get('Authorization','').removeprefix('Bearer ').strip() or request.args.get('secret','').strip()
    if not secrets.compare_digest(supplied, CRON_SECRET):
        return json_error('Unauthorized.',401,'UNAUTHORIZED')
    if db is None: return json_error('Database unavailable.',503,'DATABASE_UNAVAILABLE')
    entries=_find_abandoned_members()
    for entry in entries:
        _notify_admin_member_abandoned(entry)
    return jsonify(ok=True, checked=len(entries), notified=len(entries))

@app.get('/api/admin/daraja/test')
@admin_required
def admin_daraja_test():
    """Verifies Daraja config without moving money: confirms which required vars are
    set, then attempts the OAuth token exchange (proves consumer key/secret + DARAJA_ENV
    are correct together) without calling the STK push endpoint at all."""
    groups = {
        'DARAJA_CONSUMER_KEY': ('DARAJA_CONSUMER_KEY',),
        'DARAJA_CONSUMER_SECRET': ('DARAJA_CONSUMER_SECRET',),
        'DARAJA_SHORTCODE': ('DARAJA_SHORTCODE','DARAJA_PARTY_A_SHORTCODE'),
        'DARAJA_TILL_NUMBER': ('DARAJA_TILL_NUMBER','DARAJA_PARTY_B_BUYGOODS_TILL','DARAJA_BUYGOODS_TILL'),
        'DARAJA_PASSKEY': ('DARAJA_PASSKEY',),
        'DARAJA_CALLBACK_URL': ('DARAJA_CALLBACK_URL',),
    }
    missing = [name for name, aliases in groups.items() if not any(env(k) for k in aliases)]
    result = {
        'darajaEnabled': env_bool('DARAJA_ENABLED', default=True),
        'darajaEnvironment': str(env('DARAJA_ENV','DARAJA_ENVIRONMENT', default='production')),
        'callbackUrl': str(env('DARAJA_CALLBACK_URL', default=APP_URL + '/api/payments/callback')),
        'missingConfig': missing,
        'oauth': None,
    }
    if missing:
        result['oauth'] = 'SKIPPED_MISSING_CONFIG'
        return jsonify(ok=False, daraja=result, error={'code':'DARAJA_CONFIG_MISSING','message':'Cannot test the OAuth handshake until these are set in Vercel: '+', '.join(missing)+'.'}), 422
    try:
        base, token = daraja_token()
        result['oauth'] = 'SUCCESS'
        result['apiBase'] = base
        return jsonify(ok=True, daraja=result, message='Daraja OAuth succeeded — consumer key/secret and DARAJA_ENV are correctly matched. STK pushes should work; if a real payment still fails, check the specific payment record for the exact Daraja error.')
    except RuntimeError as e:
        code, _, detail = str(e).partition(':')
        result['oauth'] = 'FAILED'
        result['oauthError'] = detail or code
        return jsonify(ok=False, daraja=result, error={'code':'DARAJA_OAUTH_FAILED','message':f'Daraja OAuth failed: {detail or code}. Check DARAJA_CONSUMER_KEY/SECRET and that DARAJA_ENV ({result["darajaEnvironment"]}) matches where those credentials were issued.'}), 502

@app.get('/api/admin/membership/abandoned')
@admin_required
def admin_membership_abandoned():
    entries=_find_abandoned_members()
    out=[{'member':clean_doc(e['member']),'status':e['status'],'hoursStalled':e['hoursStalled'],'reason':e['reason']} for e in entries]
    return jsonify(ok=True,abandoned=out)

@app.post('/api/admin/membership/abandoned/run-check')
@admin_required
def admin_membership_abandoned_run_check():
    """Lets an admin trigger the same check the cron job runs, on demand, so the
    feature can be tested/verified without waiting for the schedule or touching CRON_SECRET."""
    entries=_find_abandoned_members()
    for entry in entries:
        _notify_admin_member_abandoned(entry)
    audit('MEMBERSHIP_ABANDONMENT_CHECK_RUN_MANUALLY','settings','abandoned',{'flagged':len(entries)})
    return jsonify(ok=True,checked=len(entries),notified=len(entries))

@app.post('/api/admin/membership/abandoned/<member_id>/remind')
@admin_required
def admin_membership_abandoned_remind(member_id):
    m=db.members.find_one({'id':member_id})
    if not m: return json_error('Member not found.',404,'MEMBER_NOT_FOUND')
    status=str(m.get('status','')).upper()
    if status not in ABANDONMENT_STAGE_THRESHOLDS: return json_error('This member is not at a stalled registration stage.',409,'NOT_STALLED')
    try:
        resume_note='Verify your email to continue.' if status=='EMAIL_PENDING' else 'Complete your membership payment to continue.'
        _send_member_email('membership_reminder',m,{'name':m.get('name',''),'message':f"GLDC noticed your membership application is incomplete. {resume_note}",'resumeUrl':f'{APP_URL}/member/dashboard','subject':'Continue your GLDC membership application','text':f"Hi {m.get('name','')}, your GLDC membership application is incomplete. {resume_note} Continue here: {APP_URL}/member/dashboard",'html':f'<div style="font-family:Arial;max-width:640px;margin:auto"><h2 style="color:#8B4A18">Continue your GLDC membership application</h2><p>Dear {m.get("name","")},</p><p>GLDC noticed your membership application is incomplete. {resume_note}</p><p><a href="{APP_URL}/member/dashboard">CONTINUE WHERE I LEFT OFF</a></p></div>'})
        audit('MEMBERSHIP_ABANDONMENT_REMINDER_SENT','member',member_id)
        return jsonify(ok=True,message='Reminder email sent to the member.')
    except Exception:
        app.logger.exception('Manual abandonment reminder failed')
        return json_error('Could not send the reminder email right now.',502,'REMINDER_FAILED')

MEMBER_ADMIN_EDITABLE_FIELDS = {'name','email','phone','profession','company','location','bio','portfolioUrl','membershipNumber','adminMessage'}
MEMBER_ADMIN_EDITABLE_STATUSES = {'EMAIL_PENDING','PENDING_PAYMENT','PAYMENT_FAILED','PAYMENT_PENDING','PENDING_REVIEW','CHANGES_REQUIRED','REJECTED','RENEWAL_PENDING','ACTIVE','EXPIRED'}

@app.patch('/api/admin/members/<member_id>')
@admin_required
def admin_member_update(member_id):
    """General-purpose admin edit for a member record: contact details, membership dates
    and (within safe limits) status. Approving a brand-new membership with a certificate
    still goes through /decision — this endpoint will not silently activate a member that
    has no membership number or validity dates."""
    m=db.members.find_one({'id':member_id})
    if not m: return json_error('Member not found.',404,'MEMBER_NOT_FOUND')
    b=request.get_json(silent=True) or {}
    update={}; before={}
    for k in MEMBER_ADMIN_EDITABLE_FIELDS:
        if k in b:
            v=str(b[k]).strip()
            if k=='email': v=v.lower()
            if v!=str(m.get(k,'')): before[k]=m.get(k); update[k]=v
    if 'email' in update:
        if '@' not in update['email']: return json_error('Enter a valid email address.',422,'EMAIL_INVALID')
        dup=db.members.find_one({'email':update['email'],'id':{'$ne':member_id}})
        if dup: return json_error('Another member already uses that email address.',409,'EMAIL_IN_USE')
        update['emailVerified']=False
        if str(m.get('status','')).upper() in {'EMAIL_PENDING','PENDING_PAYMENT','PAYMENT_FAILED','PAYMENT_PENDING'}:
            update['status']='EMAIL_PENDING'; update['resumeToken']=secrets.token_urlsafe(32)
    if 'phone' in update:
        try: update['phone']=normalize_mpesa_phone(update['phone'])
        except RuntimeError: return json_error('Enter a valid Kenyan M-Pesa phone number.',422,'PHONE_INVALID')
    for date_field in ('validFrom','validUntil'):
        if date_field in b and str(b[date_field]).strip():
            try:
                dt=datetime.strptime(str(b[date_field]).strip()[:10],'%Y-%m-%d').replace(tzinfo=timezone.utc)
            except ValueError:
                return json_error(f'{date_field} must be a valid date (YYYY-MM-DD).',422,'DATE_INVALID')
            before[date_field]=m.get(date_field); update[date_field]=dt
    if 'status' in b and str(b['status']).strip():
        new_status=str(b['status']).strip().upper()
        if new_status not in MEMBER_ADMIN_EDITABLE_STATUSES: return json_error('Unsupported status value.',422,'STATUS_INVALID')
        if new_status=='ACTIVE':
            number=update.get('membershipNumber', m.get('membershipNumber',''))
            valid_until=update.get('validUntil', m.get('validUntil'))
            if not number or str(number).startswith('PENDING-') or not valid_until:
                return json_error('A membership number and validity dates are required before marking a member ACTIVE. Use Approve Membership to issue a certificate correctly, or set those fields first.',422,'MISSING_MEMBERSHIP_DETAILS')
        before['status']=m.get('status'); update['status']=new_status
    if not update: return json_error('No changes were submitted.',422,'NO_CHANGES')
    update['updatedAt']=now()
    db.members.update_one({'_id':m['_id']},{'$set':update})
    audit('MEMBER_ADMIN_EDITED','member',member_id,{'before':{k:(v.isoformat() if hasattr(v,'isoformat') else v) for k,v in before.items()},'after':{k:(v.isoformat() if hasattr(v,'isoformat') else v) for k,v in update.items() if k!='updatedAt'}})
    return jsonify(ok=True,member=clean_doc({**m,**update}))

@app.route('/member/login')
def member_login_page():
    if _member_session(): return redirect('/member/dashboard')
    return render_template('member_login.html',title='Member Portal Login')

@app.route('/member/register')
def member_register_page(): return redirect('/member')

@app.post('/api/member/login/request')
def member_login_request():
    try:
        rate_limit('member-login:'+client_ip(), maximum=int(os.getenv('LOGIN_RATE_LIMIT_MAX','10')))
        b=request.get_json(silent=True) or {}; email=str(b.get('email','')).strip().lower()
        if '@' not in email: return json_error('Enter a valid member email address.',422,'VALIDATION_ERROR')
        m=db.members.find_one({'email':email}) if db is not None else None
        if not m or not m.get('emailVerified'):
            return json_error('No verified membership account was found for this email. Please register for membership first.',404,'MEMBER_NOT_FOUND')
        if m.get('status') in {'REJECTED','SUSPENDED'}:
            return json_error('This membership account is not eligible for portal access. Please contact GLDC.',403,'MEMBER_ACCESS_BLOCKED')
        request_otp(email)
        return jsonify(ok=True,message='Member login code sent to your email.')
    except RuntimeError as e:
        if str(e)=='RATE_LIMITED': return json_error('Too many login attempts. Please try again later.',429,'RATE_LIMITED')
        if str(e)=='OTP_COOLDOWN': return json_error('Please wait before requesting another code.',429,'OTP_COOLDOWN')
        return json_error('Unable to send the login code.',503,'OTP_SEND_FAILED')
    except Exception:
        return json_error('Unable to send the login code.',503,'OTP_SEND_FAILED')

@app.post('/api/member/login/verify')
def member_login_verify():
    b=request.get_json(silent=True) or {}; email=str(b.get('email','')).strip().lower(); code=str(b.get('code','')).strip()
    try: verify_otp(email,code)
    except RuntimeError as e: return json_error('The verification code is invalid or expired.',400,str(e))
    m=db.members.find_one({'email':email}) if db is not None else None
    if not m or not m.get('emailVerified'): return json_error('Member account not found or email is not verified.',403,'MEMBER_NOT_FOUND')
    if m.get('status') in {'REJECTED','SUSPENDED'}: return json_error('This membership account is not eligible for portal access.',403,'MEMBER_ACCESS_BLOCKED')
    session.clear(); session.permanent=True; csrf_token(); session['user']={'id':str(m['_id']),'memberId':m['id'],'email':m['email'],'name':m.get('name',''),'role':'MEMBER'}
    audit('MEMBER_LOGIN','member',m['id']); return jsonify(ok=True,user=session['user'])

@app.route('/membership')
def membership_page(): return render_template('membership.html',title='GLDC Membership')

@app.route('/member/dashboard')
def member_dashboard():
    if not _member_session(): return redirect('/member/login')
    return render_template('member_dashboard.html',title='Member Portal')

@app.get('/api/member/portal')
def member_portal_api():
    m=_member_doc()
    if not m: return json_error('Member authentication required.',401,'UNAUTHORIZED')
    m=sync_membership_state(m); member_id=m.get('id')
    docs=[clean_doc(x) for x in db.member_documents.find({'memberId':member_id}).sort('createdAt',DESCENDING).limit(100)]
    payments=[clean_doc(x) for x in db.payments.find({'memberId':member_id}).sort('createdAt',DESCENDING).limit(100)]
    leads=[clean_doc(x) for x in db.leads.find({'memberId':member_id}).sort('createdAt',DESCENDING).limit(100)]
    notifications=[clean_doc(x) for x in db.notifications.find({'memberId':member_id}).sort('createdAt',DESCENDING).limit(100)]
    plans=[clean_doc(x) for x in db.membership_plans.find({'status':'ACTIVE'}).sort('sortOrder',ASCENDING)]
    certificates=[clean_doc(x) for x in db.membership_certificates.find({'memberId':member_id}).sort('issuedAt',DESCENDING).limit(100)]
    renewals=[clean_doc(x) for x in db.membership_renewals.find({'memberId':member_id}).sort('createdAt',DESCENDING).limit(100)]
    state=membership_state(m); window=renewal_window_days(); latest_payment=payments[0] if payments else None
    pending=any(str(x.get('status','')).upper() in {'PENDING_PAYMENT','PENDING_REVIEW'} for x in renewals)
    return jsonify(ok=True,member=_member_public(m)|{'status':state,'bio':m.get('bio',''),'profession':m.get('profession',''),'company':m.get('company',''),'location':m.get('location',''),'locationLat':m.get('locationLat'),'locationLng':m.get('locationLng'),'portfolioUrl':m.get('portfolioUrl',''),'certificateNumber':m.get('certificateNumber'),'certificateDriveId':m.get('certificateDriveId'),'adminMessage':m.get('adminMessage',''),'requestedFields':m.get('requestedFields',[]),'changeDeadline':m.get('changeDeadline'),'recreateUrl':f'{APP_URL}/membership/recreate/{m.get("recreateToken","")}' if m.get('recreateToken') else None,'latestPaymentStatus':latest_payment.get('status') if latest_payment else None,'latestPaymentId':latest_payment.get('id') if latest_payment else None,'latestMpesaReceiptNumber':latest_payment.get('mpesaReceiptNumber') if latest_payment else None,'renewalAvailable':state in {'EXPIRING_SOON','EXPIRED'} and not pending,'renewalWindowDays':window},documents=docs,payments=payments,leads=leads,notifications=notifications,plans=plans,certificates=certificates,renewals=renewals)

@app.get('/membership/certificate/<certificate_no>')
def membership_certificate_verify_page(certificate_no):
    c=db.membership_certificates.find_one({'certificateNumber':certificate_no}) if db is not None else None
    if not c: return render_template('404.html',title='Certificate not found'),404
    return render_template('membership_certificate_verify.html',title='Certificate Verification',certificate=clean_doc(c))

@app.get('/api/membership/certificate/<certificate_no>')
def membership_certificate_verify_api(certificate_no):
    c=db.membership_certificates.find_one({'certificateNumber':certificate_no}) if db is not None else None
    if not c: return json_error('Certificate not found.',404,'CERTIFICATE_NOT_FOUND')
    return jsonify(ok=True,certificate=clean_doc(c))

@app.get('/api/member/certificates/<certificate_no>/download')
def member_certificate_history_download(certificate_no):
    m=_member_doc(); c=db.membership_certificates.find_one({'certificateNumber':certificate_no,'memberId':m.get('id')}) if m else None
    if not c or not c.get('driveFileId'): return json_error('Certificate is not available.',404,'CERTIFICATE_NOT_AVAILABLE')
    try:
        data=drive_download_bytes_by_id(c['driveFileId']); return Response(data,mimetype='application/pdf',headers={'Content-Disposition':f'inline; filename="GLDC-{certificate_no}.pdf"'})
    except Exception: return json_error('Unable to retrieve certificate.',503,'CERTIFICATE_DOWNLOAD_FAILED')

@app.get('/api/member/certificate')
def member_certificate_download():
    m=_member_doc()
    if not m or not m.get('certificateDriveId'): return json_error('Membership certificate is not available yet.',404,'CERTIFICATE_NOT_AVAILABLE')
    try:
        data=drive_download_bytes_by_id(m['certificateDriveId'])
        return Response(data,mimetype='application/pdf',headers={'Content-Disposition':f'inline; filename="GLDC-{m.get("certificateNumber","membership-certificate")}.pdf"'})
    except Exception: return json_error('Unable to retrieve your membership certificate.',503,'CERTIFICATE_DOWNLOAD_FAILED')


@app.route('/members')
def members_directory():
    return render_template('members.html',title='GLDC Member Directory')

@app.get('/api/members')
def public_members():
    try:
        rows=db.members.find({'status':{'$in':['ACTIVE','EXPIRING_SOON']}},{'_id':0,'id':1,'name':1,'profileSlug':1,'profession':1,'company':1,'location':1,'photoDriveId':1}).sort('name',ASCENDING)
        return jsonify(ok=True,members=[clean_doc(x) for x in rows])
    except Exception:
        return json_error('The member directory is temporarily unavailable.',503,'MEMBER_DIRECTORY_UNAVAILABLE')

@app.route('/members/<slug>')
def member_profile(slug):
    m=db.members.find_one({'profileSlug':slug,'status':'ACTIVE'}) if db is not None else None
    if not m: return render_template('404.html',title='Member not found'),404
    return render_template('member_profile.html',title=m.get('name','GLDC Member'),member=_member_public(m))

@app.get('/api/membership/plans')
def membership_plans(): return jsonify(ok=True,plans=[clean_doc(x) for x in db.membership_plans.find({'status':'ACTIVE'}).sort('sortOrder',ASCENDING)])

@app.post('/api/membership/register')
def membership_register_v14():
    # Never allow a malformed registration to reach MongoDB/SMTP and never expose a raw
    # exception to the member. Existing EMAIL_PENDING records are resumable instead of
    # causing a duplicate-key 500 when the user submits the form again.
    try:
        b=request.get_json(silent=True) or {}
        name=str(b.get('name','')).strip()
        email=str(b.get('email','')).strip().lower()
        phone_input=str(b.get('phone','')).strip()
        email_ok=bool(re.fullmatch(r'[A-Za-z0-9.!#$%&\'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+',email))
        if len(name)<2: return json_error('Please enter your full name.',422,'NAME_INVALID')
        if not email_ok: return json_error('Please enter a valid email address, for example name@example.com.',422,'EMAIL_INVALID')
        try: phone=normalize_mpesa_phone(phone_input)
        except RuntimeError: return json_error('Please use a valid Kenyan M-Pesa number, for example 0712345678 or +254712345678.',422,'PHONE_INVALID')
        existing=db.members.find_one({'email':email})
        if existing:
            status=str(existing.get('status','')).upper()
            if status=='EMAIL_PENDING':
                resume_token=existing.get('resumeToken')
                if not resume_token:
                    resume_token=secrets.token_urlsafe(32)
                    db.members.update_one({'_id':existing['_id']},{'$set':{'resumeToken':resume_token,'updatedAt':now()}})
                if not existing.get('recreateToken'):
                    existing['recreateToken']=secrets.token_urlsafe(32); db.members.update_one({'_id':existing['_id']},{'$set':{'recreateToken':existing['recreateToken'],'updatedAt':now()}})
                return jsonify(ok=False,resume=True,memberId=existing.get('id'),resumeToken=resume_token,status=status,error={'code':'REGISTRATION_IN_PROGRESS','message':'A registration for this email has already started. Use Continue where you left off.'}),409
            return json_error('A membership account already exists for this email. Please use Member Login.',409,'MEMBER_EXISTS')
        member={'id':make_id('MEM'),'resumeToken':secrets.token_urlsafe(32),'recreateToken':secrets.token_urlsafe(32),'membershipNumber':'PENDING-'+secrets.token_hex(4).upper(),'name':name,'email':email,'phone':phone,'profileSlug':_slugify(name)+'-'+secrets.token_hex(3),'status':'EMAIL_PENDING','emailVerified':False,'createdAt':now(),'updatedAt':now(),'bio':str(b.get('bio','')).strip(),'profession':str(b.get('profession','')).strip(),'company':str(b.get('company','')).strip(),'location':str(b.get('location','')).strip(),'locationLat':float(b['locationLat']) if str(b.get('locationLat','')).strip() else None,'locationLng':float(b['locationLng']) if str(b.get('locationLng','')).strip() else None,'portfolioUrl':str(b.get('portfolioUrl','')).strip()}
        try:
            db.members.insert_one(member)
        except DuplicateKeyError:
            existing=db.members.find_one({'email':email})
            if existing and not existing.get('resumeToken'):
                existing['resumeToken']=secrets.token_urlsafe(32)
                db.members.update_one({'_id':existing['_id']},{'$set':{'resumeToken':existing['resumeToken'],'updatedAt':now()}})
            if existing and not existing.get('recreateToken'):
                existing['recreateToken']=secrets.token_urlsafe(32); db.members.update_one({'_id':existing['_id']},{'$set':{'recreateToken':existing['recreateToken'],'updatedAt':now()}})
            return jsonify(ok=False,resume=True,memberId=existing.get('id') if existing else None,resumeToken=existing.get('resumeToken') if existing else None,error={'code':'REGISTRATION_IN_PROGRESS','message':'A registration for this email already exists. Use Continue where you left off.'}),409
        try:
            request_otp(email)
        except RuntimeError as e:
            app.logger.warning('Membership OTP send failed request=%s code=%s',getattr(request,'request_id','unknown'),str(e))
            return jsonify(ok=False,resume=True,memberId=member['id'],resumeToken=member['resumeToken'],status='EMAIL_PENDING',error={'code':'OTP_SEND_FAILED','message':'Your registration was saved, but we could not send the verification code right now. Use Continue where you left off and try again.'}),503
        except Exception:
            app.logger.exception('Membership OTP send failed request=%s',getattr(request,'request_id','unknown'))
            return jsonify(ok=False,resume=True,memberId=member['id'],resumeToken=member['resumeToken'],status='EMAIL_PENDING',error={'code':'OTP_SEND_FAILED','message':'Your registration was saved, but the verification email could not be sent. Please try Continue where you left off again.'}),503
        audit('MEMBERSHIP_REGISTERED','member',member['id'])
        return jsonify(ok=True,memberId=member['id'],resumeToken=member['resumeToken'],message='Verification code sent to your email.')
    except Exception:
        app.logger.exception('Membership registration failed request=%s',getattr(request,'request_id','unknown'))
        return json_error('We could not complete registration right now. Please check your details and try again.',503,'REGISTRATION_UNAVAILABLE')

@app.post('/api/membership/resume')
def membership_resume():
    try:
        b=request.get_json(silent=True) or {}; email=str(b.get('email','')).strip().lower(); member_id=str(b.get('memberId','')).strip(); token=str(b.get('resumeToken','')).strip()
        if not email or not member_id or not token: return json_error('Your saved registration session is incomplete. Please start again.',422,'RESUME_INVALID')
        m=db.members.find_one({'email':email,'id':member_id,'resumeToken':token})
        if not m: return json_error('This saved registration could not be found. Please start a new registration.',404,'RESUME_NOT_FOUND')
        status=str(m.get('status','')).upper()
        session['user']={'id':str(m.get('_id')),'memberId':m.get('id'),'email':m.get('email'),'name':m.get('name',''),'role':'MEMBER'}
        if status=='EMAIL_PENDING': return jsonify(ok=True,status=status,memberId=m.get('id'),message='Your registration is ready to continue. Verify your email to proceed.')
        if status in {'PENDING_PAYMENT','PAYMENT_FAILED','PAYMENT_PENDING','RENEWAL_PENDING'}: return jsonify(ok=True,status=status,memberId=m.get('id'),message='Welcome back. Continue to membership plan and payment.')
        if status in {'PENDING_REVIEW','ACTIVE','EXPIRING_SOON','EXPIRED'}: return jsonify(ok=True,status=status,memberId=m.get('id'),message='Welcome back. Continue from your Member Portal.')
        return jsonify(ok=True,status=status,memberId=m.get('id'))
    except Exception:
        app.logger.exception('Membership resume failed request=%s',getattr(request,'request_id','unknown'))
        return json_error('We could not resume your registration right now. Please try again.',503,'RESUME_UNAVAILABLE')

@app.post('/api/membership/resend-otp')
def membership_resend_otp():
    try:
        b=request.get_json(silent=True) or {}; email=str(b.get('email','')).strip().lower(); m=db.members.find_one({'email':email,'status':'EMAIL_PENDING'}) if db is not None else None
        if not m: return json_error('Registration session not found or email is already verified.',404,'REGISTRATION_NOT_FOUND')
        request_otp(email); return jsonify(ok=True,message='A new verification code has been sent.')
    except RuntimeError as e:
        if str(e)=='OTP_COOLDOWN': return json_error('Please wait before requesting another code.',429,'OTP_COOLDOWN')
        return json_error('Unable to resend the verification code.',503,'OTP_SEND_FAILED')
    except Exception: return json_error('Unable to resend the verification code.',503,'OTP_SEND_FAILED')

@app.post('/api/membership/verify')
def membership_verify_v14():
    b=request.get_json(silent=True) or {}; email=str(b.get('email','')).strip().lower(); code=str(b.get('code','')).strip()
    try: verify_otp(email,code)
    except RuntimeError as e: return json_error('The verification code is invalid or expired.',400,str(e))
    m=db.members.find_one({'email':email})
    if not m: return json_error('Membership account not found.',404,'MEMBER_NOT_FOUND')
    db.members.update_one({'_id':m['_id']},{'$set':{'emailVerified':True,'status':'PENDING_PAYMENT','updatedAt':now()}})
    session['user']={'id':str(m['_id']),'memberId':m['id'],'email':email,'name':m.get('name',''),'role':'MEMBER'}
    audit('MEMBERSHIP_EMAIL_VERIFIED','member',m['id']); return jsonify(ok=True,user=session['user'],status='PENDING_PAYMENT')

@app.post('/api/membership/profile')
@login_required
def membership_profile_update():
    m=_member_doc()
    if not m: return json_error('Member account not found.',404,'MEMBER_NOT_FOUND')
    b=request.get_json(silent=True) or {}; allowed=['name','phone','bio','profession','company','location','locationLat','locationLng','portfolioUrl']
    update={k:str(b[k]).strip() for k in allowed if k in b}; update['updatedAt']=now(); update['profileSlug']=_slugify(update.get('name',m.get('name','')))+'-'+str(m.get('id',''))[-6:].lower()
    db.members.update_one({'_id':m['_id']},{'$set':update}); audit('MEMBER_PROFILE_UPDATED','member',m['id']); return jsonify(ok=True,member=_member_public({**m,**update}))

@app.post('/api/membership/documents')
@login_required
def membership_document_upload():
    m=_member_doc()
    if not m: return json_error('Member account not found.',404,'MEMBER_NOT_FOUND')
    f=request.files.get('file')
    if not f or not f.filename: return json_error('Choose a file.',422,'FILE_REQUIRED')
    data=f.read(); mt=f.mimetype or 'application/octet-stream'; ext=f.filename.rsplit('.',1)[-1].lower() if '.' in f.filename else ''
    if ext not in {'pdf','jpg','jpeg','png','webp','doc','docx'}: return json_error('Allowed files: PDF, image, DOC or DOCX.',422,'FILE_TYPE_NOT_ALLOWED')
    if len(data)>2*1024*1024: return json_error('File must be 2 MB or smaller.',413,'PAYLOAD_TOO_LARGE')
    category=str(request.form.get('category','SUPPORTING DOCUMENT')).upper()
    if category=='PASSPORT PHOTO' and ext not in {'jpg','jpeg','png','webp'}: return json_error('Passport photo must be an image.',422,'FILE_TYPE_NOT_ALLOWED')
    try: drive=drive_upload_bytes(f"{m['id']}-{category.replace(' ','-')}-{f.filename}",data,mt)
    except Exception as e:
        app.logger.exception('Member Drive upload failed')
        msg=str(e)
        if 'GOOGLE_DRIVE_FOLDER_ACCESS_DENIED' in msg:
            return json_error('Your file is ready, but GLDC storage is temporarily unavailable because the configured Google Drive folder is not writable. Please try again later or contact GLDC support.',503,'DRIVE_UPLOAD_FAILED')
        return json_error('Your file could not be stored securely right now. Please try again.',503,'DRIVE_UPLOAD_FAILED')
    rec={'id':make_id('MDOC'),'memberId':m['id'],'category':category,'name':f.filename,'driveFileId':drive.get('id'),'mimeType':mt,'size':len(data),'createdAt':now(),'createdBy':m['email']}
    db.member_documents.insert_one(rec)
    if category=='PASSPORT PHOTO': db.members.update_one({'_id':m['_id']},{'$set':{'photoDriveId':drive.get('id'),'photoName':f.filename,'updatedAt':now()}})
    audit('MEMBER_DOCUMENT_UPLOADED','member',m['id'],{'category':category,'driveFileId':drive.get('id')}); return jsonify(ok=True,document=clean_doc(rec))

@app.get('/api/members/me')
@login_required
def membership_me():
    m=_member_doc()
    if not m: return json_error('Member account not found.',404,'MEMBER_NOT_FOUND')
    return jsonify(ok=True,member=_member_public(m),documents=list_collection('member_documents',100))

@app.get('/api/members/photo/<member_id>')
def member_photo(member_id):
    m=db.members.find_one({'id':member_id,'status':'ACTIVE'}) if db is not None else None
    if not m or not m.get('photoDriveId'): return Response('Not found',404)
    try:
        data=drive_download_bytes_by_id(m['photoDriveId']); meta=drive_metadata(m['photoDriveId'])[2]; return Response(data,mimetype=meta.get('mimeType','image/jpeg'),headers={'Cache-Control':'public, max-age=3600'})
    except Exception: return Response('Not found',404)

@app.post('/api/membership/change-plan')
@login_required
def membership_change_plan():
    """Create a new membership payment when a member wants to change/upgrade tier.
    The existing membership remains active until the new payment is approved.
    """
    m=_member_doc()
    if not m or not m.get('emailVerified'): return json_error('Verify your email before changing your membership plan.',403,'EMAIL_NOT_VERIFIED')
    m=sync_membership_state(m); b=request.get_json(silent=True) or {}; plan=_membership_plan(str(b.get('planId','')))
    if not plan: return json_error('Membership plan not found.',404,'PLAN_NOT_FOUND')
    current_id=m.get('membershipPlanId')
    if current_id==plan.get('id'): return json_error('You are already on this membership plan.',409,'PLAN_UNCHANGED')
    state=membership_state(m)
    if state not in {'ACTIVE','EXPIRING_SOON','EXPIRED','PENDING_PAYMENT','PAYMENT_FAILED','PAYMENT_PENDING','RENEWAL_PENDING'}:
        return json_error('Your membership is not ready for a plan change.',409,'MEMBERSHIP_STATE')
    amount=float(plan['price']); pid=make_id('PAY'); receipt='GLDC-RCP-'+secrets.token_hex(5).upper(); renewal_id=make_id('REN')
    pay={'id':pid,'memberId':m['id'],'email':m['email'],'phone':m['phone'],'amount':amount,'currency':plan.get('currency','KES'),'planId':plan['id'],'planName':plan['name'],'method':'M-PESA','status':'PENDING','receiptCode':receipt,'purpose':'MEMBERSHIP_UPGRADE','renewalId':renewal_id,'createdAt':now(),'updatedAt':now(),'source':'MEMBERSHIP'}
    try:
        db.payments.insert_one(pay)
        db.membership_renewals.insert_one({'id':renewal_id,'memberId':m['id'],'paymentId':pid,'planId':plan['id'],'planName':plan['name'],'status':'PENDING_PAYMENT','type':'UPGRADE','createdAt':now(),'updatedAt':now()})
        r=daraja_stk(m['phone'],int(amount),m.get('membershipNumber') or m['id'],f"GLDC {plan['name']} membership upgrade")
        db.payments.update_one({'id':pid},{'$set':{'merchantRequestId':r.get('MerchantRequestID'),'checkoutRequestId':r.get('CheckoutRequestID'),'responseDescription':r.get('ResponseDescription')}})
        db.membership_renewals.update_one({'id':renewal_id},{'$set':{'status':'PAYMENT_PENDING','updatedAt':now()}})
        return jsonify(ok=True,paymentId=pid,renewalId=renewal_id,receiptCode=receipt,status='PENDING',message=r.get('CustomerMessage','Payment prompt sent. Approve it on your phone.'))
    except Exception as e:
        app.logger.exception('Membership plan change failed')
        db.payments.update_one({'id':pid},{'$set':{'status':'FAILED','error':str(e),'updatedAt':now()}})
        db.membership_renewals.update_one({'id':renewal_id},{'$set':{'status':'PAYMENT_FAILED','updatedAt':now()}})
        return json_error('We could not start the plan-change payment. Your current membership has not been changed.',502,'PLAN_CHANGE_FAILED')

@app.post('/api/membership/subscribe')
@login_required
def membership_subscribe():
    m=_member_doc()
    if not m or not m.get('emailVerified'): return json_error('Verify your email before subscribing.',403,'EMAIL_NOT_VERIFIED')
    m=sync_membership_state(m); b=request.get_json(silent=True) or {}; plan=_membership_plan(str(b.get('planId','')))
    if not plan: return json_error('Membership plan not found.',404,'PLAN_NOT_FOUND')
    state=membership_state(m); is_renewal=state in {'ACTIVE','EXPIRING_SOON','EXPIRED'} or m.get('status')=='RENEWAL_PENDING'
    if not is_renewal and state not in {'PENDING_PAYMENT','PAYMENT_FAILED','EMAIL_PENDING'}: return json_error('This membership is not ready for a new payment.',409,'MEMBERSHIP_STATE')
    if state=='ACTIVE':
        window=renewal_window_days(); until=m.get('validUntil'); days=(until.date()-now().date()).days if until else 9999
        if days>window: return json_error(f'Renewal opens {window} days before expiry.',409,'RENEWAL_NOT_OPEN')
    purpose='RENEWAL' if is_renewal else 'INITIAL'; amount=float(plan['price']); pid=make_id('PAY'); receipt='GLDC-RCP-'+secrets.token_hex(5).upper(); renewal_id=make_id('REN') if is_renewal else None
    pay={'id':pid,'memberId':m['id'],'email':m['email'],'phone':m['phone'],'amount':amount,'currency':plan.get('currency','KES'),'planId':plan['id'],'planName':plan['name'],'method':'M-PESA','status':'PENDING','receiptCode':receipt,'purpose':'MEMBERSHIP_'+purpose,'renewalId':renewal_id,'createdAt':now(),'updatedAt':now(),'source':'MEMBERSHIP'}
    db.payments.insert_one(pay)
    if renewal_id: db.membership_renewals.insert_one({'id':renewal_id,'memberId':m['id'],'paymentId':pid,'planId':plan['id'],'planName':plan['name'],'status':'PENDING_PAYMENT','createdAt':now(),'updatedAt':now()})
    try:
        r=daraja_stk(m['phone'],int(amount),m['membershipNumber'],f"GLDC {plan['name']} membership {purpose.lower()}")
        db.payments.update_one({'id':pid},{'$set':{'merchantRequestId':r.get('MerchantRequestID'),'checkoutRequestId':r.get('CheckoutRequestID'),'responseDescription':r.get('ResponseDescription')}})
        db.members.update_one({'_id':m['_id']},{'$set':{'status':'RENEWAL_PENDING' if is_renewal else 'PAYMENT_PENDING','membershipPlan':plan['name'],'membershipPlanId':plan['id'],'updatedAt':now()}})
        if renewal_id: db.membership_renewals.update_one({'id':renewal_id},{'$set':{'status':'PAYMENT_PENDING','updatedAt':now()}})
        return jsonify(ok=True,paymentId=pid,renewalId=renewal_id,receiptCode=receipt,status='PENDING',purpose=purpose,message=r.get('CustomerMessage','Payment prompt sent.'))
    except Exception as e:
        db.payments.update_one({'id':pid},{'$set':{'status':'FAILED','updatedAt':now(),'error':str(e)}})
        if renewal_id: db.membership_renewals.update_one({'id':renewal_id},{'$set':{'status':'PAYMENT_FAILED','updatedAt':now()}})
        db.members.update_one({'_id':m['_id']},{'$set':{'status':'PAYMENT_FAILED' if not is_renewal else state,'updatedAt':now()}})
        try:
            _send_member_email('membership_payment_failed',m,{'name':m.get('name',''),'plan':plan.get('name',''),'message':'The M-Pesa payment prompt could not be completed. If money was deducted, do not pay again. Submit the M-Pesa transaction code from your M-Pesa message for GLDC verification.','resumeUrl':f'{APP_URL}/member/dashboard','recreateUrl':f'{APP_URL}/membership/recreate/{m.get("recreateToken","")}'})
        except Exception: app.logger.exception('Payment failure email failed')
        return json_error('We could not start the M-Pesa payment. If money was deducted, do not pay again—submit your M-Pesa transaction code for GLDC verification.',502,'PAYMENT_FAILED')

@app.post('/api/membership/submit-payment-reference')
@login_required
def membership_submit_payment_reference():
    try:
        m=_member_doc()
        if not m: return json_error('Member application not found.',404,'MEMBER_NOT_FOUND')
        b=request.get_json(silent=True) or {}
        code=re.sub(r'[^A-Za-z0-9]','',str(b.get('mpesaCode','')).upper())
        if not re.fullmatch(r'[A-Z0-9]{8,20}',code): return json_error('Enter the M-Pesa transaction code exactly as shown on your M-Pesa message.',422,'MPESA_CODE_INVALID')
        try: amount=float(b.get('amount',0) or 0)
        except Exception: amount=0
        if amount<=0: return json_error('Enter the amount you paid.',422,'AMOUNT_INVALID')
        try: phone=normalize_mpesa_phone(str(b.get('phone','')).strip() or m.get('phone',''))
        except RuntimeError: return json_error('Please enter a valid Kenyan M-Pesa number.',422,'PHONE_INVALID')
        payment_date=str(b.get('paymentDate','')).strip()
        latest=db.payments.find_one({'memberId':m['id']},sort=[('createdAt',DESCENDING)])
        if not latest: return json_error('No membership payment attempt was found. Please select your membership plan first.',409,'PAYMENT_NOT_FOUND')
        if str(latest.get('status','')).upper()=='SUCCESSFUL': return json_error('Your payment is already recorded. Please wait for GLDC validation.',409,'PAYMENT_ALREADY_RECORDED')
        plan=db.membership_plans.find_one({'id':latest.get('planId')})
        if plan and abs(float(plan.get('price',0))-amount)>0.01: return json_error(f'The amount does not match the selected plan ({CURRENCY} {float(plan.get("price",0)):,.2f}). Please check your M-Pesa message.',422,'AMOUNT_MISMATCH')
        duplicate=db.payments.find_one({'mpesaReceiptNumber':code})
        if duplicate and duplicate.get('memberId')!=m.get('id'): return json_error('That M-Pesa transaction code has already been submitted for another application.',409,'MPESA_CODE_USED')
        update={'status':'PENDING_ADMIN_VERIFICATION','mpesaReceiptNumber':code,'reference':code,'manualPaymentCode':code,'manualAmount':amount,'phoneNumber':phone,'manualPaymentDate':payment_date or None,'manualSubmittedAt':now(),'updatedAt':now(),'verificationNote':'Member submitted M-Pesa receipt for admin verification.'}
        db.payments.update_one({'_id':latest['_id']},{'$set':update})
        db.members.update_one({'_id':m['_id']},{'$set':{'status':'PENDING_REVIEW','paymentId':latest.get('id'),'paymentReceiptCode':latest.get('receiptCode'),'updatedAt':now()}})
        audit('MEMBERSHIP_MANUAL_PAYMENT_SUBMITTED','member',m['id'],{'paymentId':latest.get('id'),'mpesaCode':code})
        try: db.notifications.insert_one({'id':make_id('NOT'),'memberId':m['id'],'title':'Payment submitted for GLDC verification','message':'Your M-Pesa receipt has been submitted. GLDC will verify the transaction before approving your membership.','type':'MEMBERSHIP','audience':'MEMBER','createdAt':now()})
        except Exception: pass
        return jsonify(ok=True,status='PENDING_REVIEW',message='Payment details received. GLDC will verify the M-Pesa transaction and review your membership application.')
    except Exception:
        app.logger.exception('Manual membership payment submission failed request=%s',getattr(request,'request_id','unknown'))
        return json_error('We could not submit your payment details right now. Please try again.',503,'PAYMENT_REFERENCE_UNAVAILABLE')

@app.get('/membership/recreate/<token>')
def membership_recreate_page(token):
    m=db.members.find_one({'recreateToken':token}) if db is not None else None
    if not m: return render_template('404.html',title='Link expired'),404
    allowed={'EMAIL_PENDING','PENDING_PAYMENT','PAYMENT_FAILED','PAYMENT_PENDING','CHANGES_REQUIRED','REJECTED'}
    if str(m.get('status','')).upper() not in allowed: return render_template('membership_recreate.html',title='Cannot restart membership',allowed=False,message='This membership application can no longer be removed because payment or membership processing has progressed.'),409
    paid=db.payments.find_one({'memberId':m.get('id'),'status':{'$in':['SUCCESSFUL','PENDING_ADMIN_VERIFICATION']}})
    if paid: return render_template('membership_recreate.html',title='Cannot restart membership',allowed=False,message='This application has a payment record under review. Please wait for GLDC validation instead of creating another application.'),409
    return render_template('membership_recreate.html',title='Restart membership application',allowed=True,member=clean_doc(m),token=token)

@app.post('/api/membership/recreate/<token>')
def membership_recreate(token):
    try:
        m=db.members.find_one({'recreateToken':token}) if db is not None else None
        if not m: return json_error('This restart link is invalid or has expired.',404,'RECREATE_NOT_FOUND')
        allowed={'EMAIL_PENDING','PENDING_PAYMENT','PAYMENT_FAILED','PAYMENT_PENDING','CHANGES_REQUIRED','REJECTED'}
        if str(m.get('status','')).upper() not in allowed: return json_error('This application cannot be removed because membership processing has progressed.',409,'RECREATE_BLOCKED')
        paid=db.payments.find_one({'memberId':m.get('id'),'status':{'$in':['SUCCESSFUL','PENDING_ADMIN_VERIFICATION']}})
        if paid: return json_error('A payment is already under review. Please wait for GLDC validation.',409,'PAYMENT_UNDER_REVIEW')
        db.otps.delete_many({'email':m.get('email')}); db.membership_renewals.delete_many({'memberId':m.get('id')}); db.payments.delete_many({'memberId':m.get('id')}); db.member_documents.delete_many({'memberId':m.get('id')}); db.notifications.delete_many({'memberId':m.get('id')}); db.members.delete_one({'_id':m['_id']}); session.pop('user',None)
        audit('MEMBERSHIP_APPLICATION_REMOVED','member',m.get('id'),{'email':m.get('email')})
        return jsonify(ok=True,message='Your unfinished membership application has been removed. You can now create a new membership application.')
    except Exception:
        app.logger.exception('Membership recreate failed')
        return json_error('We could not remove the unfinished application right now. Please try again.',503,'RECREATE_FAILED')

@app.post('/api/admin/membership/request-action')
@admin_required
def admin_membership_request_action():
    try:
        b=request.get_json(silent=True) or {}; member_id=str(b.get('memberId','')).strip(); message=str(b.get('message','')).strip(); deadline=str(b.get('deadline','')).strip(); fields=b.get('fields') or []
        if not member_id or not message: return json_error('Select a member and explain what they need to complete.',422,'VALIDATION_ERROR')
        m=db.members.find_one({'id':member_id})
        if not m: return json_error('Member application not found.',404,'MEMBER_NOT_FOUND')
        edit_url=f'{APP_URL}/member/dashboard?edit=1'
        field_text=', '.join(str(x).replace('_',' ').title() for x in fields) or 'the details requested in this message'
        due=f'<p><b>Requested fields:</b> {field_text}</p>' if fields else ''
        if deadline: due += f'<p><b>Please complete by:</b> {deadline}</p>'
        current_status=str(m.get('status','')).upper(); new_status=current_status if current_status in {'ACTIVE','EXPIRING_SOON','EXPIRED'} else 'CHANGES_REQUIRED'; db.members.update_one({'_id':m['_id']},{'$set':{'status':new_status,'actionRequired':True,'adminMessage':message,'requestedFields':fields,'changeDeadline':deadline or None,'updatedAt':now()}})
        db.notifications.insert_one({'id':make_id('NOT'),'memberId':m['id'],'title':'GLDC action required','message':message,'type':'MEMBERSHIP','audience':'MEMBER','createdAt':now()})
        variables={'name':m.get('name',''),'message':message,'editUrl':edit_url,'requestedFields':field_text,'deadline':deadline or 'As soon as possible','subject':'GLDC Membership – action required','text':f'Dear {m.get("name","")},\n\n{message}\n\nRequested fields: {field_text}\nDeadline: {deadline or "As soon as possible"}\n\nUpdate your details here: {edit_url}'}
        rendered=email_template_render('membership_action_request',variables)
        if rendered: send_email(m['email'],rendered[0],rendered[1],rendered[2])
        else: send_email(m['email'],variables['subject'],variables['text'],f'<div style="font-family:Arial;max-width:640px;margin:auto"><h2 style="color:#8B4A18">GLDC Membership – action required</h2><p>Dear <b>{m.get("name","")}</b>,</p><p>{message}</p>{due}<p><a href="{edit_url}" style="background:#8B4A18;color:#fff;padding:12px 18px;text-decoration:none">OPEN MEMBER PORTAL</a></p></div>')
        audit('MEMBERSHIP_ACTION_REQUESTED','member',member_id,{'fields':fields,'deadline':deadline})
        return jsonify(ok=True,message='Member action request emailed successfully.')
    except Exception as e:
        app.logger.exception('Membership action email failed')
        if 'SMTP' in str(e).upper(): return json_error('The member details were saved, but the email could not be sent because email delivery is not configured or available.',503,'EMAIL_SEND_FAILED')
        return json_error('We could not send the member action request right now.',503,'MEMBER_ACTION_EMAIL_FAILED')

@app.post('/api/admin/membership/payments/<payment_id>/verify')
@admin_required
def admin_verify_membership_payment(payment_id):
    b=request.get_json(silent=True) or {}; decision=str(b.get('decision','VERIFY')).upper(); p=db.payments.find_one({'id':payment_id})
    if not p: return json_error('Payment not found.',404,'PAYMENT_NOT_FOUND')
    if decision not in {'VERIFY','REJECT'}: return json_error('Invalid payment decision.',422,'VALIDATION_ERROR')
    m=db.members.find_one({'id':p.get('memberId')})
    if not m: return json_error('Member application not found.',404,'MEMBER_NOT_FOUND')
    if decision=='VERIFY':
        code=str(p.get('mpesaReceiptNumber') or p.get('manualPaymentCode') or '').strip().upper()
        if not code: return json_error('No M-Pesa receipt code has been submitted.',409,'MPESA_CODE_MISSING')
        db.payments.update_one({'_id':p['_id']},{'$set':{'status':'SUCCESSFUL','verifiedManually':True,'verifiedAt':now(),'verifiedBy':current_user().get('email'),'resultDescription':'Verified by GLDC administrator.','updatedAt':now(),'mpesaReceiptNumber':code}})
        db.members.update_one({'_id':m['_id']},{'$set':{'status':'PENDING_REVIEW','paymentId':p['id'],'paymentReceiptCode':p.get('receiptCode'),'updatedAt':now()}})
        db.notifications.insert_one({'id':make_id('NOT'),'memberId':m['id'],'title':'Payment verified by GLDC','message':'Your M-Pesa payment has been verified. Your membership application is now awaiting final GLDC approval.','type':'MEMBERSHIP','audience':'MEMBER','createdAt':now()})
        try: _send_member_email('membership_payment_verified',m,{'name':m.get('name',''),'plan':p.get('planName',''),'amount':p.get('amount'),'mpesaCode':code,'editUrl':f'{APP_URL}/member/dashboard'})
        except Exception: app.logger.exception('Payment verified email failed')
        audit('MEMBERSHIP_PAYMENT_VERIFIED','payment',payment_id,{'memberId':m['id'],'mpesaCode':code})
        return jsonify(ok=True,status='SUCCESSFUL',message='Payment verified. The membership application is now ready for final approval.')
    note=str(b.get('message','The M-Pesa receipt could not be verified. Please check the transaction details and submit the correct code.')).strip()
    db.payments.update_one({'_id':p['_id']},{'$set':{'status':'FAILED','verificationRejected':True,'verificationMessage':note,'verifiedAt':now(),'verifiedBy':current_user().get('email'),'updatedAt':now()}})
    db.members.update_one({'_id':m['_id']},{'$set':{'status':'PAYMENT_FAILED','adminMessage':note,'updatedAt':now()}})
    try: _send_member_email('membership_payment_failed',m,{'name':m.get('name',''),'message':note,'resumeUrl':f'{APP_URL}/member/dashboard','recreateUrl':f'{APP_URL}/membership/recreate/{m.get("recreateToken","")}'})
    except Exception: app.logger.exception('Payment rejection email failed')
    audit('MEMBERSHIP_PAYMENT_REJECTED','payment',payment_id,{'memberId':m['id']})
    return jsonify(ok=True,status='FAILED',message='Payment marked as unverified and the member has been notified.')

@app.get('/api/admin/membership/plans')
@admin_required
def admin_membership_plans(): return jsonify(ok=True,plans=list_collection('membership_plans',100))

@app.post('/api/admin/membership/plans')
@admin_required
def admin_membership_plan_create():
    b=request.get_json(silent=True) or {}; name=str(b.get('name','')).strip(); price=float(b.get('price',0) or 0); months=int(b.get('months',1) or 1)
    if not name or price<=0 or months<=0: return json_error('Plan name, price and duration are required.',422,'VALIDATION_ERROR')
    doc={'id':make_id('PLAN'),'slug':_slugify(name),'name':name,'price':price,'currency':'KES','months':months,'billingCycle':'YEARLY' if months>=12 else 'MONTHLY','status':str(b.get('status','ACTIVE')).upper(),'description':str(b.get('description','')).strip(),'sortOrder':int(b.get('sortOrder',0) or 0),'createdAt':now(),'updatedAt':now()}
    db.membership_plans.insert_one(doc); audit('MEMBERSHIP_PLAN_CREATED','membership_plan',doc['id']); return jsonify(ok=True,plan=clean_doc(doc)),201

@app.patch('/api/admin/membership/plans/<plan_id>')
@admin_required
def admin_membership_plan_update(plan_id):
    b=request.get_json(silent=True) or {}; allowed=['name','price','months','billingCycle','status','description','sortOrder']; u={k:b[k] for k in allowed if k in b}; u['updatedAt']=now(); db.membership_plans.update_one({'id':plan_id},{'$set':u}); audit('MEMBERSHIP_PLAN_UPDATED','membership_plan',plan_id,u); return jsonify(ok=True)

@app.get('/api/admin/members')
@admin_required
def admin_members_v14():
    xs=list(db.members.find({}).sort('createdAt',DESCENDING).limit(500)); out=[]
    for x in xs:
        x=sync_membership_state(x)
        latest=db.payments.find_one({'memberId':x.get('id')},sort=[('createdAt',DESCENDING)])
        if latest:
            x['latestPaymentStatus']=latest.get('status')
            x['latestPaymentId']=latest.get('id')
            x['latestMpesaReceiptNumber']=latest.get('mpesaReceiptNumber')
            x['latestPaymentAmount']=latest.get('amount')
            x['latestPaymentReference']=latest.get('reference')
            x['latestPaymentPhone']=latest.get('phoneNumber') or latest.get('phone')
            x['latestPaymentDate']=latest.get('manualPaymentDate') or latest.get('transactionDate')
            x['paymentReceiptCode']=latest.get('receiptCode') or x.get('paymentReceiptCode')
        out.append(clean_doc(x))
    return jsonify(ok=True,members=out)

@app.post('/api/admin/members/<member_id>/decision')
@admin_required
def admin_member_decision(member_id):
    b=request.get_json(silent=True) or {}; decision=str(b.get('decision','')).upper(); m=db.members.find_one({'id':member_id})
    if not m: return json_error('Member not found.',404,'MEMBER_NOT_FOUND')
    if decision not in {'APPROVE','REQUEST_CHANGES','REJECT'}: return json_error('Invalid decision.',422,'VALIDATION_ERROR')
    if decision=='APPROVE':
        payment=db.payments.find_one({'id':m.get('paymentId')}) if m.get('paymentId') else None
        if not payment or str(payment.get('status','')).upper()!='SUCCESSFUL':
            return json_error('Payment must be successfully verified before membership approval.',409,'PAYMENT_NOT_VERIFIED')
        plan=_membership_plan(m.get('membershipPlanId','')) or db.membership_plans.find_one({'name':m.get('membershipPlan')})
        if not plan: return json_error('Membership plan not found.',409,'PLAN_NOT_FOUND')
        old_certificate_number=m.get('certificateNumber')
        is_renewal=bool(m.get('paymentId') and db.payments.find_one({'id':m.get('paymentId'),'purpose':{'$in':['MEMBERSHIP_RENEWAL','MEMBERSHIP_UPGRADE']}}))
        issue_date=now()
        if is_renewal and m.get('validUntil'):
            base=m.get('validUntil'); base=base.replace(tzinfo=timezone.utc) if getattr(base,'tzinfo',None) is None else base
            from_date=max(issue_date,base+timedelta(days=1))
            number=m.get('membershipNumber') or 'GLDC-M-'+secrets.token_hex(4).upper()
        else:
            from_date=issue_date; number='GLDC-M-'+secrets.token_hex(4).upper()
        from_date,valid=membership_period(from_date,int(plan.get('months',1))); cert='GLDC-CERT-'+secrets.token_hex(5).upper()
        update={'status':'ACTIVE','membershipNumber':number,'validFrom':from_date,'validUntil':valid,'certificateNumber':cert,'approvedAt':issue_date,'approvedBy':current_user().get('email'),'updatedAt':issue_date}
        db.members.update_one({'_id':m['_id']},{'$set':update}); m.update(update)
        if is_renewal and old_certificate_number:
            db.membership_certificates.update_one({'certificateNumber':old_certificate_number,'memberId':m['id']},{'$set':{'status':'EXPIRED','expiredAt':issue_date,'updatedAt':issue_date}})
        cert_doc={'id':make_id('CERT'),'certificateNumber':cert,'memberId':m['id'],'membershipNumber':number,'memberName':m.get('name',''),'planId':plan['id'],'planName':plan['name'],'validFrom':from_date,'validUntil':valid,'issuedAt':issue_date,'status':'ACTIVE','replacesCertificateNumber':old_certificate_number if is_renewal else None,'createdAt':issue_date}
        pdf=build_membership_certificate(m,plan,cert,from_date,valid,issue_date)
        try:
            drive=drive_upload_bytes(f'{cert}.pdf',pdf,'application/pdf'); cert_doc['driveFileId']=drive.get('id'); db.members.update_one({'_id':m['_id']},{'$set':{'certificateDriveId':drive.get('id')}})
        except Exception: app.logger.exception('Certificate Drive archive failed')
        db.membership_certificates.insert_one(cert_doc)
        if is_renewal and m.get('paymentId'): db.membership_renewals.update_one({'paymentId':m.get('paymentId')},{'$set':{'status':'APPROVED','certificateNumber':cert,'validFrom':from_date,'validUntil':valid,'approvedAt':issue_date,'approvedBy':current_user().get('email'),'updatedAt':issue_date}})
        _send_member_email('membership_certificate',m,{'name':m['name'],'membershipNumber':number,'plan':plan['name'],'validUntil':str(valid)[:10],'certificateNumber':cert,'subject':'Your GLDC Membership Certificate','text':f'Your GLDC membership has been approved. Certificate {cert}.','html':f'<div style="font-family:Arial;max-width:640px;margin:auto"><h2 style="color:#8B4A18">Membership Approved</h2><p>Dear {m["name"]},</p><p>Your {"renewal" if is_renewal else "membership"} has been approved.</p><p><b>Membership No:</b> {number}<br><b>Plan:</b> {plan["name"]}<br><b>Valid:</b> {str(from_date)[:10]} to {str(valid)[:10]}<br><b>Certificate:</b> {cert}</p></div>'},[('GLDC-'+cert+'.pdf',pdf,'application/pdf')])
    else:
        status='CHANGES_REQUIRED' if decision=='REQUEST_CHANGES' else 'REJECTED'; note=str(b.get('message','Please review and update your membership details.')).strip(); db.members.update_one({'_id':m['_id']},{'$set':{'status':status,'adminMessage':note,'updatedAt':now()}})
        if m.get('paymentId') and db.payments.find_one({'id':m.get('paymentId'),'purpose':{'$in':['MEMBERSHIP_RENEWAL','MEMBERSHIP_UPGRADE']}}): db.membership_renewals.update_one({'paymentId':m.get('paymentId')},{'$set':{'status':status,'adminMessage':note,'updatedAt':now()}})
        edit_url=f'{APP_URL}/member/dashboard?edit=1'; _send_member_email('membership_changes',m,{'name':m['name'],'message':note,'editUrl':edit_url,'subject':'GLDC Membership – action required','text':f'Please review your membership details: {edit_url}','html':f'<div style="font-family:Arial;max-width:640px;margin:auto"><h2 style="color:#8B4A18">Membership details need your attention</h2><p>Dear {m["name"]},</p><p>{note}</p><p><a href="{edit_url}">EDIT MY DETAILS</a></p></div>'})
    audit('MEMBERSHIP_DECISION','member',member_id,{'decision':decision}); return jsonify(ok=True,status='ACTIVE' if decision=='APPROVE' else ('CHANGES_REQUIRED' if decision=='REQUEST_CHANGES' else 'REJECTED'))

@app.get('/api/admin/membership/recover/<receipt_code>')
@admin_required
def admin_membership_recover(receipt_code):
    p=db.payments.find_one({'receiptCode':receipt_code});
    if not p: return json_error('Payment receipt not found.',404,'RECEIPT_NOT_FOUND')
    m=db.members.find_one({'id':p.get('memberId')}); return jsonify(ok=True,payment=clean_doc(p),member=clean_doc(m) if m else None)

@app.get('/api/admin/membership/renewals')
@admin_required
def admin_membership_renewals(): return jsonify(ok=True,renewals=list_collection('membership_renewals',500))

@app.get('/api/admin/membership/certificates')
@admin_required
def admin_membership_certificates(): return jsonify(ok=True,certificates=list_collection('membership_certificates',500))

@app.get('/api/admin/email-templates')
@admin_required
def admin_email_templates(): return jsonify(ok=True,templates=list_collection('email_templates',100))

@app.post('/api/admin/email-templates')
@admin_required
def admin_email_template_save():
    b=request.get_json(silent=True) or {}; name=str(b.get('name','')).strip(); subject=str(b.get('subject','')).strip(); html=str(b.get('html','')); text=str(b.get('text',''))
    if not name or not subject or not html: return json_error('Template name, subject and HTML are required.',422,'VALIDATION_ERROR')
    doc={'name':name,'subject':subject,'html':html,'text':text,'updatedAt':now(),'updatedBy':current_user().get('email')}; db.email_templates.update_one({'name':name},{'$set':doc,'$setOnInsert':{'createdAt':now()}},upsert=True); audit('EMAIL_TEMPLATE_SAVED','email_template',name); return jsonify(ok=True)

@app.post('/api/admin/email-templates/test')
@admin_required
def admin_email_template_test():
    b=request.get_json(silent=True) or {}; to=str(b.get('to','')).strip().lower(); subject=str(b.get('subject','')).strip(); html=str(b.get('html','')); text=str(b.get('text',''))
    if '@' not in to: return json_error('Valid recipient email required.',422,'VALIDATION_ERROR')
    send_email(to,subject,text,html); audit('EMAIL_TEMPLATE_TEST_SENT','email_template',str(b.get('name',''))); return jsonify(ok=True)

@app.get('/api/public/media/<file_id>')
def public_media(file_id):
    if db is None: return Response('Not found',404)
    media=db.media.find_one({'driveFileId':file_id,'status':'PUBLISHED'})
    if not media: return Response('Not found',404)
    try:
        data=drive_download_bytes_by_id(file_id); meta=drive_metadata(file_id)[2]; return Response(data,mimetype=meta.get('mimeType','image/jpeg'),headers={'Cache-Control':'public, max-age=3600'})
    except Exception: return Response('Not found',404)

@app.get('/locations')
def public_locations(): return render_template('locations.html',title='GLDC Offices & Locations')

@app.get('/api/public/locations')
def public_locations_api(): return jsonify(ok=True,locations=list_collection('office_locations',100))

@app.get('/api/admin/media')
@admin_required
def admin_media(): return jsonify(ok=True,media=list_collection('media',300))

@app.post('/api/admin/media')
@admin_required
def admin_media_save():
    b=request.get_json(silent=True) or {}; slot=str(b.get('slot','')).strip(); drive_id=str(b.get('driveFileId','')).strip(); name=str(b.get('name','')).strip()
    if not slot or not drive_id: return json_error('Media slot and Google Drive file ID are required.',422,'VALIDATION_ERROR')
    doc={'id':make_id('MED'),'slot':slot,'name':name or slot,'driveFileId':drive_id,'status':str(b.get('status','PUBLISHED')).upper(),'alt':str(b.get('alt','GLDC image')).strip(),'position':str(b.get('position','center center')).strip(),'updatedAt':now(),'updatedBy':current_user().get('email')}
    db.media.update_one({'slot':slot},{'$set':doc,'$setOnInsert':{'createdAt':now()}},upsert=True); audit('MEDIA_SLOT_UPDATED','media',slot,{'driveFileId':drive_id}); return jsonify(ok=True,media=clean_doc(doc))

@app.patch('/api/admin/media/<media_id>')
@admin_required
def admin_media_update(media_id):
    b=request.get_json(silent=True) or {}; allowed=['slot','name','driveFileId','status','alt','position']; u={k:b[k] for k in allowed if k in b}; u['updatedAt']=now(); db.media.update_one({'id':media_id},{'$set':u}); audit('MEDIA_UPDATED','media',media_id,u); return jsonify(ok=True)

@app.get('/api/admin/whatsapp')
@admin_required
def admin_whatsapp():
    x=db.settings.find_one({'key':'whatsapp'},{'_id':0}) or {'key':'whatsapp','enabled':False,'phone':'','url':'','message':'Hello GLDC, I would like to make an enquiry.','label':'WhatsApp'}; return jsonify(ok=True,whatsapp=x)

@app.post('/api/admin/whatsapp')
@admin_required
def admin_whatsapp_save():
    b=request.get_json(silent=True) or {}; phone=re.sub(r'\\D','',str(b.get('phone',''))); url=str(b.get('url','')).strip(); enabled=bool(b.get('enabled',True)); msg=str(b.get('message','Hello GLDC, I would like to make an enquiry.')).strip()
    if phone and not url: url='https://wa.me/'+phone+'?text='+requests.utils.quote(msg)
    doc={'key':'whatsapp','enabled':enabled,'phone':phone,'url':url,'message':msg,'label':str(b.get('label','WhatsApp')).strip(),'updatedAt':now(),'updatedBy':current_user().get('email')}; db.settings.update_one({'key':'whatsapp'},{'$set':doc},upsert=True); audit('WHATSAPP_SETTINGS_UPDATED','settings','whatsapp'); return jsonify(ok=True,whatsapp=clean_doc(doc))

@app.get('/api/admin/locations')
@admin_required
def admin_locations(): return jsonify(ok=True,locations=list_collection('office_locations',100))

@app.post('/api/admin/locations')
@admin_required
def admin_location_create():
    b=request.get_json(silent=True) or {}; name=str(b.get('name','')).strip(); address=str(b.get('address','')).strip()
    try: lat=float(b.get('lat')) ; lng=float(b.get('lng'))
    except (TypeError,ValueError): return json_error('Select the office position on the basemap before saving.',422,'MAP_LOCATION_REQUIRED')
    if not name or not address or not (-90<=lat<=90 and -180<=lng<=180): return json_error('Office name, address and a valid basemap position are required.',422,'VALIDATION_ERROR')
    primary=str(b.get('primary','NO')).upper() in ('YES','TRUE','1')
    zoom=int(b.get('zoom',15) or 15); zoom=max(10,min(19,zoom))
    status=str(b.get('status','PUBLISHED')).upper(); status=status if status in ('PUBLISHED','DRAFT','ARCHIVED') else 'DRAFT'
    doc={'id':make_id('OFF'),'name':name,'address':address,'description':str(b.get('description','')).strip(),'phone':str(b.get('phone','')).strip(),'email':str(b.get('email','')).strip(),'hours':str(b.get('hours','')).strip(),'lat':lat,'lng':lng,'zoom':zoom,'primary':primary,'status':status,'createdAt':now(),'updatedAt':now()}; db.office_locations.insert_one(doc); audit('OFFICE_LOCATION_CREATED','office',doc['id']); return jsonify(ok=True,location=clean_doc(doc)),201

@app.patch('/api/admin/locations/<location_id>')
@admin_required
def admin_location_update(location_id):
    b=request.get_json(silent=True) or {}; allowed=['name','address','description','phone','email','hours','lat','lng','zoom','primary','status']; u={k:b[k] for k in allowed if k in b}; u['updatedAt']=now(); db.office_locations.update_one({'id':location_id},{'$set':u}); audit('OFFICE_LOCATION_UPDATED','office',location_id,u); return jsonify(ok=True)

@app.get('/invoice/<invoice_number>')
def invoice_verify_page(invoice_number):
    inv=db.invoices.find_one({'invoiceNumber':invoice_number},{'_id':0,'invoiceNumber':1,'status':1,'issuedAt':1,'dueDate':1}) if db is not None else None
    if not inv: return render_template('404.html',title='Invoice not found'),404
    return render_template('invoice_verify.html',title='Invoice Verification',invoice=clean_doc(inv))

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
    db.membership_plans.create_index([('status',ASCENDING),('sortOrder',ASCENDING)])
    db.membership_certificates.create_index([('certificateNumber',ASCENDING)], unique=True)
    db.membership_certificates.create_index([('memberId',ASCENDING),('issuedAt',DESCENDING)])
    db.membership_renewals.create_index([('memberId',ASCENDING),('createdAt',DESCENDING)])
    db.membership_renewals.create_index([('paymentId',ASCENDING)], unique=True, sparse=True)
    db.members.create_index([('recreateToken',ASCENDING)], unique=True, sparse=True)
    db.payments.create_index([('mpesaReceiptNumber',ASCENDING)], unique=True, sparse=True)
    db.member_documents.create_index([('memberId',ASCENDING),('category',ASCENDING),('createdAt',DESCENDING)])
    db.email_templates.create_index([('name',ASCENDING)], unique=True)
    db.media.create_index([('slot',ASCENDING)], unique=True)
    db.office_locations.create_index([('status',ASCENDING),('primary',DESCENDING)])
    plans=[
        {'slug':'monthly','name':'Monthly Membership','price':1000,'months':1,'billingCycle':'MONTHLY','description':'Flexible month-to-month GLDC membership.','sortOrder':1},
        {'slug':'yearly','name':'Annual Membership','price':10000,'months':12,'billingCycle':'YEARLY','description':'Best value annual GLDC membership.','sortOrder':2},
    ]
    for pl in plans: db.membership_plans.update_one({'slug':pl['slug']},{'$setOnInsert':{'id':'PLAN-'+pl['slug'].upper(),'status':'ACTIVE','currency':'KES','createdAt':now()},'$set':{**pl,'updatedAt':now()}},upsert=True)
    email_defaults={
      'membership_payment_receipt':{'subject':'GLDC Membership Payment Receipt – {{receiptCode}}','text':'Dear {{name}}, your payment of KES {{amount}} has been received. Receipt {{receiptCode}}.','html':'<div style=\"font-family:Arial;max-width:640px;margin:auto\"><div style=\"padding:24px;background:#8B4A18;color:#fff\"><h2>GLDC Membership</h2></div><div style=\"padding:24px\"><p>Dear <b>{{name}}</b>,</p><p>We have received your payment for <b>{{plan}}</b>.</p><p><b>Amount:</b> KES {{amount}}<br><b>Receipt:</b> {{receiptCode}}</p><p>Your application is now awaiting membership review. The receipt PDF is attached.</p></div></div>'},
      'membership_changes':{'subject':'GLDC Membership – action required','text':'Dear {{name}}, {{message}} Please use {{editUrl}} to update your details.','html':'<div style=\"font-family:Arial;max-width:640px;margin:auto\"><h2 style=\"color:#8B4A18\">Membership details need your attention</h2><p>Dear {{name}},</p><p>{{message}}</p><p><a href=\"{{editUrl}}\" style=\"background:#8B4A18;color:#fff;padding:12px 18px;text-decoration:none\">EDIT MY DETAILS</a></p></div>'},
      'membership_action_request':{'subject':'GLDC Membership – action required','text':'Dear {{name}}, {{message}} Requested fields: {{requestedFields}}. Deadline: {{deadline}}. Update your details: {{editUrl}}','html':'<div style="font-family:Arial;max-width:640px;margin:auto"><div style="padding:22px;background:#8B4A18;color:#fff"><h2>GLDC Membership – action required</h2></div><div style="padding:24px"><p>Dear <b>{{name}}</b>,</p><p>{{message}}</p><p><b>Requested fields:</b> {{requestedFields}}<br><b>Deadline:</b> {{deadline}}</p><p><a href="{{editUrl}}" style="background:#8B4A18;color:#fff;padding:12px 18px;text-decoration:none">OPEN MEMBER PORTAL</a></p></div></div>'},
      'membership_payment_failed':{'subject':'GLDC Membership – payment needs attention','text':'Dear {{name}}, your membership payment could not be completed. {{message}} Continue: {{resumeUrl}}. If you already paid, submit the M-Pesa code for GLDC verification.','html':'<div style=\"font-family:Arial;max-width:640px;margin:auto\"><div style=\"padding:22px;background:#8B4A18;color:#fff\"><h2>GLDC Membership Payment</h2></div><div style=\"padding:24px\"><p>Dear <b>{{name}}</b>,</p><p>{{message}}</p><p><b>If you already paid:</b> do not pay again. Open your Member Portal and submit the M-Pesa transaction code shown in your M-Pesa message. GLDC will verify the transaction before approving your membership.</p><p><a href=\"{{resumeUrl}}\" style=\"background:#8B4A18;color:#fff;padding:12px 18px;text-decoration:none\">CONTINUE MY APPLICATION</a></p><p>If you need to remove an unfinished application and start again, <a href=\"{{recreateUrl}}\">use the secure restart link</a>. This option is unavailable after a payment is under review.</p></div></div>'},
      'membership_payment_verified':{'subject':'GLDC Membership – payment verified','text':'Dear {{name}}, your M-Pesa payment {{mpesaCode}} has been verified by GLDC. Your application is now awaiting final approval.','html':'<div style=\"font-family:Arial;max-width:640px;margin:auto\"><h2 style=\"color:#8B4A18\">Payment verified ✓</h2><p>Dear <b>{{name}}</b>,</p><p>Your M-Pesa payment <b>{{mpesaCode}}</b> for <b>{{plan}}</b> has been verified by GLDC.</p><p>Your application is now awaiting final membership approval. Please do not make another payment.</p><p><a href=\"{{APP_URL}}/member/dashboard\">OPEN MEMBER PORTAL</a></p></div>'},
      'membership_certificate':{'subject':'Your GLDC Membership Certificate – {{certificateNumber}}','text':'Congratulations {{name}}. Your GLDC membership certificate is attached.','html':'<div style=\"font-family:Arial;max-width:640px;margin:auto\"><div style=\"padding:24px;background:#8B4A18;color:#fff\"><h2>Membership Approved</h2></div><div style=\"padding:24px\"><p>Dear <b>{{name}}</b>,</p><p>Your GLDC membership has been approved. Your certificate is attached.</p><p><b>Plan:</b> {{plan}}<br><b>Membership No:</b> {{membershipNumber}}<br><b>Valid until:</b> {{validUntil}}</p></div></div>'}
    }
    for name,t in email_defaults.items(): db.email_templates.update_one({'name':name},{'$setOnInsert':{'name':name,'createdAt':now()},'$set':t},upsert=True)
    db.settings.update_one({'key':'membership_policy'},{'$setOnInsert':{'key':'membership_policy','renewalWindowDays':30,'abandonEmailHours':MEMBERSHIP_ABANDON_EMAIL_HOURS,'abandonPaymentHours':MEMBERSHIP_ABANDON_PAYMENT_HOURS,'abandonRenotifyHours':MEMBERSHIP_ABANDON_RENOTIFY_HOURS,'createdAt':now()}},upsert=True)
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
