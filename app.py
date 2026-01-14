import os
import io
import csv
import json
import smtplib
import cloudinary
import cloudinary.uploader
import anthropic
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
from email.mime.text import MIMEText
from datetime import datetime, timedelta
from functools import wraps
from flask import Flask, render_template, request, jsonify, redirect, url_for, session, send_from_directory, Response
from flask_sqlalchemy import SQLAlchemy
from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse
from werkzeug.utils import secure_filename
import secrets

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', secrets.token_hex(32))
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///sms_platform.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max

# Cloudinary config (for persistent file storage)
cloudinary.config(
    cloud_name=os.environ.get('CLOUDINARY_CLOUD_NAME'),
    api_key=os.environ.get('CLOUDINARY_API_KEY'),
    api_secret=os.environ.get('CLOUDINARY_API_SECRET')
)

# Anthropic AI config
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY')

# Twilio config
TWILIO_ACCOUNT_SID = os.environ.get('TWILIO_ACCOUNT_SID')
TWILIO_AUTH_TOKEN = os.environ.get('TWILIO_AUTH_TOKEN')
TWILIO_PHONE_NUMBER = os.environ.get('TWILIO_PHONE_NUMBER')

# Notification config
NOTIFICATION_EMAIL = os.environ.get('NOTIFICATION_EMAIL')
NOTIFICATION_SMS = os.environ.get('NOTIFICATION_SMS')
SMTP_SERVER = os.environ.get('SMTP_SERVER', 'smtp.gmail.com')
SMTP_PORT = int(os.environ.get('SMTP_PORT', 587))
SMTP_USER = os.environ.get('SMTP_USER')
SMTP_PASSWORD = os.environ.get('SMTP_PASSWORD')

# App auth
APP_USERNAME = os.environ.get('APP_USERNAME', 'admin')
APP_PASSWORD = os.environ.get('APP_PASSWORD', 'changeme')

# App base URL for media
APP_BASE_URL = os.environ.get('APP_BASE_URL', 'http://localhost:5000')

db = SQLAlchemy(app)

# Association table for partner products
partner_products = db.Table('partner_products',
    db.Column('partner_id', db.Integer, db.ForeignKey('partner.id'), primary_key=True),
    db.Column('product_id', db.Integer, db.ForeignKey('product.id'), primary_key=True)
)

# Association table for partner tags
partner_tags = db.Table('partner_tags',
    db.Column('partner_id', db.Integer, db.ForeignKey('partner.id'), primary_key=True),
    db.Column('tag_id', db.Integer, db.ForeignKey('tag.id'), primary_key=True)
)

