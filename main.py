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

# ✅ ASYNC POSTGRES
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
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL")

DATABASE_URL = os.getenv("DATABASE_URL")

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
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL not set")

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
    total: confloat(gt=0)

class SuccessRequest(BaseModel):
    customer: CustomerInfo
    cart: List[Item]
    total: confloat(gt=0)
    checkoutId: Optional[str]
    client_email: EmailStr

class StatusUpdateRequest(BaseModel):
    status: str

# ================== TEMP STORE ==================
ORDERS_DB = {}

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
    usable_bottom = margin + barcode_height + 6 * mm

    c.setFont("Helvetica-Bold", 12)
    c.drawString(margin, height - margin - 10, "TO:")
    y = height - margin - 20

    for line in [
        customer["fullName"],
        customer["address"],
        f'{customer["city"]}, {customer["state"]} {customer["zip"]}',
        customer["country"],
    ]:
        if y < usable_bottom:
            break
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
async def create_checkout(req: CheckoutRequest):
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
        } for i in req.cart],
        mode="payment",
        success_url=f"{FRONTEND_URL}/success.html?checkoutId={checkout_id}",
        cancel_url=f"{FRONTEND_URL}/cancel.html",
        customer_email=req.customer.email,
    )

    ORDERS_DB[checkout_id] = {
        "customer": req.customer.dict(),
        "cart": [i.dict() for i in req.cart],
        "total": req.total,
    }

    return {"url": session.url}

@app.post("/payment-success")
async def payment_success(req: SuccessRequest):
    order = ORDERS_DB.get(req.checkoutId)
    if not order:
        raise HTTPException(404, "Order not found")

    label_path = generate_local_label(req.checkoutId, req.customer.dict())

    async with db_pool.acquire() as conn:
        await conn.execute("""
        INSERT INTO orders (id, customer_name, email, phone, address, city, state, zip, country, total)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
        """,
        uuid.UUID(req.checkoutId),
        req.customer.fullName,
        req.client_email,
        req.customer.phone,
        req.customer.address,
        req.customer.city,
        req.customer.state,
        req.customer.zip,
        req.customer.country,
        order["total"]
        )

        for i in order["cart"]:
            await conn.execute("""
            INSERT INTO order_items (order_id, product_name, price, quantity)
            VALUES ($1,$2,$3,$4)
            """,
            uuid.UUID(req.checkoutId),
            i["name"],
            i["price"],
            i["qty"]
            )

        await conn.execute("""
        INSERT INTO shipping_labels (order_id, label_path)
        VALUES ($1,$2)
        """, uuid.UUID(req.checkoutId), label_path)

    send_email(req.client_email, "Order Confirmation", "<h2>Thank you for your order!</h2>")

    return {"status": "success", "order_id": req.checkoutId}

@app.get("/admin/orders")
async def admin_orders():
    async with db_pool.acquire() as conn:
        return await conn.fetch("SELECT * FROM orders ORDER BY created_at DESC")

@app.get("/admin/labels/{order_id}")
async def get_label(order_id: str):
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT label_path FROM shipping_labels WHERE order_id=$1",
            uuid.UUID(order_id),
        )
        if not row:
            raise HTTPException(404, "Label not found")
        return FileResponse(row["label_path"], media_type="application/pdf")
