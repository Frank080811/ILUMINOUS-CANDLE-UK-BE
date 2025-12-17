from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel, conint, confloat, EmailStr
import uuid
import os
from typing import List, Optional

from reportlab.pdfgen import canvas
from reportlab.graphics.barcode import code128
from reportlab.lib.pagesizes import landscape, A6
from reportlab.lib.units import mm

import stripe
from dotenv import load_dotenv

# SMTP
import smtplib
from email.message import EmailMessage

# ASYNC POSTGRES
import asyncpg

# ================== ENV ==================
load_dotenv()

ENV = os.getenv("ENV", "development")
FRONTEND_URL = os.getenv("FRONTEND_URL", "https://iluminous-candle-uk-fe.onrender.com")

STRIPE_SECRET_KEY = (
    os.getenv("STRIPE_SECRET_KEY_LIVE")
    if ENV == "production"
    else os.getenv("STRIPE_SECRET_KEY_TEST")
)
stripe.api_key = STRIPE_SECRET_KEY

SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
FROM_EMAIL = os.getenv("FROM_EMAIL")

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL not set")

# ================== APP ==================
app = FastAPI(title="Luminous Candles API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ================== DB POOL ==================
db_pool: asyncpg.Pool | None = None

@app.on_event("startup")
async def startup():
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL)

    async with db_pool.acquire() as conn:
        await conn.execute("""
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

        await conn.execute("""
        CREATE TABLE IF NOT EXISTS order_items (
            id SERIAL PRIMARY KEY,
            order_id UUID REFERENCES orders(id) ON DELETE CASCADE,
            product_name TEXT,
            price NUMERIC,
            quantity INT
        );
        """)

        await conn.execute("""
        CREATE TABLE IF NOT EXISTS shipping_labels (
            id SERIAL PRIMARY KEY,
            order_id UUID REFERENCES orders(id) ON DELETE CASCADE,
            label_path TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        );
        """)

@app.on_event("shutdown")
async def shutdown():
    if db_pool:
        await db_pool.close()

# ================== MODELS ==================
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

class SuccessRequest(BaseModel):
    checkoutId: str
    client_email: EmailStr

class StatusUpdateRequest(BaseModel):
    status: str

# ================== TEMP STORE ==================
ORDERS_DB: dict[str, dict] = {}

# ================== TAX HELPER ==================
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

# ================== EMAIL ==================
def send_email(to_email: str, subject: str, html: str) -> bool:
    try:
        msg = EmailMessage()
        msg["From"] = FROM_EMAIL
        msg["To"] = to_email
        msg["Subject"] = subject
        msg.set_content("HTML email required")
        msg.add_alternative(html, subtype="html")

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.send_message(msg)
        return True
    except Exception as e:
        print("[EMAIL ERROR]", e)
        return False

# ================== LABEL ==================
def generate_local_label(order_id: str, customer: dict) -> str:
    os.makedirs("labels", exist_ok=True)
    path = f"labels/{order_id}.pdf"

    c = canvas.Canvas(path, pagesize=landscape(A6))
    width, height = landscape(A6)

    margin = 8 * mm
    barcode_height = 20 * mm

    c.setFont("Helvetica-Bold", 12)
    c.drawString(margin, height - margin - 10, "TO:")

    y = height - margin - 22
    for line in [
        customer["fullName"],
        customer["address"],
        f'{customer["city"]}, {customer["state"]} {customer["zip"]}',
        customer["country"],
    ]:
        c.drawString(margin, y, line)
        y -= 14

    barcode = code128.Code128(order_id, barHeight=barcode_height)
    barcode.drawOn(c, (width - barcode.width) / 2, margin)

    c.showPage()
    c.save()
    return path

# ================== ROUTES ==================
@app.get("/", response_class=HTMLResponse)
async def home():
    return "<h1>Luminous Candles API ✅</h1>"

@app.post("/create-checkout-session")
async def create_checkout(request: CheckoutRequest):
    subtotal = sum(i.price * i.qty for i in request.cart)
    tax = round(subtotal * get_tax_rate_by_state(request.customer.state), 2)
    shipping = 5.99 if subtotal <= 50 else 0.0
    total = round(subtotal + tax + shipping, 2)

    checkout_id = str(uuid.uuid4())

    session = stripe.checkout.Session.create(
        payment_method_types=["card"],
        line_items=[{
            "price_data": {
                "currency": "gbp",
                "product_data": {"name": i.name},
                "unit_amount": int(i.price * 100),
            },
            "quantity": i.qty,
        } for i in request.cart],
        mode="payment",
        success_url=f"{FRONTEND_URL}/success.html?checkoutId={checkout_id}",
        cancel_url=f"{FRONTEND_URL}/cancel.html",
        customer_email=request.customer.email,
    )

    ORDERS_DB[checkout_id] = {
        "id": checkout_id,
        "customer": request.customer.dict(),
        "cart": [i.dict() for i in request.cart],
        "subtotal": subtotal,
        "tax": tax,
        "shipping": shipping,
        "total": total,
    }

    return {"url": session.url}

@app.post("/payment-success")
async def payment_success(req: SuccessRequest):
    order = ORDERS_DB.get(req.checkoutId)
    if not order:
        raise HTTPException(404, "Order not found")

    label_path = generate_local_label(req.checkoutId, order["customer"])

    async with db_pool.acquire() as conn:
        await conn.execute("""
        INSERT INTO orders (
            id, customer_name, email, phone, address, city, state, zip, country,
            subtotal, tax, shipping, total
        )
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
        """,
        uuid.UUID(req.checkoutId),
        order["customer"]["fullName"],
        req.client_email,
        order["customer"]["phone"],
        order["customer"]["address"],
        order["customer"]["city"],
        order["customer"]["state"],
        order["customer"]["zip"],
        order["customer"]["country"],
        order["subtotal"],
        order["tax"],
        order["shipping"],
        order["total"]
        )

        for item in order["cart"]:
            await conn.execute("""
            INSERT INTO order_items (order_id, product_name, price, quantity)
            VALUES ($1,$2,$3,$4)
            """,
            uuid.UUID(req.checkoutId),
            item["name"],
            item["price"],
            item["qty"]
            )

        await conn.execute("""
        INSERT INTO shipping_labels (order_id, label_path)
        VALUES ($1,$2)
        """, uuid.UUID(req.checkoutId), label_path)

    email_html = f"""
    <h2>Order Confirmation</h2>
    <p>Thank you {order['customer']['fullName']}!</p>
    <p><b>Order ID:</b> {req.checkoutId}</p>
    <p>Subtotal: £{order['subtotal']}</p>
    <p>Tax: £{order['tax']}</p>
    <p>Shipping: £{order['shipping']}</p>
    <h3>Total: £{order['total']}</h3>
    """

    send_email(req.client_email, "Your Order Confirmation", email_html)

    # ✅ CLEAR checkout after success
    ORDERS_DB.pop(req.checkoutId, None)

    return {"status": "success", "order_id": req.checkoutId}