# Models
class Partner(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    first_name = db.Column(db.String(50), nullable=False)
    last_name = db.Column(db.String(50))
    company = db.Column(db.String(100))
    phone = db.Column(db.String(20), nullable=False, unique=True)
    region_id = db.Column(db.Integer, db.ForeignKey('region.id'))
    tsd_id = db.Column(db.Integer, db.ForeignKey('tsd.id'))
    notes = db.Column(db.Text)
    opted_out = db.Column(db.Boolean, default=False)
    pinned = db.Column(db.Boolean, default=False)
    archived = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_contacted = db.Column(db.DateTime)
    
    region = db.relationship('Region', backref='partners')
    tsd = db.relationship('TSD', backref='partners')
    products = db.relationship('Product', secondary=partner_products, backref='partners')
    tags = db.relationship('Tag', secondary=partner_tags, backref='partners')
    messages = db.relationship('Message', backref='partner', lazy=True)
    
    @property
    def full_name(self):
        if self.last_name:
            return f"{self.first_name} {self.last_name}"
        return self.first_name
    
    @property
    def is_new(self):
        return self.last_contacted is None

class Tag(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), nullable=False, unique=True)
    color = db.Column(db.String(20), default='accent')  # accent, warning, error, etc.
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class Region(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), nullable=False, unique=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class TSD(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False, unique=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class Product(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False, unique=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class Message(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    partner_id = db.Column(db.Integer, db.ForeignKey('partner.id'), nullable=False)
    direction = db.Column(db.String(10))  # 'inbound' or 'outbound'
    body = db.Column(db.Text)
    media_url = db.Column(db.String(500))  # For MMS
    media_type = db.Column(db.String(50))  # image, video, audio
    status = db.Column(db.String(20))
    twilio_sid = db.Column(db.String(50))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class MessageTemplate(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    body = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class ScheduledMessage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    message_template = db.Column(db.Text, nullable=False)
    partner_ids = db.Column(db.Text, nullable=False)  # JSON array
    media_url = db.Column(db.String(500))
    media_type = db.Column(db.String(50))
    scheduled_time = db.Column(db.DateTime, nullable=False)
    status = db.Column(db.String(20), default='pending')  # pending, sent, cancelled
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class AIKnowledge(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    category = db.Column(db.String(50), nullable=False)  # products, objections, faq, tone, general
    title = db.Column(db.String(200), nullable=False)
    content = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class AISettings(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    setting_key = db.Column(db.String(50), nullable=False, unique=True)
    setting_value = db.Column(db.Text)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class UserSettings(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    onboarding_step = db.Column(db.Integer, default=0)  # 0=not started, 6=complete
    calendar_link = db.Column(db.String(500))
    custom_password = db.Column(db.String(200))  # If set, overrides env var
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

def get_user_settings():
    """Get or create user settings"""
    settings = UserSettings.query.first()
    if not settings:
        settings = UserSettings()
        db.session.add(settings)
        db.session.commit()
    return settings

# Auth decorator
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

# Twilio client
def get_twilio_client():
    if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN:
        return Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    return None

# Personalize message
def personalize_message(template, partner):
    message = template.replace('{{first_name}}', partner.first_name or '')
    message = message.replace('{{last_name}}', partner.last_name or '')
    message = message.replace('{{name}}', partner.full_name or '')
    message = message.replace('{{company}}', partner.company or '')
    if partner.region:
        message = message.replace('{{region}}', partner.region.name)
    else:
        message = message.replace('{{region}}', '')
    if partner.tsd:
        message = message.replace('{{tsd}}', partner.tsd.name)
    else:
        message = message.replace('{{tsd}}', '')
    return message

# Send SMS/MMS
def send_sms(to_phone, body, partner_id=None, media_url=None, media_type=None):
    client = get_twilio_client()
    if not client:
        return {'success': False, 'error': 'Twilio not configured'}
    
    # Check opt-out
    if partner_id:
        partner = Partner.query.get(partner_id)
        if partner and partner.opted_out:
            return {'success': False, 'error': 'Partner has opted out'}
    
    try:
        params = {
            'body': body,
            'from_': TWILIO_PHONE_NUMBER,
            'to': to_phone
        }
        
        if media_url:
            params['media_url'] = [media_url]
        
        message = client.messages.create(**params)
        
        # Log message and update last_contacted
        if partner_id:
            msg = Message(
                partner_id=partner_id,
                direction='outbound',
                body=body,
                media_url=media_url,
                media_type=media_type,
                status=message.status,
                twilio_sid=message.sid
            )
            db.session.add(msg)
            
            partner = Partner.query.get(partner_id)
            if partner:
                partner.last_contacted = datetime.utcnow()
            
            db.session.commit()
        
        return {'success': True, 'sid': message.sid, 'status': message.status}
    except Exception as e:
        return {'success': False, 'error': str(e)}

# Send notification
def send_notification(partner_name, message_body):
    if NOTIFICATION_EMAIL and SMTP_USER and SMTP_PASSWORD:
        try:
            msg = MIMEText(f"Reply from {partner_name}:\n\n{message_body}\n\nOpen app to respond.")
            msg['Subject'] = f"SMS Reply from {partner_name}"
            msg['From'] = SMTP_USER
            msg['To'] = NOTIFICATION_EMAIL
            
            with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
                server.starttls()
                server.login(SMTP_USER, SMTP_PASSWORD)
                server.send_message(msg)
        except Exception as e:
            print(f"Email notification failed: {e}")
    
    if NOTIFICATION_SMS and TWILIO_PHONE_NUMBER:
        client = get_twilio_client()
        if client:
            try:
                client.messages.create(
                    body=f"Reply from {partner_name}: {message_body[:100]}",
                    from_=TWILIO_PHONE_NUMBER,
                    to=NOTIFICATION_SMS
                )
            except Exception as e:
                print(f"SMS notification failed: {e}")

# Routes
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        
        # Check custom password first, then fall back to env var
        user_settings = UserSettings.query.first()
        valid_password = APP_PASSWORD
        if user_settings and user_settings.custom_password:
            valid_password = user_settings.custom_password
        
        if username == APP_USERNAME and password == valid_password:
            session['logged_in'] = True
            
            # Check if onboarding needed
            settings = get_user_settings()
            if settings.onboarding_step < 6:
                return redirect(url_for('onboarding'))
            
            return redirect(url_for('index'))
        return render_template('login.html', error='Invalid credentials')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    return redirect(url_for('login'))

@app.route('/onboarding')
@login_required
def onboarding():
    settings = get_user_settings()
    return render_template('onboarding.html', current_step=settings.onboarding_step)

@app.route('/')
@login_required
def index():
    settings = get_user_settings()
    return render_template('index.html', onboarding_complete=(settings.onboarding_step >= 6), onboarding_step=settings.onboarding_step)

@app.route('/partners')
@login_required
def partners():
    return render_template('partners.html')

@app.route('/inbox')
@login_required
def inbox():
    return render_template('inbox.html')

@app.route('/broadcast')
@login_required
def broadcast():
    return render_template('broadcast.html')

@app.route('/settings')
@login_required
def settings():
    return render_template('settings.html')

# API: Partners
@app.route('/api/partners', methods=['GET', 'POST'])
@login_required
def api_partners():
    if request.method == 'POST':
        data = request.json
        
        # Validate phone
        phone = data.get('phone', '').strip()
        if not phone.startswith('+'):
            phone = '+1' + phone.replace('-', '').replace(' ', '').replace('(', '').replace(')', '')
        
        # Check duplicate
        existing = Partner.query.filter_by(phone=phone).first()
        if existing:
            return jsonify({'error': 'Phone number already exists'}), 400
        
        partner = Partner(
            first_name=data['first_name'],
            last_name=data.get('last_name'),
            company=data.get('company'),
            phone=phone,
            region_id=data.get('region_id') or None,
            tsd_id=data.get('tsd_id') or None,
            notes=data.get('notes')
        )
        
        if data.get('product_ids'):
            products = Product.query.filter(Product.id.in_(data['product_ids'])).all()
            partner.products = products
        
        if data.get('tag_ids'):
            tags = Tag.query.filter(Tag.id.in_(data['tag_ids'])).all()
            partner.tags = tags
        
        db.session.add(partner)
        db.session.commit()
        return jsonify({'success': True, 'id': partner.id})
    
    # GET with filtering and search
    query = Partner.query
    
    # Search
    search = request.args.get('search', '').strip()
    if search:
        search_term = f"%{search}%"
        query = query.filter(
            db.or_(
                Partner.first_name.ilike(search_term),
                Partner.last_name.ilike(search_term),
                Partner.company.ilike(search_term),
                Partner.phone.ilike(search_term)
            )
        )
    
    # Filter by region
    region_ids = request.args.getlist('region_id')
    if region_ids:
        query = query.filter(Partner.region_id.in_(region_ids))
    
    # Filter by TSD
    tsd_ids = request.args.getlist('tsd_id')
    if tsd_ids:
        query = query.filter(Partner.tsd_id.in_(tsd_ids))
    
    # Filter by product
    product_ids = request.args.getlist('product_id')
    if product_ids:
        query = query.filter(Partner.products.any(Product.id.in_(product_ids)))
    
    # Filter by tag
    tag_ids = request.args.getlist('tag_id')
    if tag_ids:
        query = query.filter(Partner.tags.any(Tag.id.in_(tag_ids)))
    
    # Filter new partners only
    if request.args.get('new_only') == 'true':
        query = query.filter(Partner.last_contacted.is_(None))
    
    partners = query.order_by(Partner.company).all()
    
    return jsonify([{
        'id': p.id,
        'first_name': p.first_name,
        'last_name': p.last_name,
        'full_name': p.full_name,
        'company': p.company,
        'phone': p.phone,
        'region_id': p.region_id,
        'region': p.region.name if p.region else None,
        'tsd_id': p.tsd_id,
        'tsd': p.tsd.name if p.tsd else None,
        'products': [{'id': prod.id, 'name': prod.name} for prod in p.products],
        'tags': [{'id': tag.id, 'name': tag.name, 'color': tag.color} for tag in p.tags],
        'notes': p.notes,
        'opted_out': p.opted_out,
        'is_new': p.is_new,
        'last_contacted': p.last_contacted.isoformat() if p.last_contacted else None,
        'created_at': p.created_at.isoformat()
    } for p in partners])

@app.route('/api/partners/<int:id>', methods=['GET', 'PUT', 'DELETE'])
@login_required
def api_partner(id):
    partner = Partner.query.get_or_404(id)
    
    if request.method == 'DELETE':
        # Delete associated messages first
        Message.query.filter_by(partner_id=id).delete()
        db.session.delete(partner)
        db.session.commit()
        return jsonify({'success': True})
    
    if request.method == 'PUT':
        data = request.json
        partner.first_name = data.get('first_name', partner.first_name)
        partner.last_name = data.get('last_name', partner.last_name)
        partner.company = data.get('company', partner.company)
        partner.phone = data.get('phone', partner.phone)
        partner.region_id = data.get('region_id') or None
        partner.tsd_id = data.get('tsd_id') or None
        partner.notes = data.get('notes', partner.notes)
        
        if 'product_ids' in data:
            products = Product.query.filter(Product.id.in_(data['product_ids'])).all()
            partner.products = products
        
        if 'tag_ids' in data:
            tags = Tag.query.filter(Tag.id.in_(data['tag_ids'])).all()
            partner.tags = tags
        
        db.session.commit()
        return jsonify({'success': True})
    
    return jsonify({
        'id': partner.id,
        'first_name': partner.first_name,
        'last_name': partner.last_name,
        'full_name': partner.full_name,
        'company': partner.company,
        'phone': partner.phone,
        'region_id': partner.region_id,
        'region': partner.region.name if partner.region else None,
        'tsd_id': partner.tsd_id,
        'tsd': partner.tsd.name if partner.tsd else None,
        'products': [{'id': prod.id, 'name': prod.name} for prod in partner.products],
        'tags': [{'id': tag.id, 'name': tag.name, 'color': tag.color} for tag in partner.tags],
        'notes': partner.notes,
        'opted_out': partner.opted_out,
        'is_new': partner.is_new,
        'last_contacted': partner.last_contacted.isoformat() if partner.last_contacted else None
    })

# API: Import CSV
@app.route('/api/partners/import', methods=['POST'])
@login_required
def api_import_partners():
    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400
    
    file = request.files['file']
    if not file.filename.endswith('.csv'):
        return jsonify({'error': 'Must be a CSV file'}), 400
    
    try:
        content = file.read().decode('utf-8')
        reader = csv.DictReader(io.StringIO(content))
        
        imported = 0
        skipped = 0
        errors = []
        
        for row in reader:
            try:
                # Get phone and clean it
                phone = row.get('phone', row.get('Phone', '')).strip()
                if not phone:
                    skipped += 1
                    continue
                
                if not phone.startswith('+'):
                    phone = '+1' + phone.replace('-', '').replace(' ', '').replace('(', '').replace(')', '')
                
                # Check if exists
                if Partner.query.filter_by(phone=phone).first():
                    skipped += 1
                    continue
                
                # Get other fields (flexible column names)
                first_name = row.get('first_name', row.get('First Name', row.get('FirstName', ''))).strip()
                last_name = row.get('last_name', row.get('Last Name', row.get('LastName', ''))).strip()
                company = row.get('company', row.get('Company', '')).strip()
                
                if not first_name:
                    skipped += 1
                    continue
                
                partner = Partner(
                    first_name=first_name,
                    last_name=last_name or None,
                    company=company or None,
                    phone=phone
                )
                db.session.add(partner)
                imported += 1
                
            except Exception as e:
                errors.append(str(e))
                skipped += 1
        
        db.session.commit()
        return jsonify({
            'success': True,
            'imported': imported,
            'skipped': skipped,
            'errors': errors[:5]  # Return first 5 errors
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 400

# API: Export CSV
@app.route('/api/partners/export')
@login_required
def api_export_partners():
    partners = Partner.query.order_by(Partner.company).all()
    
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['first_name', 'last_name', 'company', 'phone', 'region', 'tsd', 'products', 'notes', 'last_contacted'])
    
    for p in partners:
        writer.writerow([
            p.first_name,
            p.last_name or '',
            p.company or '',
            p.phone,
            p.region.name if p.region else '',
            p.tsd.name if p.tsd else '',
            ', '.join([prod.name for prod in p.products]),
            p.notes or '',
            p.last_contacted.isoformat() if p.last_contacted else ''
        ])
    
    output.seek(0)
    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename=partners.csv'}
    )

# API: Regions
@app.route('/api/regions', methods=['GET', 'POST'])
@login_required
def api_regions():
    if request.method == 'POST':
        data = request.json
        region = Region(name=data['name'])
        db.session.add(region)
        db.session.commit()
        return jsonify({'success': True, 'id': region.id})
    
    regions = Region.query.order_by(Region.name).all()
    return jsonify([{'id': r.id, 'name': r.name} for r in regions])

@app.route('/api/regions/<int:id>', methods=['PUT', 'DELETE'])
@login_required
def api_region(id):
    region = Region.query.get_or_404(id)
    
    if request.method == 'DELETE':
        db.session.delete(region)
        db.session.commit()
        return jsonify({'success': True})
    
    if request.method == 'PUT':
        data = request.json
        region.name = data.get('name', region.name)
        db.session.commit()
        return jsonify({'success': True})

# API: TSDs
@app.route('/api/tsds', methods=['GET', 'POST'])
@login_required
def api_tsds():
    if request.method == 'POST':
        data = request.json
        tsd = TSD(name=data['name'])
        db.session.add(tsd)
        db.session.commit()
        return jsonify({'success': True, 'id': tsd.id})
    
    tsds = TSD.query.order_by(TSD.name).all()
    return jsonify([{'id': t.id, 'name': t.name} for t in tsds])

@app.route('/api/tsds/<int:id>', methods=['PUT', 'DELETE'])
@login_required
def api_tsd(id):
    tsd = TSD.query.get_or_404(id)
    
    if request.method == 'DELETE':
        db.session.delete(tsd)
        db.session.commit()
        return jsonify({'success': True})
    
    if request.method == 'PUT':
        data = request.json
        tsd.name = data.get('name', tsd.name)
        db.session.commit()
        return jsonify({'success': True})

# API: Products
@app.route('/api/products', methods=['GET', 'POST'])
@login_required
def api_products():
    if request.method == 'POST':
        data = request.json
        product = Product(name=data['name'])
        db.session.add(product)
        db.session.commit()
        return jsonify({'success': True, 'id': product.id})
    
    products = Product.query.order_by(Product.name).all()
    return jsonify([{'id': p.id, 'name': p.name} for p in products])

@app.route('/api/products/<int:id>', methods=['PUT', 'DELETE'])
@login_required
def api_product(id):
    product = Product.query.get_or_404(id)
    
    if request.method == 'DELETE':
        db.session.delete(product)
        db.session.commit()
        return jsonify({'success': True})
    
    if request.method == 'PUT':
        data = request.json
        product.name = data.get('name', product.name)
        db.session.commit()
        return jsonify({'success': True})
    return jsonify({'success': True})

# API: Tags
@app.route('/api/tags', methods=['GET', 'POST'])
@login_required
def api_tags():
    if request.method == 'POST':
        data = request.json
        tag = Tag(name=data['name'], color=data.get('color', 'accent'))
        db.session.add(tag)
        db.session.commit()
        return jsonify({'success': True, 'id': tag.id})
    
    tags = Tag.query.order_by(Tag.name).all()
    return jsonify([{'id': t.id, 'name': t.name, 'color': t.color} for t in tags])

@app.route('/api/tags/<int:id>', methods=['PUT', 'DELETE'])
@login_required
def api_tag(id):
    tag = Tag.query.get_or_404(id)
    
    if request.method == 'DELETE':
        db.session.delete(tag)
        db.session.commit()
        return jsonify({'success': True})
    
    if request.method == 'PUT':
        data = request.json
        tag.name = data.get('name', tag.name)
        tag.color = data.get('color', tag.color)
        db.session.commit()
        return jsonify({'success': True})
    return jsonify({'success': True})

# API: Message Search
@app.route('/api/messages/search')
@login_required
def api_message_search():
    query = request.args.get('q', '').strip()
    if not query or len(query) < 2:
        return jsonify([])
    
    search_term = f"%{query}%"
    messages = Message.query.filter(Message.body.ilike(search_term)).order_by(Message.created_at.desc()).limit(50).all()
    
    results = []
    for m in messages:
        partner = Partner.query.get(m.partner_id)
        results.append({
            'id': m.id,
            'partner_id': m.partner_id,
            'partner_name': partner.full_name if partner else 'Unknown',
            'direction': m.direction,
            'body': m.body,
            'created_at': m.created_at.isoformat()
        })
    
    return jsonify(results)

# AI: Get Anthropic client
def get_ai_client():
    if ANTHROPIC_API_KEY:
        return anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    return None

# AI: Get all knowledge for context
def get_ai_knowledge_context():
    knowledge_items = AIKnowledge.query.all()
    if not knowledge_items:
        return ""
    
    context = "\n\n=== BUSINESS KNOWLEDGE BASE ===\n"
    
    # Group by category
    categories = {}
    for item in knowledge_items:
        if item.category not in categories:
            categories[item.category] = []
        categories[item.category].append(item)
    
    category_labels = {
        'products': 'PRODUCTS & SERVICES',
        'objections': 'COMMON OBJECTIONS & RESPONSES',
        'faq': 'FREQUENTLY ASKED QUESTIONS',
        'tone': 'COMMUNICATION STYLE & TONE',
        'general': 'GENERAL BUSINESS INFO'
    }
    
    for cat, items in categories.items():
        label = category_labels.get(cat, cat.upper())
        context += f"\n--- {label} ---\n"
        for item in items:
            context += f"\n**{item.title}**\n{item.content}\n"
    
    return context

# AI: Get settings
def get_ai_setting(key, default=''):
    setting = AISettings.query.filter_by(setting_key=key).first()
    return setting.setting_value if setting else default

# AI: Generate reply suggestions based on conversation
@app.route('/api/ai/suggestions/<int:partner_id>', methods=['POST'])
@login_required
def api_ai_suggestions(partner_id):
    client = get_ai_client()
    if not client:
        return jsonify({'error': 'AI not configured. Add ANTHROPIC_API_KEY to environment.'}), 400
    
    partner = Partner.query.get_or_404(partner_id)
    messages = Message.query.filter_by(partner_id=partner_id).order_by(Message.created_at.desc()).limit(10).all()
    messages.reverse()  # Chronological order
    
    if not messages:
        return jsonify({'suggestions': ['Hi! How can I help you today?', 'Thanks for reaching out!', 'Let me know if you have any questions.']})
    
    # Build conversation context
    conversation = []
    for m in messages:
        role = 'Partner' if m.direction == 'inbound' else 'You'
        conversation.append(f"{role}: {m.body}")
    
    conversation_text = '\n'.join(conversation)
    
    # Get business knowledge
    knowledge = get_ai_knowledge_context()
    
    prompt = f"""You are helping a Strategic Partner Manager at SilverSky (a cybersecurity company) respond to SMS messages from channel partners.
{knowledge}

Partner info:
- Name: {partner.full_name}
- Company: {partner.company or 'Unknown'}
- Region: {partner.region.name if partner.region else 'Unknown'}
- Products interested in: {', '.join([p.name for p in partner.products]) or 'Unknown'}
- Notes: {partner.notes or 'None'}

Recent conversation:
{conversation_text}

Generate exactly 3 short, professional SMS reply suggestions (under 160 characters each) that the partner manager could send next. Make them contextually relevant to the conversation. Use the business knowledge above to give accurate, informed responses. Be helpful, friendly, and action-oriented.

Return ONLY a JSON array of 3 strings, no other text. Example: ["Reply 1", "Reply 2", "Reply 3"]"""

    try:
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}]
        )
        
        # Parse the response
        response_text = response.content[0].text.strip()
        suggestions = json.loads(response_text)
        
        return jsonify({'suggestions': suggestions[:3]})
    except json.JSONDecodeError:
        # If JSON parsing fails, return defaults
        return jsonify({'suggestions': ['Thanks for the update!', 'Let me look into that for you.', 'Can we schedule a quick call?']})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# AI: Generate message based on prompt
@app.route('/api/ai/compose', methods=['POST'])
@login_required
def api_ai_compose():
    client = get_ai_client()
    if not client:
        return jsonify({'error': 'AI not configured. Add ANTHROPIC_API_KEY to environment.'}), 400
    
    data = request.json
    prompt_text = data.get('prompt', '')
    partner_id = data.get('partner_id')
    
    if not prompt_text:
        return jsonify({'error': 'Please provide a prompt'}), 400
    
    # Get partner context if provided
    partner_context = ""
    if partner_id:
        partner = Partner.query.get(partner_id)
        if partner:
            partner_context = f"""
Partner info:
- Name: {partner.full_name}
- Company: {partner.company or 'Unknown'}
- Region: {partner.region.name if partner.region else 'Unknown'}
- TSD: {partner.tsd.name if partner.tsd else 'Unknown'}
- Products: {', '.join([p.name for p in partner.products]) or 'Unknown'}
- Notes: {partner.notes or 'None'}
"""
    
    # Get business knowledge
    knowledge = get_ai_knowledge_context()
    
    prompt = f"""You are helping a Strategic Partner Manager at SilverSky (a cybersecurity company) write SMS messages to channel partners.
{knowledge}
{partner_context}

User request: {prompt_text}

Write a professional, friendly SMS message (under 160 characters if possible, max 320 characters). Use the business knowledge above to be accurate and specific. Use {{{{first_name}}}} if you want to personalize with the partner's name.

Return ONLY the message text, no quotes or explanation."""

    try:
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}]
        )
        
        message = response.content[0].text.strip()
        # Remove quotes if present
        if message.startswith('"') and message.endswith('"'):
            message = message[1:-1]
        
        return jsonify({'message': message})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# AI: Summarize conversation
@app.route('/api/ai/summarize/<int:partner_id>')
@login_required
def api_ai_summarize(partner_id):
    client = get_ai_client()
    if not client:
        return jsonify({'error': 'AI not configured. Add ANTHROPIC_API_KEY to environment.'}), 400
    
    partner = Partner.query.get_or_404(partner_id)
    messages = Message.query.filter_by(partner_id=partner_id).order_by(Message.created_at).all()
    
    if not messages:
        return jsonify({'summary': 'No conversation history yet.'})
    
    # Build conversation
    conversation = []
    for m in messages:
        role = partner.first_name if m.direction == 'inbound' else 'You'
        conversation.append(f"{role}: {m.body or '[Media]'}")
    
    conversation_text = '\n'.join(conversation)
    
    prompt = f"""Summarize this SMS conversation between a SilverSky partner manager and {partner.full_name} ({partner.company or 'unknown company'}).

Conversation:
{conversation_text}

Provide a brief summary (2-3 sentences) covering:
1. Main topics discussed
2. Current status/next steps
3. Partner's sentiment/interest level

Return ONLY the summary, no headers or labels."""

    try:
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}]
        )
        
        summary = response.content[0].text.strip()
        return jsonify({'summary': summary})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# AI: Analyze sentiment of conversation
