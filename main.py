from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel, conint, confloat, EmailStr
import uuid
import warnings
import tempfile
import os
from typing import List, Optional
from datetime import datetime

from reportlab.pdfgen import canvas
from reportlab.graphics.barcode import code128
from reportlab.lib.pagesizes import landscape, A6
from reportlab.lib.units import mm

import stripe
from dotenv import load_dotenv

# ✅ SMTP imports (Google/Gmail SMTP)
import smtplib
from email.message import EmailMessage

# ✅ PostgreSQL
import psycopg2
from psycopg2.extras import RealDictCursor

# ================== Load Environment Variables ==================
load_dotenv()

ENV = os.getenv("ENV", "development")
FRONTEND_URL = os.getenv("FRONTEND_URL", "https://iluminous-candle-uk-fe.onrender.com")

if ENV == "production":
    STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY_LIVE")
else:
    STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY_TEST")

stripe.api_key = STRIPE_SECRET_KEY

# ✅ SMTP env vars (Render)
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
FROM_EMAIL = os.getenv("FROM_EMAIL")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL")

# ✅ Render Postgres connection string
DATABASE_URL = os.getenv("DATABASE_URL")  # provided by Render Postgres

warnings.filterwarnings("ignore", message="Unverified HTTPS request")

# ================== FastAPI Setup ==================
app = FastAPI(title="Luminous Candles API", version="1.0.0")

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

# ================== DB Helpers ==================
def get_db():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set. Create Postgres on Render and add env var.")
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)

def init_db():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id UUID PRIMARY KEY,
                customer_name TEXT,
                email TEXT,
                phone TEXT,
                address TEXT,
                city TEXT,
                state TEXT,
                zip TEXT,
                country TEXT,
                subtotal NUMERIC,
                tax NUMERIC,
                shipping NUMERIC,
                total NUMERIC,
                status TEXT DEFAULT 'paid',
                created_at TIMESTAMP DEFAULT NOW()
            );
            """)

            cur.execute("""
            CREATE TABLE IF NOT EXISTS order_items (
                id SERIAL PRIMARY KEY,
                order_id UUID REFERENCES orders(id) ON DELETE CASCADE,
                product_name TEXT,
                price NUMERIC,
                quantity INT
            );
            """)

            cur.execute("""
            CREATE TABLE IF NOT EXISTS shipping_labels (
                id SERIAL PRIMARY KEY,
                order_id UUID REFERENCES orders(id) ON DELETE CASCADE,
                label_path TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            );
            """)
        conn.commit()

@app.on_event("startup")
def startup_checks():
    # SMTP config check (optional but useful)
    missing = [k for k in ["SMTP_USER", "SMTP_PASSWORD", "FROM_EMAIL", "ADMIN_EMAIL"] if not os.getenv(k)]
    if missing:
        print(f"[WARN] Missing email env vars: {', '.join(missing)} (emails may fail)")

    # DB init
    init_db()

# ----------------- Root Route -----------------
@app.get("/", response_class=HTMLResponse)
async def home():
    return """
    <html>
      <head>
        <title>Luminous Candles API</title>
      </head>
      <body style="font-family: Arial; text-align: center; margin-top: 10%;">
        <h1>💡 Luminous Candles API</h1>
        <p>Backend running ✅</p>
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

class StatusUpdateRequest(BaseModel):
    status: str

