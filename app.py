import os
import json
import smtplib
from email.mime.text import MIMEText
from datetime import datetime, timedelta
from functools import wraps
from flask import Flask, render_template, request, jsonify, redirect, url_for, session, send_from_directory
from flask_sqlalchemy import SQLAlchemy
from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse
from werkzeug.utils import secure_filename
import secrets

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', secrets.token_hex(32))
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///sms_platform.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max

# Ensure upload folder exists
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

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
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_contacted = db.Column(db.DateTime)
    
    region = db.relationship('Region', backref='partners')
    tsd = db.relationship('TSD', backref='partners')
    products = db.relationship('Product', secondary=partner_products, backref='partners')
    messages = db.relationship('Message', backref='partner', lazy=True)
    
    @property
    def full_name(self):
        if self.last_name:
            return f"{self.first_name} {self.last_name}"
        return self.first_name
    
    @property
    def is_new(self):
        return self.last_contacted is None

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

class MediaFile(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(255), nullable=False)
    original_name = db.Column(db.String(255))
    media_type = db.Column(db.String(50))  # image, video, audio
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

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
    if partner.tsd:
        message = message.replace('{{tsd}}', partner.tsd.name)
    return message

# Send SMS/MMS
def send_sms(to_phone, body, partner_id=None, media_url=None, media_type=None):
    client = get_twilio_client()
    if not client:
        return {'success': False, 'error': 'Twilio not configured'}
    
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
        if request.form['username'] == APP_USERNAME and request.form['password'] == APP_PASSWORD:
            session['logged_in'] = True
            return redirect(url_for('index'))
        return render_template('login.html', error='Invalid credentials')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    return redirect(url_for('login'))

@app.route('/')
@login_required
def index():
    return render_template('index.html')

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

# Serve uploaded media
@app.route('/uploads/<filename>')
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

# API: Partners
@app.route('/api/partners', methods=['GET', 'POST'])
@login_required
def api_partners():
    if request.method == 'POST':
        data = request.json
        
        partner = Partner(
            first_name=data['first_name'],
            last_name=data.get('last_name', ''),
            company=data.get('company', ''),
            phone=data['phone'],
            region_id=data.get('region_id'),
            tsd_id=data.get('tsd_id'),
            notes=data.get('notes', '')
        )
        
        # Add products
        if 'product_ids' in data:
            products = Product.query.filter(Product.id.in_(data['product_ids'])).all()
            partner.products = products
        
        db.session.add(partner)
        db.session.commit()
        
        return jsonify({'success': True, 'id': partner.id})
    
    # GET with filtering
    query = Partner.query
    
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
        'notes': p.notes,
        'is_new': p.is_new,
        'last_contacted': p.last_contacted.isoformat() if p.last_contacted else None,
        'created_at': p.created_at.isoformat()
    } for p in partners])

@app.route('/api/partners/<int:id>', methods=['GET', 'PUT', 'DELETE'])
@login_required
def api_partner(id):
    partner = Partner.query.get_or_404(id)
    
    if request.method == 'DELETE':
        db.session.delete(partner)
        db.session.commit()
        return jsonify({'success': True})
    
    if request.method == 'PUT':
        data = request.json
        partner.first_name = data.get('first_name', partner.first_name)
        partner.last_name = data.get('last_name', partner.last_name)
        partner.company = data.get('company', partner.company)
        partner.phone = data.get('phone', partner.phone)
        partner.region_id = data.get('region_id', partner.region_id)
        partner.tsd_id = data.get('tsd_id', partner.tsd_id)
        partner.notes = data.get('notes', partner.notes)
        
        if 'product_ids' in data:
            products = Product.query.filter(Product.id.in_(data['product_ids'])).all()
            partner.products = products
        
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
        'notes': partner.notes,
        'is_new': partner.is_new,
        'last_contacted': partner.last_contacted.isoformat() if partner.last_contacted else None
    })

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

# API: Messages/Conversations
@app.route('/api/conversations')
@login_required
def api_conversations():
    partners = Partner.query.all()
    conversations = []
    
    for partner in partners:
        latest = Message.query.filter_by(partner_id=partner.id).order_by(Message.created_at.desc()).first()
        unread = Message.query.filter_by(partner_id=partner.id, direction='inbound', status='received').count()
        
        if latest:  # Only show partners with messages
            conversations.append({
                'partner_id': partner.id,
                'name': partner.full_name,
                'company': partner.company,
                'phone': partner.phone,
                'latest_message': latest.body,
                'latest_time': latest.created_at.isoformat(),
                'has_media': latest.media_url is not None,
                'unread': unread,
                'direction': latest.direction
            })
    
    conversations.sort(key=lambda x: x['latest_time'], reverse=True)
    return jsonify(conversations)

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
    
    # If media_url is relative, make it absolute
    if media_url and not media_url.startswith('http'):
        media_url = f"{APP_BASE_URL}{media_url}"
    
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
    
    if media_url and not media_url.startswith('http'):
        media_url = f"{APP_BASE_URL}{media_url}"
    
    results = []
    for pid in partner_ids:
        partner = Partner.query.get(pid)
        if partner:
            message = personalize_message(message_template, partner)
            result = send_sms(partner.phone, message, partner.id, media_url, media_type)
            results.append({'partner': partner.full_name, 'result': result})
    
    return jsonify({'sent': len(results), 'results': results})

# API: Upload media
@app.route('/api/upload', methods=['POST'])
@login_required
def api_upload():
    if 'file' not in request.files:
        return jsonify({'success': False, 'error': 'No file provided'}), 400
    
    file = request.files['file']
    if file.filename == '':
        return jsonify({'success': False, 'error': 'No file selected'}), 400
    
    # Determine media type
    filename = secure_filename(file.filename)
    ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
    
    if ext in ['jpg', 'jpeg', 'png', 'gif']:
        media_type = 'image'
    elif ext in ['mp4', 'mov', 'avi', 'webm']:
        media_type = 'video'
    elif ext in ['mp3', 'wav', 'ogg', 'm4a', 'webm']:
        media_type = 'audio'
    else:
        return jsonify({'success': False, 'error': 'Unsupported file type'}), 400
    
    # Save with unique name
    unique_filename = f"{secrets.token_hex(8)}_{filename}"
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], unique_filename)
    file.save(filepath)
    
    # Save to database
    media = MediaFile(
        filename=unique_filename,
        original_name=filename,
        media_type=media_type
    )
    db.session.add(media)
    db.session.commit()
    
    return jsonify({
        'success': True,
        'id': media.id,
        'url': f"/uploads/{unique_filename}",
        'media_type': media_type
    })

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

init_db()

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
