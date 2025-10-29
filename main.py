from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, conint, confloat, EmailStr
import uuid
import warnings
import tempfile
import os
from typing import List, Optional
from reportlab.pdfgen import canvas
from reportlab.graphics.barcode import code128
from reportlab.lib.pagesizes import landscape, A6
from reportlab.lib.units import mm
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail, Attachment, FileContent, FileName, FileType, Disposition
import base64
import stripe
from dotenv import load_dotenv

from database import SessionLocal, init_db, Order  # SQLAlchemy session + model


# ================== Load Environment Variables ==================
load_dotenv()

ENV = os.getenv("ENV", "development")  # "production" or "development"
FRONTEND_URL = os.getenv("FRONTEND_URL", "https://iluminous-candle-uk-fe.onrender.com")

if ENV == "production":
    STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY_LIVE")
else:
    STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY_TEST")

stripe.api_key = STRIPE_SECRET_KEY

SENDGRID_API_KEY = os.getenv("SENDGRID_API_KEY")
FROM_EMAIL = os.getenv("FROM_EMAIL")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL")

warnings.filterwarnings("ignore", message="Unverified HTTPS request")

# ================== FastAPI Setup ==================
app = FastAPI(title="Luminous Candles API", version="1.0.0")

init_db()

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        FRONTEND_URL,
        "https://iluminous-candle-uk-fe.onrender.com",
        "http://192.168.178.65:3033",
        "http://127.0.0.1:3033",
        "http://localhost:3033",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ----------------- Root Route -----------------
@app.get("/", response_class=HTMLResponse)
async def home():
    return f"""
    <html>
      <head>
        <title>Luminous Candles API</title>
        <style>
          body {{ font-family: Arial; text-align: center; margin-top: 10%; background: #fafafa; color: #333; }}
          h1 {{ color: #d4a017; }}
          p {{ font-size: 1.1em; }}
        </style>
      </head>
      <body>
        <h1>💡 Luminous Candles API</h1>
        <p>Your backend is running successfully!</p>
        <p>Use endpoints like <code>/create-checkout-session</code> to process orders.</p>
      </body>
    </html>
    """


# ----------------- Models -----------------
class Item(BaseModel):
    name: str
    price: confloat(gt=0)
    qty: conint(gt=0)

class CustomerInfo(BaseModel):
    fullName: str
    email: EmailStr
    phone: str
    address: str
    city: str
    state: str
    zip: str
    country: str

class CheckoutRequest(BaseModel):
    customer: CustomerInfo
    cart: List[Item]
    total: confloat(gt=0)

class SuccessRequest(BaseModel):
    customer: CustomerInfo
    cart: List[Item]
    total: confloat(gt=0)
    checkoutId: Optional[str] = None
    client_email: EmailStr


# ----------------- Email Utility -----------------
def send_email(to_email: str, subject: str, html_content: str, attachments: list[str] | None = None) -> bool:
    try:
        print(f"[EMAIL] Preparing to send to {to_email}")
        print(f"FROM_EMAIL={FROM_EMAIL}, SENDGRID_API_KEY={'SET' if SENDGRID_API_KEY else 'MISSING'}")

        message = Mail(
            from_email=FROM_EMAIL,
            to_emails=to_email,
            subject=subject,
            html_content=html_content,
        )
        message.content_subtype = "html"

        # Attachments (if any)
        if attachments:
            for filepath in attachments:
                if os.path.exists(filepath):
                    with open(filepath, "rb") as f:
                        encoded = base64.b64encode(f.read()).decode()
                        attachment = Attachment(
                            FileContent(encoded),
                            FileName(os.path.basename(filepath)),
                            FileType("application/pdf"),
                            Disposition("attachment"),
                        )
                        message.add_attachment(attachment)
                else:
                    print(f"[WARN] Attachment not found: {filepath}")

        sg = SendGridAPIClient(SENDGRID_API_KEY)
        response = sg.send(message)

        print(f"[SENDGRID RESPONSE] → {to_email}")
        print(f"Status: {response.status_code}")
        print(f"Body: {response.body}")
        print(f"Headers: {response.headers}")

        return response.status_code in (200, 202)

    except Exception as e:
        print(f"[ERROR] Email send failed → {to_email}: {e}")
        return False