# ----------------- In-memory Storage (temporary) -----------------
ORDERS_DB = {}

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

        allowed_countries = ["US", "CA", "GB", "DE"]

        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=line_items,
            mode="payment",
            success_url=f"{FRONTEND_URL}/success.html?checkoutId={checkout_id}",
            cancel_url=f"{FRONTEND_URL}/cancel.html",
            customer_email=customer.email,
            shipping_address_collection={"allowed_countries": allowed_countries},
        )

        print(f"✅ Stripe session created: {session.url}")
        return session.url

    except stripe.error.StripeError as e:
        raise HTTPException(status_code=400, detail=e.user_message or str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ----------------- Email Utility (Google SMTP) -----------------
def send_email(to_email: str, subject: str, html_content: str) -> bool:
    try:
        if not all([SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, FROM_EMAIL]):
            print("[ERROR] SMTP config missing.")
            return False

        msg = EmailMessage()
        msg["From"] = FROM_EMAIL
        msg["To"] = to_email
        msg["Subject"] = subject
        msg.set_content("This email requires HTML support.")
        msg.add_alternative(html_content, subtype="html")

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.send_message(msg)

        print(f"[OK] Email sent to {to_email}")
        return True
    except Exception as e:
        print(f"[ERROR] Email failed to {to_email}: {e}")
        return False

# ----------------- Tax Helper -----------------
def get_tax_rate_by_state(state: str) -> float:
    tax_rates = {
        "Alabama": 0.04, "Alaska": 0.00, "Arizona": 0.056, "Arkansas": 0.065,
        "California": 0.0725, "Colorado": 0.029, "Connecticut": 0.0635, "Delaware": 0.00,
        "Florida": 0.06, "Georgia": 0.04, "Hawaii": 0.04, "Idaho": 0.06,
        "Illinois": 0.0625, "Indiana": 0.07, "Iowa": 0.06, "Kansas": 0.065,
        "Kentucky": 0.06, "Louisiana": 0.0445, "Maine": 0.055, "Maryland": 0.06,
        "Massachusetts": 0.0625, "Michigan": 0.06, "Minnesota": 0.06875, "Mississippi": 0.07,
        "Missouri": 0.04225, "Montana": 0.00, "Nebraska": 0.055, "Nevada": 0.0685,
        "New Hampshire": 0.00, "New Jersey": 0.06625, "New Mexico": 0.05125, "New York": 0.04
    }
    return tax_rates.get(state, 0.07)

# ----------------- Label Generator (NO OVERLAP) -----------------
def generate_local_label(order: dict, customer: dict, order_id: str) -> str:
    try:
        os.makedirs("labels", exist_ok=True)
        label_path = os.path.join("labels", f"{order_id}.pdf")

        c = canvas.Canvas(label_path, pagesize=landscape(A6))
        width, height = landscape(A6)

        margin = 8 * mm
        barcode_height = 20 * mm
        barcode_padding = 6 * mm

        usable_top = height - margin
        usable_bottom = margin + barcode_height + barcode_padding

        logo_path = "images/LOGON.jpg"
        logo_w, logo_h = 22 * mm, 22 * mm

        if os.path.exists(logo_path):
            c.drawImage(
                logo_path,
                margin,
                usable_top - logo_h,
                width=logo_w,
                height=logo_h,
                preserveAspectRatio=True,
                mask="auto",
            )

        from_x = margin + logo_w + 6 * mm
        from_y = usable_top - 4 * mm

        c.setFont("Helvetica-Bold", 9)
        c.drawString(from_x, from_y, "FROM:")
        c.setFont("Helvetica", 8.5)

        sender_lines = [
            "Luminous Candles Ltd T/A Nelux Candles",
            "71–75 Shelton Street, Covent Garden",
            "London, WC2H 9JQ, United Kingdom",
        ]

        for i, line in enumerate(sender_lines):
            c.drawString(from_x, from_y - ((i + 1) * 4.5 * mm), line)

        to_start_y = usable_top - logo_h - 10 * mm
        c.setFont("Helvetica-Bold", 10)
        c.drawString(margin, to_start_y, "TO:")

        c.setFont("Helvetica-Bold", 12)
        to_lines = [
            customer.get("fullName", ""),
            customer.get("address", ""),
            f"{customer.get('city', '')}, {customer.get('state', '')} {customer.get('zip', '')}",
            customer.get("country", ""),
        ]

        line_gap = 6 * mm
        y_cursor = to_start_y - 6 * mm

        for line in to_lines:
            if y_cursor < usable_bottom + 6 * mm:
                break
            c.drawString(margin, y_cursor, line)
            y_cursor -= line_gap

        # Divider
        c.setLineWidth(1)
        c.line(margin, usable_bottom + 3 * mm, width - margin, usable_bottom + 3 * mm)

        # Barcode
        barcode = code128.Code128(order_id, barHeight=barcode_height, barWidth=0.5 * mm)
        barcode_x = (width - barcode.width) / 2
        barcode_y = margin
        barcode.drawOn(c, barcode_x, barcode_y)

        c.showPage()
        c.save()
        return label_path

    except Exception as e:
        print(f"[ERROR] Failed to generate label: {e}")
        return None

# ----------------- Checkout API -----------------
@app.post("/create-checkout-session")
async def create_checkout_session(request: CheckoutRequest):
    try:
        subtotal = sum(item.price * item.qty for item in request.cart)
        if subtotal < 0.5:
            raise HTTPException(status_code=400, detail="Order total must be at least £0.50")

        tax_rate = get_tax_rate_by_state(request.customer.state)
        tax = round(subtotal * tax_rate, 2)
        shipping = 5.99 if subtotal <= 50 else 0.0
        total = round(subtotal + tax + shipping, 2)

        checkout_id = str(uuid.uuid4())
        checkout_url = create_payment_link(request.cart, request.customer, total, checkout_id)

        # still keep temp memory store to fetch at payment-success
        ORDERS_DB[checkout_id] = {
            "id": checkout_id,
            "customer": request.customer.dict(),
            "cart": [i.dict() for i in request.cart],
            "subtotal": float(subtotal),
            "tax": float(tax),
            "shipping": float(shipping),
            "total": float(total),
        }

        return {"url": checkout_url}

    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

# ----------------- Payment Success (SAVE TO DB + LABEL PATH) -----------------
@app.post("/payment-success")
async def payment_success(req: SuccessRequest):
    if not req.checkoutId or req.checkoutId not in ORDERS_DB:
        raise HTTPException(status_code=404, detail="Order not found")

    order = ORDERS_DB[req.checkoutId]

    # Build email HTML
    items_html = "".join([
        f"<li>{item['qty']} × {item['name']} — £{item['price']*item['qty']:.2f}</li>"
        for item in order["cart"]
    ])

    html = f"""
    <h2>Order Confirmation</h2>
    <p>Thank you for your order, {req.customer.fullName}!</p>
    <p><b>Order ID:</b> {req.checkoutId}</p>
    <ul>{items_html}</ul>
    <p>Subtotal: £{order['subtotal']:.2f}<br>
       Tax: £{order['tax']:.2f}<br>
       Shipping: £{order['shipping']:.2f}<br>
       <b>Total: £{order['total']:.2f}</b></p>
    """

    # ✅ Generate label and KEEP it (do not delete)
    label_path = generate_local_label(order, req.customer.dict(), req.checkoutId)
    if not label_path:
        raise HTTPException(status_code=500, detail="Failed to generate shipping label")

    # ✅ Save order + items + label path into Postgres
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO orders (
                    id, customer_name, email, phone, address, city, state, zip, country,
                    subtotal, tax, shipping, total, status
                )
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (id) DO NOTHING;
            """, (
                req.checkoutId,
                req.customer.fullName,
                req.client_email,
                req.customer.phone,
                req.customer.address,
                req.customer.city,
                req.customer.state,
                req.customer.zip,
                req.customer.country,
                order["subtotal"],
                order["tax"],
                order["shipping"],
                order["total"],
                "paid"
            ))

            for item in order["cart"]:
                cur.execute("""
                    INSERT INTO order_items (order_id, product_name, price, quantity)
                    VALUES (%s, %s, %s, %s);
                """, (req.checkoutId, item["name"], item["price"], item["qty"]))

            cur.execute("""
                INSERT INTO shipping_labels (order_id, label_path)
                VALUES (%s, %s);
            """, (req.checkoutId, label_path))

        conn.commit()

    # ✅ Send customer email (no attachment)
    ok_customer = send_email(req.client_email, "Your Order Confirmation", html)
    if not ok_customer:
        # order is stored, so we can still proceed
        print("[WARN] Customer email failed but order saved.")

    return {"status": "success", "message": "Order saved. Label available for printing.", "order_id": req.checkoutId}

# ----------------- Admin: List Orders -----------------
@app.get("/admin/orders")
def admin_list_orders():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM orders ORDER BY created_at DESC;")
            return cur.fetchall()

# ----------------- Admin: Order Detail -----------------
@app.get("/admin/orders/{order_id}")
def admin_order_detail(order_id: str):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM orders WHERE id=%s;", (order_id,))
            order = cur.fetchone()
            if not order:
                raise HTTPException(status_code=404, detail="Order not found")

            cur.execute("SELECT * FROM order_items WHERE order_id=%s;", (order_id,))
            items = cur.fetchall()

            cur.execute("SELECT label_path FROM shipping_labels WHERE order_id=%s;", (order_id,))
            label = cur.fetchone()

            return {"order": order, "items": items, "label": label}

# ----------------- Admin: Download/Print Label -----------------
@app.get("/admin/labels/{order_id}")
def admin_get_label(order_id: str):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT label_path FROM shipping_labels WHERE order_id=%s;", (order_id,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Label not found")

            label_path = row["label_path"]
            if not os.path.exists(label_path):
                raise HTTPException(status_code=404, detail="Label file missing on server")

            return FileResponse(label_path, media_type="application/pdf", filename=f"{order_id}.pdf")

# ----------------- Admin: Update Order Status -----------------
@app.post("/admin/orders/{order_id}/status")
def admin_update_status(order_id: str, req: StatusUpdateRequest):
    allowed = {"paid", "processing", "shipped", "delivered", "cancelled", "refunded"}
    if req.status not in allowed:
        raise HTTPException(status_code=400, detail=f"Invalid status. Allowed: {sorted(allowed)}")

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE orders SET status=%s WHERE id=%s;", (req.status, order_id))
        conn.commit()

    return {"status": "ok", "order_id": order_id, "new_status": req.status}