@app.route('/api/ai/sentiment/<int:partner_id>')
@login_required
def api_ai_sentiment(partner_id):
    client = get_ai_client()
    if not client:
        return jsonify({'error': 'AI not configured'}), 400
    
    messages = Message.query.filter_by(partner_id=partner_id, direction='inbound').order_by(Message.created_at.desc()).limit(5).all()
    
    if not messages:
        return jsonify({'sentiment': 'neutral', 'score': 50, 'label': 'No messages yet'})
    
    recent_messages = '\n'.join([m.body or '' for m in messages if m.body])
    
    prompt = f"""Analyze the sentiment of these recent messages from a sales prospect:

{recent_messages}

Return ONLY a JSON object with:
- "sentiment": "positive", "neutral", or "negative"
- "score": number from 0-100 (0=very negative, 100=very positive)
- "label": brief 2-3 word description (e.g., "Very interested", "Needs follow-up", "Frustrated")

Example: {{"sentiment": "positive", "score": 75, "label": "Interested"}}"""

    try:
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=100,
            messages=[{"role": "user", "content": prompt}]
        )
        
        result = json.loads(response.content[0].text.strip())
        return jsonify(result)
    except:
        return jsonify({'sentiment': 'neutral', 'score': 50, 'label': 'Unknown'})