# ----------------- Stripe Payment Link -----------------
def create_payment_link(items: List[Item], customer: CustomerInfo, total: float, checkout_id: str) -> str:
    try:
        line_items = [
            {
                "price_data": {
                    "currency": "gbp",
                    "product_data": {"name": item.name},
                    "unit_amount": int(item.price * 100),
                },
                "quantity": item.qty,
            }
            for item in items
        ]

        subtotal = sum(item.price * item.qty for item in items)
        shipping = 5.99 if subtotal <= 50 else 0.0

        if shipping > 0:
            line_items.append({
                "price_data": {
                    "currency": "gbp",
                    "product_data": {"name": "Shipping"},
                    "unit_amount": int(shipping * 100),
                },
                "quantity": 1,
            })

        allowed_countries = ["US", "CA", "GB", "DE"]

        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=line_items,
            mode="payment",
            success_url=f"{FRONTEND_URL}/success.html?checkoutId={checkout_id}",
            cancel_url=f"{FRONTEND_URL}/cancel.html",
            customer_email=customer.email,
            shipping_address_collection={"allowed_countries": allowed_countries},
            client_reference_id=checkout_id,  # ✅ so webhook can match order
        )

        print(f"✅ Stripe session created: {session.url}")
        return session.url

    except stripe.error.StripeError as e:
        print("[STRIPE ERROR]", e.user_message or str(e))
        raise HTTPException(status_code=400, detail=e.user_message or str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ----------------- Label Generator -----------------
def generate_local_label(order_obj: Order, customer: dict, order_id: str) -> str | None:
    try:
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
        c = canvas.Canvas(tmp.name, pagesize=landscape(A6))
        width, height = landscape(A6)

        # Margins
        top_margin = 8 * mm
        side_margin = 8 * mm
        bottom_margin = 20 * mm  # Increased to give more room for barcode

        # Logo
        logo_path = "images/LOGON.jpg"
        logo_w, logo_h = 25 * mm, 25 * mm
        y_top = height - top_margin
        if os.path.exists(logo_path):
            c.drawImage(
                logo_path,
                side_margin,
                y_top - logo_h,
                width=logo_w,
                height=logo_h,
                preserveAspectRatio=True,
                mask="auto",
            )

        # FROM section
        from_x = side_margin + logo_w + 6 * mm
        from_y = y_top - 6 * mm
        c.setFont("Helvetica-Bold", 10)
        c.drawString(from_x, from_y, "FROM:")
        c.setFont("Helvetica", 9)
        sender_lines = [
            "Luminous Candles Ltd T/A Nelux Candles",
            "71-75, Shelton Street, Covent Garden,",
            "London, United Kingdom, WC2H 9JQ",
        ]
        for i, line in enumerate(sender_lines):
            c.drawString(from_x, from_y - ((i + 1) * 5 * mm), line)

        # TO section (moved slightly upward)
        c.setFont("Helvetica-Bold", 11)
        y_to_start = y_top - logo_h - 10 * mm
        c.drawString(side_margin, y_to_start, "TO:")

        c.setFont("Helvetica-Bold", 13)
        line_gap = 6 * mm
        to_lines = [
            customer.get("fullName", ""),
            customer.get("address", ""),
            f"{customer.get('city', '')}, {customer.get('state', '')} {customer.get('zip', '')}",
            customer.get("country", "GB"),
        ]
        start_y = y_to_start - 4 * mm
        for i, text in enumerate(to_lines):
            c.drawCentredString(width / 2, start_y - (i * line_gap), text)

        # Barcode centered at bottom with margin
        barcode = code128.Code128(order_id, barHeight=18 * mm, barWidth=0.45 * mm)
        barcode_x = (width - barcode.width) / 2
        barcode_y = bottom_margin
        barcode.drawOn(c, barcode_x, barcode_y)

        c.showPage()
        c.save()
        return tmp.name

    except Exception as e:
        print(f"[ERROR] Failed to generate label: {e}")
        return None


# ----------------- Checkout API -----------------
@app.post("/create-checkout-session")
async def create_checkout_session(request: CheckoutRequest):
    print("✅ Checkout request received:", request.dict())
    db = SessionLocal()
    try:
        subtotal = sum(item.price * item.qty for item in request.cart)
        if subtotal < 0.5:
            raise HTTPException(status_code=400, detail="Order total must be at least £0.50")

        shipping = 5.99 if subtotal <= 50 else 0.0
        total = round(subtotal + shipping, 2)  # ✅ Tax removed

        checkout_id = str(uuid.uuid4())
        checkout_url = create_payment_link(request.cart, request.customer, total, checkout_id)

        order = Order(
            id=checkout_id,
            customer=request.customer.dict(),
            cart=[i.dict() for i in request.cart],
            subtotal=float(subtotal),
            shipping=float(shipping),
            total=float(total),
        )
        db.add(order)
        db.commit()

        return {"url": checkout_url}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        db.close()


# ----------------- Order Fetch -----------------
@app.get("/order/{checkout_id}")
async def get_order(checkout_id: str):
    db = SessionLocal()
    try:
        order = db.query(Order).filter(Order.id == checkout_id).first()
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")
        return {
            "id": order.id,
            "customer": order.customer,
            "cart": order.cart,
            "subtotal": order.subtotal,
            "shipping": order.shipping,
            "total": order.total,
        }
    finally:
        db.close()


# ----------------- Payment Success (manual success page) -----------------
@app.post("/payment-success")
async def payment_success(req: SuccessRequest):
    """
    Triggered by success.html after checkout completes.
    Sends confirmation email to client and admin with label PDF.
    """
    db = SessionLocal()
    try:
        order = db.query(Order).filter(Order.id == req.checkoutId).first()
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")

        # 🧾 Build HTML email body
        items_html = "".join(
            f"<li>{item['qty']} × {item['name']} — £{float(item['price']) * int(item['qty']):.2f}</li>"
            for item in order.cart
        )

        html = f"""
        <h2>Order Confirmation</h2>
        <p>Thank you for your order, {req.customer.fullName}!</p>
        <p><b>Order ID:</b> {req.checkoutId}</p>
        <ul>{items_html}</ul>
        <p>Subtotal: £{order.subtotal:.2f}<br>
           Shipping: £{order.shipping:.2f}<br>
           <b>Total: £{order.total:.2f}</b></p>
        """

        # 📨 Send to customer
        sent_to_customer = send_email(req.client_email, "Your Order Confirmation", html)

        # 📦 Generate and attach label PDF (optional)
        label = generate_local_label(order, req.customer.dict(), req.checkoutId or order.id)

        # 📨 Send to admin
        if label:
            sent_to_admin = send_email(ADMIN_EMAIL, f"New Order ({req.checkoutId})", html, [label])
        else:
            sent_to_admin = send_email(ADMIN_EMAIL, f"New Order ({req.checkoutId})", html)

        if sent_to_customer or sent_to_admin:
            print(f"[EMAIL SUCCESS] Notifications sent for order {req.checkoutId}")
            return {"status": "success", "message": "Emails sent successfully"}
        else:
            print(f"[EMAIL FAIL] Unable to send one or more emails for order {req.checkoutId}")
            raise HTTPException(status_code=500, detail="Failed to send emails")

    except Exception as e:
        print(f"[ERROR] /payment-success failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()



# ----------------- Stripe Webhook -----------------
@app.post("/stripe-webhook")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")
    endpoint_secret = os.getenv("STRIPE_WEBHOOK_SECRET")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, endpoint_secret)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid payload")
    except stripe.error.SignatureVerificationError:
        raise HTTPException(status_code=400, detail="Invalid signature")

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        checkout_id = session.get("client_reference_id")
        customer_email = session.get("customer_email")

        if checkout_id:
            db = SessionLocal()
            try:
                order = db.query(Order).filter(Order.id == checkout_id).first()
                if order:
                    html = f"""
                    <h2>Order Confirmed</h2>
                    <p><b>Order ID:</b> {order.id}</p>
                    <p>Total: £{order.total:.2f}</p>
                    """
                    if customer_email:
                        send_email(customer_email, "Your Order Confirmation", html)
                    send_email(ADMIN_EMAIL, f"New Order ({order.id})", html)
            finally:
                db.close()

    return {"status": "success"}
