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

import smtplib
from email.message import EmailMessage

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
FROM_EMAIL = os.getenv("FROM_EMAIL") or SMTP_USER
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL")

DATABASE_URL = os.getenv("DATABASE_URL")

# ================== APP ==================
app = FastAPI(title="Luminous Candles API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_URL, "*"],
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

    # Helpful startup logs
    missing_smtp = [k for k in ["SMTP_USER", "SMTP_PASSWORD", "FROM_EMAIL"] if not os.getenv(k)]
    if missing_smtp:
        print(f"[WARN] Missing SMTP env vars: {missing_smtp} (emails may fail)")
    print("[OK] Startup complete. DB connected.")

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
    total: confloat(gt=0)

class SuccessRequest(BaseModel):
    customer: CustomerInfo
    cart: List[Item]
    total: confloat(gt=0)
    checkoutId: Optional[str] = None
    client_email: EmailStr

class StatusUpdateRequest(BaseModel):
    status: str

# ================== TEMP STORE (still used for /order preview) ==================
ORDERS_DB = {}

# ================== EMAIL ==================
def send_email(to_email: str, subject: str, html: str) -> bool:
    try:
        if not SMTP_USER or not SMTP_PASSWORD:
            print("[EMAIL ERROR] SMTP_USER/SMTP_PASSWORD missing.")
            return False

        # Gmail is strict: FROM should generally be the authenticated mailbox
        from_addr = FROM_EMAIL or SMTP_USER

        msg = EmailMessage()
        msg["From"] = from_addr
        msg["To"] = to_email
        msg["Subject"] = subject
        msg.set_content("HTML email required")
        msg.add_alternative(html, subtype="html")

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.send_message(msg)

        print(f"[OK] Email sent to {to_email}")
        return True

    except Exception as e:
        print(f"[EMAIL ERROR] Failed to {to_email}: {repr(e)}")
        return False

# ================== LABEL (NO OVERLAP) ==================
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

    lines = [
        customer.get("fullName", ""),
        customer.get("address", ""),
        f'{customer.get("city","")}, {customer.get("state","")} {customer.get("zip","")}',
        customer.get("country", ""),
    ]

    for line in lines:
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
    if not STRIPE_SECRET_KEY:
        raise HTTPException(500, "Stripe key not configured")

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

    # Store pending order so /order/{id} can show it
    ORDERS_DB[checkout_id] = {
        "id": checkout_id,
        "customer": req.customer.dict(),
        "cart": [i.dict() for i in req.cart],
        "total": float(req.total),
        "status": "pending",
    }

    print(f"[OK] Checkout created: {checkout_id}")
    return {"url": session.url, "checkoutId": checkout_id}

# ✅ FIX: frontend calls this. Return pending from memory OR saved from DB if already inserted.
@app.get("/order/{checkout_id}")
async def get_order(checkout_id: str):
    # first check memory (pending)
    if checkout_id in ORDERS_DB:
        return ORDERS_DB[checkout_id]

    # then check DB (paid)
    try:
        oid = uuid.UUID(checkout_id)
    except Exception:
        raise HTTPException(400, "Invalid order id")

    async with db_pool.acquire() as conn:
        order = await conn.fetchrow("SELECT * FROM orders WHERE id=$1", oid)
        if not order:
            raise HTTPException(404, "Order not found")

        items = await conn.fetch("SELECT product_name, price, quantity FROM order_items WHERE order_id=$1", oid)
        label = await conn.fetchrow("SELECT label_path FROM shipping_labels WHERE order_id=$1", oid)

        return {
            "id": str(order["id"]),
            "customer": {
                "fullName": order["customer_name"],
                "email": order["email"],
                "phone": order["phone"],
                "address": order["address"],
                "city": order["city"],
                "state": order["state"],
                "zip": order["zip"],
                "country": order["country"],
            },
            "cart": [
                {"name": r["product_name"], "price": float(r["price"]), "qty": int(r["quantity"])}
                for r in items
            ],
            "total": float(order["total"]) if order["total"] is not None else 0.0,
            "status": order["status"],
            "label_path": label["label_path"] if label else None,
        }

@app.post("/payment-success")
async def payment_success(req: SuccessRequest):
    if not req.checkoutId:
        raise HTTPException(400, "checkoutId is required")

    print(f"[INFO] payment-success called for checkoutId={req.checkoutId}")

    # Use memory if available
    order = ORDERS_DB.get(req.checkoutId)

    # If memory missing, still proceed using request payload (more reliable on Render)
    cart = [i.dict() for i in req.cart]
    customer = req.customer.dict()

    if order:
        total = float(order.get("total", req.total))
    else:
        total = float(req.total)
        print("[WARN] ORDERS_DB missing. Proceeding using request payload.")

    # Generate label
    label_path = generate_local_label(req.checkoutId, customer)

    # Insert into DB (idempotent)
    try:
        oid = uuid.UUID(req.checkoutId)
    except Exception:
        raise HTTPException(400, "Invalid checkoutId format")

    async with db_pool.acquire() as conn:
        async with conn.transaction():
            # Upsert order
            await conn.execute("""
            INSERT INTO orders (
                id, customer_name, email, phone, address, city, state, zip, country,
                total, status
            )
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,'paid')
            ON CONFLICT (id) DO UPDATE
            SET status='paid';
            """,
            oid,
            customer["fullName"],
            req.client_email,
            customer["phone"],
            customer["address"],
            customer["city"],
            customer["state"],
            customer["zip"],
            customer["country"],
            total
            )

            # Clear existing items (idempotent)
            await conn.execute("DELETE FROM order_items WHERE order_id=$1", oid)

            for i in cart:
                await conn.execute("""
                INSERT INTO order_items (order_id, product_name, price, quantity)
                VALUES ($1,$2,$3,$4)
                """, oid, i["name"], i["price"], i["qty"])

            # Upsert label
            await conn.execute("""
            INSERT INTO shipping_labels (order_id, label_path)
            VALUES ($1,$2)
            ON CONFLICT DO NOTHING
            """, oid, label_path)

    # Email (log success/failure)
    html = f"""
    <h2>Order Confirmation</h2>
    <p>Thank you for your order, {customer["fullName"]}!</p>
    <p><b>Order ID:</b> {req.checkoutId}</p>
    <p><b>Total:</b> £{total:.2f}</p>
    """

    email_ok = send_email(req.client_email, "Your Order Confirmation", html)
    if not email_ok:
        print("[WARN] Email failed, but order was saved to DB.")

    # update memory status (optional)
    if req.checkoutId in ORDERS_DB:
        ORDERS_DB[req.checkoutId]["status"] = "paid"

    return {"status": "success", "order_id": req.checkoutId, "email_sent": email_ok}

@app.get("/admin/orders")
async def admin_orders():
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM orders ORDER BY created_at DESC")
        return [dict(r) for r in rows]

@app.get("/admin/labels/{order_id}")
async def get_label(order_id: str):
    try:
        oid = uuid.UUID(order_id)
    except Exception:
        raise HTTPException(400, "Invalid order id")

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT label_path FROM shipping_labels WHERE order_id=$1",
            oid,
        )
        if not row:
            raise HTTPException(404, "Label not found")

        label_path = row["label_path"]
        if not os.path.exists(label_path):
            raise HTTPException(404, "Label file missing on server")

        return FileResponse(label_path, media_type="application/pdf", filename=f"{order_id}.pdf")