# AI: Predict best time to text a partner
@app.route('/api/ai/best-time/<int:partner_id>')
@login_required
def api_ai_best_time(partner_id):
    partner = Partner.query.get_or_404(partner_id)
    
    # Get inbound messages (their responses) with timestamps
    responses = Message.query.filter_by(partner_id=partner_id, direction='inbound').all()
    
    if len(responses) < 2:
        return jsonify({
            'best_time': None,
            'best_day': None,
            'message': 'Not enough data yet. Need more responses to analyze patterns.',
            'confidence': 'low'
        })
    
    # Analyze response times
    hours = {}
    days = {}
    
    for msg in responses:
        hour = msg.created_at.hour
        day = msg.created_at.strftime('%A')
        
        hours[hour] = hours.get(hour, 0) + 1
        days[day] = days.get(day, 0) + 1
    
    # Find peaks
    best_hour = max(hours, key=hours.get) if hours else 10
    best_day = max(days, key=days.get) if days else 'Tuesday'
    
    # Format time nicely
    if best_hour < 12:
        time_str = f"{best_hour}:00 AM" if best_hour != 0 else "12:00 AM"
    elif best_hour == 12:
        time_str = "12:00 PM"
    else:
        time_str = f"{best_hour - 12}:00 PM"
    
    confidence = 'high' if len(responses) >= 10 else 'medium' if len(responses) >= 5 else 'low'
    
    return jsonify({
        'best_time': time_str,
        'best_hour': best_hour,
        'best_day': best_day,
        'response_count': len(responses),
        'confidence': confidence,
        'hour_breakdown': hours,
        'day_breakdown': days
    })

# AI: Ghost Alert - Find partners who've gone silent
@app.route('/api/ai/ghost-alerts')
@login_required
def api_ai_ghost_alerts():
    client = get_ai_client()
    
    ghosts = []
    partners = Partner.query.filter_by(archived=False, opted_out=False).all()
    
    for partner in partners:
        # Get their messages
        messages = Message.query.filter_by(partner_id=partner.id).order_by(Message.created_at.desc()).all()
        
        if not messages:
            continue
        
        # Check if last message was from us (they haven't replied)
        last_msg = messages[0]
        if last_msg.direction != 'outbound':
            continue
        
        # Calculate their typical response time
        response_times = []
        for i, msg in enumerate(messages[:-1]):
            if msg.direction == 'inbound':
                # Find the outbound message before this
                for prev_msg in messages[i+1:]:
                    if prev_msg.direction == 'outbound':
                        delta = (msg.created_at - prev_msg.created_at).total_seconds() / 3600  # hours
                        if delta > 0 and delta < 168:  # within a week
                            response_times.append(delta)
                        break
        
        if not response_times:
            avg_response_hours = 48  # default assumption
        else:
            avg_response_hours = sum(response_times) / len(response_times)
        
        # How long since we messaged them?
        hours_waiting = (datetime.utcnow() - last_msg.created_at).total_seconds() / 3600
        
        # If waiting longer than 2x their usual response time, they're ghosting
        if hours_waiting > max(avg_response_hours * 2, 48):  # at least 48 hours
            days_waiting = int(hours_waiting / 24)
            
            ghosts.append({
                'partner_id': partner.id,
                'name': partner.full_name,
                'company': partner.company,
                'last_message': last_msg.body[:100] if last_msg.body else '[Media]',
                'last_message_date': last_msg.created_at.isoformat(),
                'days_waiting': days_waiting,
                'avg_response_hours': round(avg_response_hours, 1),
                'urgency': 'high' if days_waiting > 7 else 'medium' if days_waiting > 3 else 'low'
            })
    
    # Sort by days waiting (most urgent first)
    ghosts.sort(key=lambda x: x['days_waiting'], reverse=True)
    
    # Generate re-engagement messages with AI if we have a client
    if client and ghosts:
        for ghost in ghosts[:5]:  # Only generate for top 5 to save API calls
            try:
                prompt = f"""Write a short, friendly SMS follow-up for someone who hasn't responded in {ghost['days_waiting']} days.

Last message sent to them: "{ghost['last_message']}"

Keep it under 160 characters. Be casual, not pushy. Don't guilt them. Maybe add value or give them an easy out.

Return ONLY the message text."""

                response = client.messages.create(
                    model="claude-sonnet-4-20250514",
                    max_tokens=100,
                    messages=[{"role": "user", "content": prompt}]
                )
                ghost['suggested_message'] = response.content[0].text.strip().strip('"')
            except:
                ghost['suggested_message'] = f"Hey, just floating this back up - any thoughts?"
    
    return jsonify(ghosts)

