# SilverSky Partner SMS Platform

SMS outreach platform for SilverSky partner management. Segment partners by region, TSD, and product. Send personalized texts, voice memos, and video messages.

## Features

- **Partner Management** — Track partners with region, TSD, and product assignments
- **Smart Segmentation** — Filter by region, TSD, product, or "new partners only"
- **Broadcast Messaging** — Send to filtered groups with personalization
- **Voice & Video** — Record and send voice memos or video messages
- **Two-Way Inbox** — Full conversation threads with reply notifications
- **Mobile PWA** — Install on your phone, works like a native app

## Quick Setup

### 1. Twilio Account

1. Sign up at [twilio.com](https://twilio.com)
2. Get **Account SID** and **Auth Token** from dashboard
3. Buy a phone number with SMS/MMS (~$1/month)
4. Register for 10DLC - Sole Proprietor (~$4 one-time)

### 2. Deploy to Railway

1. Push code to GitHub
2. [railway.app](https://railway.app) → New Project → Deploy from GitHub
3. Add environment variables:

```
TWILIO_ACCOUNT_SID=your_sid
TWILIO_AUTH_TOKEN=your_token
TWILIO_PHONE_NUMBER=+1234567890
APP_USERNAME=ira
APP_PASSWORD=your_password
SECRET_KEY=any_random_string
APP_BASE_URL=https://your-app.railway.app
```

Optional (for reply notifications):
```
NOTIFICATION_EMAIL=your@email.com
NOTIFICATION_SMS=+1234567890
SMTP_USER=your@gmail.com
SMTP_PASSWORD=app_password
```

### 3. Twilio Webhook

Phone Numbers → Your Number → Messaging:
- "When a message comes in": `https://your-app.railway.app/webhook/incoming`

### 4. Initial Setup

1. Login to your app
2. Settings → Add your regions (SoCal, NorCal, Denver, etc.)
3. Settings → Add TSDs (Pax8, Ingram, TD Synnex, etc.)
4. Products are pre-loaded (MxDR, Email Protection, Compliance)

### 5. Install on Phone

Chrome on Android → Menu → "Add to Home screen"

## Message Variables

Use in any message for personalization:
- `{{first_name}}` — First name
- `{{name}}` — Full name
- `{{company}}` — Company name
- `{{region}}` — Region name
- `{{tsd}}` — TSD name

Example: "Hey {{first_name}}, wanted to check in on {{company}}'s MxDR rollout..."

## Costs

- Railway: ~$5/month
- Twilio number: $1/month
- SMS: ~$0.0079 each
- MMS (media): ~$0.02 each
- 10DLC: ~$4 one-time

**Total: Under $10/month**