# AI: Next Best Action - Who to text today and what to say
@app.route('/api/ai/next-actions')
@login_required
def api_ai_next_actions():
    client = get_ai_client()
    if not client:
        return jsonify({'error': 'AI not configured'}), 400
    
    actions = []
    partners = Partner.query.filter_by(archived=False, opted_out=False).all()
    
    partner_data = []
    
    for partner in partners:
        messages = Message.query.filter_by(partner_id=partner.id).order_by(Message.created_at.desc()).limit(5).all()
        
        if not messages:
            # Never contacted - potential action
            partner_data.append({
                'id': partner.id,
                'name': partner.full_name,
                'company': partner.company,
                'region': partner.region.name if partner.region else None,
                'status': 'never_contacted',
                'days_since_contact': None,
                'last_direction': None,
                'recent_messages': [],
                'notes': partner.notes
            })
        else:
            last_msg = messages[0]
            days_since = (datetime.utcnow() - last_msg.created_at).days
            
            partner_data.append({
                'id': partner.id,
                'name': partner.full_name,
                'company': partner.company,
                'region': partner.region.name if partner.region else None,
                'status': 'awaiting_reply' if last_msg.direction == 'outbound' else 'needs_response',
                'days_since_contact': days_since,
                'last_direction': last_msg.direction,
                'recent_messages': [{'direction': m.direction, 'body': m.body[:100] if m.body else '[Media]'} for m in messages[:3]],
                'notes': partner.notes
            })
    
    # Use AI to prioritize and suggest actions
    knowledge = get_ai_knowledge_context()
    
    prompt = f"""You're a sales AI assistant. Analyze these partners and recommend the top 5 actions for today.
{knowledge}

Partner data:
{json.dumps(partner_data[:30], indent=2)}

For each recommended action, provide:
1. partner_id (number)
2. priority: "high", "medium", or "low"  
3. reason: Brief explanation why (under 50 chars)
4. action: What to do ("send intro", "follow up", "respond", "re-engage", "check in")
5. suggested_message: A ready-to-send SMS (under 160 chars)

Consider:
- Partners awaiting our response (needs_response) are highest priority
- Never contacted partners are opportunities
- Don't let good conversations go cold
- Warm leads need nurturing

Return ONLY a JSON array of 5 action objects. Example:
[{{"partner_id": 1, "priority": "high", "reason": "They asked a question 2 days ago", "action": "respond", "suggested_message": "Hey! Great question about pricing..."}}]"""

    try:
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}]
        )
        
        response_text = response.content[0].text.strip()
        # Clean up if wrapped in code blocks
        if response_text.startswith('```'):
            response_text = response_text.split('```')[1]
            if response_text.startswith('json'):
                response_text = response_text[4:]
        
        actions = json.loads(response_text)
        
        # Enrich with partner details
        for action in actions:
            partner = Partner.query.get(action['partner_id'])
            if partner:
                action['partner_name'] = partner.full_name
                action['partner_company'] = partner.company
                action['partner_phone'] = partner.phone
        
        return jsonify(actions)
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# API: AI Knowledge Base
@app.route('/api/ai/knowledge', methods=['GET', 'POST'])
@login_required
def api_ai_knowledge():
    if request.method == 'POST':
        data = request.json
        knowledge = AIKnowledge(
            category=data['category'],
            title=data['title'],
            content=data['content']
        )
        db.session.add(knowledge)
        db.session.commit()
        return jsonify({'success': True, 'id': knowledge.id})
    
    items = AIKnowledge.query.order_by(AIKnowledge.category, AIKnowledge.title).all()
    return jsonify([{
        'id': k.id,
        'category': k.category,
        'title': k.title,
        'content': k.content,
        'created_at': k.created_at.isoformat(),
        'updated_at': k.updated_at.isoformat()
    } for k in items])

@app.route('/api/ai/knowledge/<int:id>', methods=['GET', 'PUT', 'DELETE'])
@login_required
def api_ai_knowledge_item(id):
    item = AIKnowledge.query.get_or_404(id)
    
    if request.method == 'DELETE':
        db.session.delete(item)
        db.session.commit()
        return jsonify({'success': True})
    
    if request.method == 'PUT':
        data = request.json
        item.category = data.get('category', item.category)
        item.title = data.get('title', item.title)
        item.content = data.get('content', item.content)
        db.session.commit()
        return jsonify({'success': True})
    
    return jsonify({
        'id': item.id,
        'category': item.category,
        'title': item.title,
        'content': item.content
    })

# API: AI Settings
@app.route('/api/ai/settings', methods=['GET', 'POST'])
@login_required
def api_ai_settings():
    if request.method == 'POST':
        data = request.json
        for key, value in data.items():
            setting = AISettings.query.filter_by(setting_key=key).first()
            if setting:
                setting.setting_value = value
            else:
                setting = AISettings(setting_key=key, setting_value=value)
                db.session.add(setting)
        db.session.commit()
        return jsonify({'success': True})
    
    settings = AISettings.query.all()
    return jsonify({s.setting_key: s.setting_value for s in settings})

# API: Analyze user's writing style from sent messages
@app.route('/api/ai/analyze-style', methods=['POST'])
@login_required
def api_ai_analyze_style():
    client = get_ai_client()
    if not client:
        return jsonify({'error': 'AI not configured'}), 400
    
    # Get user's sent messages
    messages = Message.query.filter_by(direction='outbound').order_by(Message.created_at.desc()).limit(50).all()
    
    if len(messages) < 5:
        return jsonify({'error': 'Need at least 5 sent messages to analyze your style. Send more messages first!'}), 400
    
    # Extract message bodies
    message_texts = [m.body for m in messages if m.body and len(m.body) > 10]
    
    if len(message_texts) < 5:
        return jsonify({'error': 'Not enough text messages to analyze. Send more messages first!'}), 400
    
    sample = '\n---\n'.join(message_texts[:30])
    
    prompt = f"""Analyze these SMS messages written by a sales professional and create a detailed writing style guide that captures exactly how they communicate.

Messages:
{sample}

Create a comprehensive style guide that includes:
1. Overall tone (formal/casual/friendly/direct)
2. Typical message length preferences
3. Common phrases and expressions they use
4. Punctuation style (exclamation points, emojis, ellipses, etc.)
5. How they start messages (greetings used)
6. How they end messages (sign-offs, CTAs)
7. Vocabulary patterns (technical terms, casual words, etc.)
8. Personality traits that come through
9. What they avoid doing

Write this as instructions for an AI to mimic this person's exact writing style. Be specific and include actual examples from their messages.

Format as a clear, actionable style guide in 200-300 words."""

    try:
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1000,
            messages=[{"role": "user", "content": prompt}]
        )
        
        style_guide = response.content[0].text.strip()
        
        # Save or update the writing style
        existing = AIKnowledge.query.filter_by(category='tone', title='My Writing Style').first()
        if existing:
            existing.content = style_guide
        else:
            knowledge = AIKnowledge(
                category='tone',
                title='My Writing Style',
                content=style_guide
            )
            db.session.add(knowledge)
        
        db.session.commit()
        
        return jsonify({
            'success': True,
            'style_guide': style_guide,
            'messages_analyzed': len(message_texts)
        })
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# API: Learn from example messages
@app.route('/api/ai/learn-examples', methods=['POST'])
@login_required
def api_ai_learn_examples():
    client = get_ai_client()
    if not client:
        return jsonify({'error': 'AI not configured'}), 400
    
    data = request.json
    examples = data.get('examples', '').strip()
    
    if not examples or len(examples) < 50:
        return jsonify({'error': 'Please provide more example messages'}), 400
    
    prompt = f"""Analyze these example SMS messages and create a detailed writing style guide that captures exactly how this person communicates.

Example messages:
{examples}

Create a comprehensive style guide that includes:
1. Overall tone (formal/casual/friendly/direct)
2. Typical message length preferences
3. Common phrases and expressions they use
4. Punctuation style (exclamation points, emojis, etc.)
5. How they start and end messages
6. Vocabulary patterns
7. Personality traits that come through

Write this as instructions for an AI to mimic this exact writing style. Be specific and include actual examples.

Format as a clear, actionable style guide in 150-250 words."""

    try:
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=800,
            messages=[{"role": "user", "content": prompt}]
        )
        
        style_guide = response.content[0].text.strip()
        
        # Save or update
        existing = AIKnowledge.query.filter_by(category='tone', title='My Writing Style').first()
        if existing:
            existing.content = style_guide
        else:
            knowledge = AIKnowledge(
                category='tone',
                title='My Writing Style',
                content=style_guide
            )
            db.session.add(knowledge)
        
        db.session.commit()
        
        return jsonify({
            'success': True,
            'style_guide': style_guide
        })
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# API: Scrape website for AI training
@app.route('/api/ai/scrape', methods=['POST'])
@login_required
def api_ai_scrape():
    client = get_ai_client()
    if not client:
        return jsonify({'error': 'AI not configured'}), 400
    
    data = request.json
    url = data.get('url', '').strip()
    
    if not url:
        return jsonify({'error': 'Please provide a URL'}), 400
    
    if not url.startswith('http'):
        url = 'https://' + url
    
    try:
        # Scrape the main page
        headers = {'User-Agent': 'Mozilla/5.0 (compatible; SilverSkyBot/1.0)'}
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Remove script and style elements
        for script in soup(["script", "style", "nav", "footer", "header"]):
            script.decompose()
        
        # Get text content
        text = soup.get_text(separator='\n', strip=True)
        
        # Clean up text - remove extra whitespace
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        text = '\n'.join(lines)
        
        # Truncate if too long
        if len(text) > 15000:
            text = text[:15000] + "..."
        
        # Find internal links to scrape more pages
        base_domain = urlparse(url).netloc
        internal_links = set()
        for a in soup.find_all('a', href=True):
            href = a['href']
            full_url = urljoin(url, href)
            if urlparse(full_url).netloc == base_domain and full_url != url:
                internal_links.add(full_url)
        
        # Scrape up to 5 additional pages
        additional_content = []
        for link in list(internal_links)[:5]:
            try:
                resp = requests.get(link, headers=headers, timeout=10)
                if resp.status_code == 200:
                    page_soup = BeautifulSoup(resp.text, 'html.parser')
                    for script in page_soup(["script", "style", "nav", "footer", "header"]):
                        script.decompose()
                    page_text = page_soup.get_text(separator='\n', strip=True)
                    page_lines = [line.strip() for line in page_text.splitlines() if line.strip()]
                    page_text = '\n'.join(page_lines)
                    if len(page_text) > 5000:
                        page_text = page_text[:5000]
                    additional_content.append(f"\n\n--- Page: {link} ---\n{page_text}")
            except:
                continue
        
        all_content = text + ''.join(additional_content)
        
        # Use AI to extract and categorize knowledge
        prompt = f"""Analyze this website content and extract useful information for training an AI sales assistant for SilverSky (a cybersecurity company selling to channel partners/MSPs).

Website content:
{all_content}

Extract and categorize the most important information into these categories:
1. products - Product/service descriptions, features, benefits
2. general - Company info, about us, differentiators, value props
3. faq - Any FAQ content or common questions answered

Return a JSON array of knowledge items. Each item should have:
- "category": one of "products", "general", or "faq"  
- "title": short descriptive title (under 100 chars)
- "content": the extracted content (keep it concise but informative, under 500 chars)

Return 5-15 items that would be most useful for a sales person. Focus on:
- Product features and benefits
- Pricing info if available
- Company differentiators
- Key selling points

Return ONLY valid JSON array, no other text."""

        ai_response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=4000,
            messages=[{"role": "user", "content": prompt}]
        )
        
        # Parse AI response
        response_text = ai_response.content[0].text.strip()
        # Clean up response if needed
        if response_text.startswith('```'):
            response_text = response_text.split('```')[1]
            if response_text.startswith('json'):
                response_text = response_text[4:]
        
        knowledge_items = json.loads(response_text)
        
        # Save to database
        added = 0
        for item in knowledge_items:
            if item.get('title') and item.get('content'):
                knowledge = AIKnowledge(
                    category=item.get('category', 'general'),
                    title=item['title'][:200],
                    content=item['content']
                )
                db.session.add(knowledge)
                added += 1
        
        db.session.commit()
        
        return jsonify({
            'success': True,
            'added': added,
            'pages_scraped': 1 + len(additional_content),
            'items': knowledge_items
        })
        
    except requests.RequestException as e:
        return jsonify({'error': f'Failed to fetch URL: {str(e)}'}), 400
    except json.JSONDecodeError as e:
        return jsonify({'error': 'Failed to parse AI response'}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# API: Message Templates
@app.route('/api/templates', methods=['GET', 'POST'])
@login_required
def api_templates():
    if request.method == 'POST':
        data = request.json
        template = MessageTemplate(
            name=data['name'],
            body=data['body']
        )
        db.session.add(template)
        db.session.commit()
        return jsonify({'success': True, 'id': template.id})
    
    templates = MessageTemplate.query.order_by(MessageTemplate.name).all()
    return jsonify([{
        'id': t.id,
        'name': t.name,
        'body': t.body,
        'created_at': t.created_at.isoformat()
    } for t in templates])

@app.route('/api/templates/<int:id>', methods=['PUT', 'DELETE'])
@login_required
def api_template(id):
    template = MessageTemplate.query.get_or_404(id)
    
    if request.method == 'DELETE':
        db.session.delete(template)
        db.session.commit()
        return jsonify({'success': True})
    
    if request.method == 'PUT':
        data = request.json
        template.name = data.get('name', template.name)
        template.body = data.get('body', template.body)
        db.session.commit()
        return jsonify({'success': True})

# API: Messages/Conversations
@app.route('/api/conversations')
@login_required
def api_conversations():
    show_archived = request.args.get('archived') == 'true'
    filter_unread = request.args.get('unread') == 'true'
    filter_has_media = request.args.get('has_media') == 'true'
    
    partners = Partner.query.filter_by(archived=show_archived).all()
    conversations = []
    
    for partner in partners:
        latest = Message.query.filter_by(partner_id=partner.id).order_by(Message.created_at.desc()).first()
        unread = Message.query.filter_by(partner_id=partner.id, direction='inbound', status='received').count()
        total_messages = Message.query.filter_by(partner_id=partner.id).count()
        has_any_media = Message.query.filter(Message.partner_id == partner.id, Message.media_url.isnot(None)).count() > 0
        
        if latest:  # Only show partners with messages
            # Apply filters
            if filter_unread and unread == 0:
                continue
            if filter_has_media and not has_any_media:
                continue
                
            conversations.append({
                'partner_id': partner.id,
                'name': partner.full_name,
                'first_name': partner.first_name,
                'company': partner.company,
                'phone': partner.phone,
                'region': partner.region.name if partner.region else None,
                'tsd': partner.tsd.name if partner.tsd else None,
                'tags': [{'id': t.id, 'name': t.name, 'color': t.color} for t in partner.tags],
                'notes': partner.notes,
                'opted_out': partner.opted_out,
                'pinned': partner.pinned if hasattr(partner, 'pinned') else False,
                'archived': partner.archived if hasattr(partner, 'archived') else False,
                'latest_message': latest.body,
                'latest_time': latest.created_at.isoformat(),
                'has_media': latest.media_url is not None,
                'has_any_media': has_any_media,
                'unread': unread,
                'total_messages': total_messages,
                'direction': latest.direction,
                'last_contacted': partner.last_contacted.isoformat() if partner.last_contacted else None
            })
    
    # Sort: pinned first, then by latest time
    conversations.sort(key=lambda x: (not x.get('pinned', False), x['latest_time']), reverse=True)
    conversations.sort(key=lambda x: not x.get('pinned', False))
    
    return jsonify(conversations)

# API: Pin/Unpin conversation
@app.route('/api/partners/<int:id>/pin', methods=['POST'])
@login_required
def api_pin_partner(id):
    partner = Partner.query.get_or_404(id)
    partner.pinned = not partner.pinned
    db.session.commit()
    return jsonify({'success': True, 'pinned': partner.pinned})

# API: Archive/Unarchive conversation
@app.route('/api/partners/<int:id>/archive', methods=['POST'])
@login_required
def api_archive_partner(id):
    partner = Partner.query.get_or_404(id)
    partner.archived = not partner.archived
    db.session.commit()
    return jsonify({'success': True, 'archived': partner.archived})

# API: Update partner notes
@app.route('/api/partners/<int:id>/notes', methods=['POST'])
@login_required
def api_update_notes(id):
    partner = Partner.query.get_or_404(id)
    data = request.json
    partner.notes = data.get('notes', '')
    db.session.commit()
    return jsonify({'success': True})

# API: Export conversation
@app.route('/api/partners/<int:id>/export')
@login_required
def api_export_conversation(id):
    partner = Partner.query.get_or_404(id)
    messages = Message.query.filter_by(partner_id=id).order_by(Message.created_at).all()
    
    export_format = request.args.get('format', 'txt')
    
    if export_format == 'txt':
        content = f"Conversation with {partner.full_name}\n"
        content += f"Phone: {partner.phone}\n"
        content += f"Company: {partner.company or 'N/A'}\n"
        content += f"Exported: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}\n"
        content += "=" * 50 + "\n\n"
        
        for m in messages:
            direction = "→ You" if m.direction == 'outbound' else f"← {partner.first_name}"
            time = m.created_at.strftime('%Y-%m-%d %H:%M')
            content += f"[{time}] {direction}:\n{m.body or '[Media]'}\n\n"
        
        return Response(
            content,
            mimetype='text/plain',
            headers={'Content-Disposition': f'attachment; filename=conversation_{partner.first_name}_{id}.txt'}
        )
    
    return jsonify({'error': 'Unsupported format'}), 400

# API: Dashboard Stats
@app.route('/api/stats')
@login_required
def api_stats():
    from sqlalchemy import func
    
    now = datetime.utcnow()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = today_start - timedelta(days=now.weekday())
    month_start = today_start.replace(day=1)
    
    # Total partners
    total_partners = Partner.query.count()
    
    # Never contacted
    never_contacted = Partner.query.filter(Partner.last_contacted.is_(None)).count()
    
    # Messages today
    messages_today = Message.query.filter(Message.created_at >= today_start).count()
    
    # Messages this week
    messages_week = Message.query.filter(Message.created_at >= week_start).count()
    
    # Messages sent (outbound) this week
    sent_week = Message.query.filter(
        Message.created_at >= week_start,
        Message.direction == 'outbound'
    ).count()
    
    # Replies received this week
    replies_week = Message.query.filter(
        Message.created_at >= week_start,
        Message.direction == 'inbound'
    ).count()
    
    # Unread count
    unread = Message.query.filter_by(direction='inbound', status='received').count()
    
    # Response rate (partners who replied / partners messaged this month)
    partners_messaged = db.session.query(func.count(func.distinct(Message.partner_id))).filter(
        Message.created_at >= month_start,
        Message.direction == 'outbound'
    ).scalar() or 0
    
    partners_replied = db.session.query(func.count(func.distinct(Message.partner_id))).filter(
        Message.created_at >= month_start,
        Message.direction == 'inbound'
    ).scalar() or 0
    
    response_rate = round((partners_replied / partners_messaged * 100), 1) if partners_messaged > 0 else 0
    
    # Partners by region
    region_stats = db.session.query(
        Region.name,
        func.count(Partner.id)
    ).outerjoin(Partner).group_by(Region.id, Region.name).all()
    
    no_region = Partner.query.filter(Partner.region_id.is_(None)).count()
    
    return jsonify({
        'total_partners': total_partners,
        'never_contacted': never_contacted,
        'messages_today': messages_today,
        'messages_week': messages_week,
        'sent_week': sent_week,
        'replies_week': replies_week,
        'unread': unread,
        'response_rate': response_rate,
        'partners_by_region': [{'name': r[0], 'count': r[1]} for r in region_stats] + ([{'name': 'No Region', 'count': no_region}] if no_region > 0 else [])
    })

@app.route('/api/messages/<int:partner_id>')
@login_required
def api_messages(partner_id):
    messages = Message.query.filter_by(partner_id=partner_id).order_by(Message.created_at).all()
    
    for m in messages:
        if m.direction == 'inbound' and m.status == 'received':
            m.status = 'read'
    db.session.commit()
    
    return jsonify([{
        'id': m.id,
        'direction': m.direction,
        'body': m.body,
        'media_url': m.media_url,
        'media_type': m.media_type,
        'status': m.status,
        'created_at': m.created_at.isoformat()
    } for m in messages])

# API: Send message
@app.route('/api/send', methods=['POST'])
@login_required
def api_send():
    data = request.json
    partner = Partner.query.get_or_404(data['partner_id'])
    message = personalize_message(data['message'], partner)
    
    media_url = data.get('media_url')
    media_type = data.get('media_type')
    
    result = send_sms(partner.phone, message, partner.id, media_url, media_type)
    return jsonify(result)

# API: Broadcast
@app.route('/api/broadcast', methods=['POST'])
@login_required
def api_broadcast():
    data = request.json
    message_template = data['message']
    partner_ids = data.get('partner_ids', [])
    
    media_url = data.get('media_url')
    media_type = data.get('media_type')
    
    results = []
    for pid in partner_ids:
        partner = Partner.query.get(pid)
        if partner and not partner.opted_out:
            message = personalize_message(message_template, partner)
            result = send_sms(partner.phone, message, partner.id, media_url, media_type)
            results.append({'partner': partner.full_name, 'result': result})
    
    return jsonify({'sent': len(results), 'results': results})

# API: Scheduled Messages
@app.route('/api/scheduled', methods=['GET', 'POST'])
@login_required
def api_scheduled():
    if request.method == 'POST':
        data = request.json
        scheduled = ScheduledMessage(
            message_template=data['message'],
            partner_ids=json.dumps(data['partner_ids']),
            media_url=data.get('media_url'),
            media_type=data.get('media_type'),
            scheduled_time=datetime.fromisoformat(data['scheduled_time'].replace('Z', '+00:00'))
        )
        db.session.add(scheduled)
        db.session.commit()
        return jsonify({'success': True, 'id': scheduled.id})
    
    scheduled = ScheduledMessage.query.filter_by(status='pending').order_by(ScheduledMessage.scheduled_time).all()
    return jsonify([{
        'id': s.id,
        'message': s.message_template,
        'partner_count': len(json.loads(s.partner_ids)),
        'scheduled_time': s.scheduled_time.isoformat(),
        'status': s.status
    } for s in scheduled])

@app.route('/api/scheduled/<int:id>', methods=['DELETE'])
@login_required
def api_scheduled_delete(id):
    scheduled = ScheduledMessage.query.get_or_404(id)
    scheduled.status = 'cancelled'
    db.session.commit()
    return jsonify({'success': True})

# API: Upload media (now uses Cloudinary)
@app.route('/api/upload', methods=['POST'])
@login_required
def api_upload():
    if 'file' not in request.files:
        return jsonify({'success': False, 'error': 'No file provided'}), 400
    
    file = request.files['file']
    if file.filename == '':
        return jsonify({'success': False, 'error': 'No file selected'}), 400
    
    # Check for explicit media_type from form (for recordings)
    explicit_type = request.form.get('media_type')
    
    # Determine media type from MIME type first, then extension
    content_type = file.content_type or ''
    filename = secure_filename(file.filename)
    ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
    
    # Use explicit type if provided (from recordings)
    if explicit_type in ['audio', 'video', 'image']:
        media_type = explicit_type
        resource_type = 'image' if explicit_type == 'image' else 'video'
    # Check MIME type first (more reliable for recordings)
    elif content_type.startswith('audio/'):
        media_type = 'audio'
        resource_type = 'video'  # Cloudinary uses 'video' for audio too
    elif content_type.startswith('video/'):
        media_type = 'video'
        resource_type = 'video'
    elif content_type.startswith('image/'):
        media_type = 'image'
        resource_type = 'image'
    elif ext in ['jpg', 'jpeg', 'png', 'gif']:
        media_type = 'image'
        resource_type = 'image'
    elif ext in ['mp4', 'mov', 'avi']:
        media_type = 'video'
        resource_type = 'video'
    elif ext in ['mp3', 'wav', 'ogg', 'm4a']:
        media_type = 'audio'
        resource_type = 'video'
    elif ext == 'webm':
        # Default webm to video, but explicit type or MIME type check above should catch audio
        media_type = 'video'
        resource_type = 'video'
    else:
        return jsonify({'success': False, 'error': 'Unsupported file type'}), 400
    
    # Check if Cloudinary is configured
    if not os.environ.get('CLOUDINARY_CLOUD_NAME'):
        return jsonify({'success': False, 'error': 'Cloud storage not configured. Add Cloudinary credentials.'}), 400
    
    try:
        # Upload to Cloudinary
        result = cloudinary.uploader.upload(
            file,
            resource_type=resource_type,
            folder='silversky-sms'
        )
        
        return jsonify({
            'success': True,
            'url': result['secure_url'],
            'media_type': media_type
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# Twilio webhook for incoming messages
@app.route('/webhook/incoming', methods=['POST'])
def webhook_incoming():
    from_number = request.values.get('From', '')
    body = request.values.get('Body', '')
    num_media = int(request.values.get('NumMedia', 0))
    
    # Get media if present
    media_url = None
    media_type = None
    if num_media > 0:
        media_url = request.values.get('MediaUrl0', '')
        content_type = request.values.get('MediaContentType0', '')
        if 'image' in content_type:
            media_type = 'image'
        elif 'video' in content_type:
            media_type = 'video'
        elif 'audio' in content_type:
            media_type = 'audio'
    
    partner = Partner.query.filter_by(phone=from_number).first()
    
    # Check for opt-out keywords
    opt_out_keywords = ['stop', 'unsubscribe', 'cancel', 'quit', 'end']
    if body.strip().lower() in opt_out_keywords:
        if partner:
            partner.opted_out = True
            db.session.commit()
    
    if partner:
        msg = Message(
            partner_id=partner.id,
            direction='inbound',
            body=body,
            media_url=media_url,
            media_type=media_type,
            status='received'
        )
        db.session.add(msg)
        db.session.commit()
        send_notification(partner.full_name, body)
    else:
        # Unknown sender - create partner
        partner = Partner(
            first_name=from_number,
            phone=from_number,
            notes='Auto-created from incoming message'
        )
        db.session.add(partner)
        db.session.commit()
        
        msg = Message(
            partner_id=partner.id,
            direction='inbound',
            body=body,
            media_url=media_url,
            media_type=media_type,
            status='received'
        )
        db.session.add(msg)
        db.session.commit()
        send_notification(from_number, body)
    
    resp = MessagingResponse()
    return str(resp)

# Twilio webhook for delivery status updates
@app.route('/webhook/status', methods=['POST'])
def webhook_status():
    message_sid = request.values.get('MessageSid', '')
    status = request.values.get('MessageStatus', '')
    
    if message_sid and status:
        message = Message.query.filter_by(twilio_sid=message_sid).first()
        if message:
            message.status = status
            db.session.commit()
    
    return '', 200

# Background job to send scheduled messages
def send_scheduled_messages():
    with app.app_context():
        now = datetime.utcnow()
        pending = ScheduledMessage.query.filter(
            ScheduledMessage.status == 'pending',
            ScheduledMessage.scheduled_time <= now
        ).all()
        
        for scheduled in pending:
            partner_ids = json.loads(scheduled.partner_ids)
            for pid in partner_ids:
                partner = Partner.query.get(pid)
                if partner and not partner.opted_out:
                    message = personalize_message(scheduled.message_template, partner)
                    send_sms(partner.phone, message, partner.id, scheduled.media_url, scheduled.media_type)
            
            scheduled.status = 'sent'
            db.session.commit()

# Initialize database with default products
def init_db():
    with app.app_context():
        db.create_all()
        
        # Add default products if none exist
        if Product.query.count() == 0:
            default_products = ['MxDR', 'Email Protection', 'Compliance']
            for name in default_products:
                db.session.add(Product(name=name))
            db.session.commit()

# API: User Settings (onboarding, calendar, password)
@app.route('/api/user/settings', methods=['GET', 'PUT'])
@login_required
def api_user_settings():
    settings = get_user_settings()
    
    if request.method == 'PUT':
        data = request.json
        if 'onboarding_step' in data:
            settings.onboarding_step = data['onboarding_step']
        if 'calendar_link' in data:
            settings.calendar_link = data['calendar_link']
        db.session.commit()
        return jsonify({'success': True})
    
    return jsonify({
        'onboarding_step': settings.onboarding_step,
        'calendar_link': settings.calendar_link or ''
    })

@app.route('/api/user/change-password', methods=['POST'])
@login_required
def api_change_password():
    data = request.json
    current_password = data.get('current_password', '')
    new_password = data.get('new_password', '')
    
    if not new_password or len(new_password) < 6:
        return jsonify({'success': False, 'error': 'Password must be at least 6 characters'}), 400
    
    # Check current password
    settings = get_user_settings()
    valid_password = settings.custom_password if settings.custom_password else APP_PASSWORD
    
    if current_password != valid_password:
        return jsonify({'success': False, 'error': 'Current password is incorrect'}), 400
    
    # Set new password
    settings.custom_password = new_password
    db.session.commit()
    
    return jsonify({'success': True})

@app.route('/api/user/onboarding-status')
@login_required
def api_onboarding_status():
    settings = get_user_settings()
    knowledge_count = AIKnowledge.query.count()
    partner_count = Partner.query.count()
    
    # Calculate completion based on what's actually done
    steps_complete = {
        '1': knowledge_count > 0,
        '2': knowledge_count >= 3,
        '3': AIKnowledge.query.filter_by(category='objections').count() > 0,
        '4': AIKnowledge.query.filter_by(category='tone').count() > 0,
        '5': partner_count > 0,
        '6': settings.calendar_link is not None and settings.calendar_link != ''
    }
    
    return jsonify({
        'current_step': settings.onboarding_step,
        'steps_complete': steps_complete,
        'knowledge_count': knowledge_count,
        'partner_count': partner_count,
        'calendar_link': settings.calendar_link or ''
    })

@app.route('/api/user/onboarding-step', methods=['POST'])
@login_required
def api_update_onboarding_step():
    data = request.json
    step = data.get('step', 0)
    
    settings = get_user_settings()
    settings.onboarding_step = max(settings.onboarding_step, step)  # Only go forward
    db.session.commit()
    
    return jsonify({'success': True, 'step': settings.onboarding_step})

@app.route('/api/user/calendar-link', methods=['GET', 'POST'])
@login_required
def api_calendar_link():
    settings = get_user_settings()
    
    if request.method == 'POST':
        data = request.json
        settings.calendar_link = data.get('calendar_link', '').strip()
        db.session.commit()
        
        # Also save to AI knowledge so AI knows about it
        existing = AIKnowledge.query.filter_by(category='general', title='Calendar Link').first()
        if settings.calendar_link:
            if existing:
                existing.content = f"When scheduling meetings, use this calendar link: {settings.calendar_link}"
            else:
                knowledge = AIKnowledge(
                    category='general',
                    title='Calendar Link',
                    content=f"When scheduling meetings, use this calendar link: {settings.calendar_link}"
                )
                db.session.add(knowledge)
            db.session.commit()
        
        return jsonify({'success': True})
    
    return jsonify({'calendar_link': settings.calendar_link or ''})

# Initialize scheduler for scheduled messages
def init_scheduler():
    from apscheduler.schedulers.background import BackgroundScheduler
    scheduler = BackgroundScheduler()
    scheduler.add_job(send_scheduled_messages, 'interval', minutes=1)
    scheduler.start()

init_db()

# Only start scheduler if not in debug/reload mode
if not app.debug or os.environ.get('WERKZEUG_RUN_MAIN') == 'true':
    try:
        init_scheduler()
    except:
        pass  # Scheduler may already be running

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
