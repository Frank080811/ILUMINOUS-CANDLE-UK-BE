from fastapi import (
    FastAPI,
    HTTPException,
    Request,
    Depends
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from pydantic import BaseModel, conint, confloat, EmailStr
from typing import List
from datetime import datetime, timedelta
import uuid, os, io, logging, tempfile

import stripe
import asyncpg
import smtplib
from email.message import EmailMessage
from dotenv import load_dotenv

from passlib.context import CryptContext
from jose import jwt

from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A6, landscape
from reportlab.lib.units import mm
from reportlab.graphics.barcode import qr
from reportlab.graphics import renderPDF
from reportlab.graphics.shapes import Drawing
from PyPDF2 import PdfMerger

from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

security = HTTPBearer()

# ================= ENV =================
load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
FRONTEND_URL = os.getenv("FRONTEND_URL")

SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
FROM_EMAIL = os.getenv("FROM_EMAIL")

stripe.api_key = os.getenv("STRIPE_SECRET_KEY_TEST")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")

ADMIN_EMAIL = os.getenv("ADMIN_EMAIL")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")
SECRET_KEY = os.getenv("ADMIN_JWT_SECRET")
ALGORITHM = "HS256"

# ================= LOGGING =================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("luminous-api")

# ================= APP =================
app = FastAPI(title="Luminous Candles API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_headers=["*"],
    allow_methods=["*"]
)

# ================= SECURITY =================
from passlib.context import CryptContext
import os

pwd_context = CryptContext(
    schemes=["bcrypt"],
    deprecated="auto"
)

ADMIN_EMAIL = os.getenv("ADMIN_EMAIL")
ADMIN_PASSWORD_HASH = os.getenv("ADMIN_PASSWORD_HASH")

if not ADMIN_EMAIL:
    raise RuntimeError("ADMIN_EMAIL is missing in environment variables")

if not ADMIN_PASSWORD_HASH:
    raise RuntimeError("ADMIN_PASSWORD_HASH is missing in environment variables")


def admin_required(
    token: HTTPAuthorizationCredentials = Depends(security)
):
    try:
        return jwt.decode(
            token.credentials,
            SECRET_KEY,
            algorithms=[ALGORITHM]
        )
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired token")


# ================= DB =================
db_pool: asyncpg.Pool | None = None

@app.on_event("startup")
async def startup():
    global db_pool

    # Create connection pool
    db_pool = await asyncpg.create_pool(
        DATABASE_URL,
        min_size=1,
        max_size=10
    )

    async with db_pool.acquire() as c:

        # ---------------- ORDERS TABLE ----------------
        await c.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id UUID PRIMARY KEY,
            status TEXT NOT NULL,
            customer_name TEXT,
            email TEXT,
            phone TEXT,
            address TEXT,
            city TEXT,
            state TEXT,
            zip TEXT,
            country TEXT,
            subtotal NUMERIC(10,2),
            tax NUMERIC(10,2),
            shipping NUMERIC(10,2),
            total NUMERIC(10,2),
            stripe_session_id TEXT,
            email_sent BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW()
        );
        """)

        # ---------------- ORDER ITEMS ----------------
        await c.execute("""
        CREATE TABLE IF NOT EXISTS order_items (
            id SERIAL PRIMARY KEY,
            order_id UUID NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
            product_name TEXT NOT NULL,
            price NUMERIC(10,2) NOT NULL,
            quantity INT NOT NULL CHECK (quantity > 0)
        );
        """)

        # ---------------- SHIPPING LABELS ----------------
        await c.execute("""
        CREATE TABLE IF NOT EXISTS shipping_labels (
            id UUID PRIMARY KEY,
            order_id UUID NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
            label_pdf BYTEA NOT NULL,
            created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW()
        );
        """)

        # ---------------- PERFORMANCE INDEXES ----------------
        await c.execute("""
        CREATE INDEX IF NOT EXISTS idx_orders_status
        ON orders(status);
        """)

        await c.execute("""
        CREATE INDEX IF NOT EXISTS idx_orders_created_at
        ON orders(created_at);
        """)

        await c.execute("""
        CREATE INDEX IF NOT EXISTS idx_order_items_order_id
        ON order_items(order_id);
        """)

    print("✅ Database initialized successfully")


# ================= MODELS =================
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

class CheckoutTotals(BaseModel):
    subtotal: confloat(ge=0)
    tax: confloat(ge=0)
    shipping: confloat(ge=0)
    total: confloat(ge=0)

class CheckoutRequest(BaseModel):
    customer: CustomerInfo
    cart: List[Item]
    totals: CheckoutTotals

class AdminLogin(BaseModel):
    email: str
    password: str

# ================= EMAIL =================
def send_order_confirmation_email(to_email: str, order: dict, items: list[dict]):
    msg = EmailMessage()
    msg["From"] = FROM_EMAIL
    msg["To"] = to_email
    msg["Subject"] = f"Order Confirmation – {order['id']}"

    lines = []
    for i in items:
        total = float(i["price"]) * i["quantity"]
        lines.append(f"{i['product_name']} × {i['quantity']} – £{total:.2f}")

    msg.set_content(f"""
Thank you for your order!

Order ID: {order['id']}

{chr(10).join(lines)}

Total Paid: £{order['total']:.2f}
""")

    with smtplib.SMTP("smtp.gmail.com", 587) as s:
        s.starttls()
        s.login(SMTP_USER, SMTP_PASSWORD)
        s.send_message(msg)

# ================= ADMIN LOGIN =================
# ================= ADMIN LOGIN =================
@app.post("/admin/login")
async def admin_login(data: AdminLogin):

    # 1️⃣ Email check
    if data.email.strip().lower() != ADMIN_EMAIL.lower():
        raise HTTPException(status_code=401, detail="Invalid credentials")

    # 2️⃣ Password check (bcrypt-safe, 72-byte limit)
    password = data.password.encode("utf-8")[:72]

    if not pwd_context.verify(password, ADMIN_PASSWORD_HASH):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    # 3️⃣ Issue JWT
    token = jwt.encode(
        {
            "sub": data.email,
            "exp": datetime.utcnow() + timedelta(hours=8)
        },
        SECRET_KEY,
        algorithm=ALGORITHM
    )

    return {
        "access_token": token,
        "token_type": "bearer"
    }


# ================= CHECKOUT =================
@app.post("/create-checkout-session")
async def create_checkout(req: CheckoutRequest):

    order_id = uuid.uuid4()

    # ---------------- BUILD STRIPE LINE ITEMS ----------------
    line_items = []

    for i in req.cart:
        line_items.append({
            "price_data": {
                "currency": "usd",
                "product_data": {"name": i.name},
                "unit_amount": int(i.price * 100),
            },
            "quantity": i.qty,
        })

    if req.totals.tax > 0:
        line_items.append({
            "price_data": {
                "currency": "usd",
                "product_data": {"name": "Tax"},
                "unit_amount": int(req.totals.tax * 100),
            },
            "quantity": 1,
        })

    if req.totals.shipping > 0:
        line_items.append({
            "price_data": {
                "currency": "usd",
                "product_data": {"name": "Shipping"},
                "unit_amount": int(req.totals.shipping * 100),
            },
            "quantity": 1,
        })

    # ---------------- CREATE STRIPE SESSION ----------------
    session = stripe.checkout.Session.create(
        mode="payment",
        line_items=line_items,
        success_url=f"{FRONTEND_URL}/success.html?order_id={order_id}",
        cancel_url=f"{FRONTEND_URL}/cancel.html",
        customer_email=req.customer.email,
        metadata={"order_id": str(order_id)},
    )

    # ---------------- SAVE ORDER TO DATABASE ----------------
    async with db_pool.acquire() as c:

        await c.execute(
            """
            INSERT INTO orders (
                id,
                status,
                customer_name,
                email,
                phone,
                address,
                city,
                state,
                zip,
                country,
                subtotal,
                tax,
                shipping,
                total,
                stripe_session_id,
                email_sent,
                created_at
            ) VALUES (
                $1, 'PENDING', $2, $3, $4,
                $5, $6, $7, $8, $9,
                $10, $11, $12, $13,
                $14, FALSE, NOW()
            )
            """,
            order_id,
            req.customer.fullName,
            req.customer.email,
            req.customer.phone,
            req.customer.address,
            req.customer.city,
            req.customer.state,
            req.customer.zip,
            req.customer.country,
            req.totals.subtotal,
            req.totals.tax,
            req.totals.shipping,
            req.totals.total,
            session.id
        )

        # ---------------- SAVE ORDER ITEMS ----------------
        for item in req.cart:
            await c.execute(
                """
                INSERT INTO order_items (
                    order_id,
                    product_name,
                    price,
                    quantity
                ) VALUES (
                    $1, $2, $3, $4
                )
                """,
                order_id,
                item.name,
                item.price,
                item.qty
            )

    # ---------------- RESPONSE ----------------
    return {
        "url": session.url,
        "orderId": str(order_id)
    }

# ================= STRIPE WEBHOOK =================
@app.post("/stripe/webhook")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig = request.headers.get("stripe-signature")

    try:
        event = stripe.Webhook.construct_event(
            payload, sig, STRIPE_WEBHOOK_SECRET
        )
    except Exception:
        raise HTTPException(400, "Invalid Stripe signature")

    if event["type"] != "checkout.session.completed":
        return {"status": "ignored"}

    session = event["data"]["object"]
    order_id = uuid.UUID(session["metadata"]["order_id"])

    async with db_pool.acquire() as c:
        order = await c.fetchrow(
            "SELECT * FROM orders WHERE id=$1",
            order_id
        )

        if not order:
            raise HTTPException(404, "Order not found")

        if order["status"] == "PAID":
            return {"status": "already_processed"}

        # ✅ 1. Mark order PAID
        await c.execute(
            "UPDATE orders SET status='PAID' WHERE id=$1",
            order_id
        )

        # ✅ 2. Generate shipping label ONCE
        label_exists = await c.fetchval(
            "SELECT 1 FROM shipping_labels WHERE order_id=$1",
            order_id
        )

        if not label_exists:
            label_pdf = generate_shipping_label(dict(order))

            await c.execute(
                """
                INSERT INTO shipping_labels (id, order_id, label_pdf)
                VALUES ($1, $2, $3)
                """,
                uuid.uuid4(),
                order_id,
                label_pdf
            )

        # ✅ 3. Fetch order items
        items = await c.fetch(
            """
            SELECT product_name, price, quantity
            FROM order_items
            WHERE order_id=$1
            """,
            order_id
        )

        # ✅ 4. Send confirmation email ONCE
        if not order["email_sent"]:
            send_order_confirmation_email(
                order["email"],
                dict(order),
                [dict(i) for i in items]
            )

            await c.execute(
                "UPDATE orders SET email_sent=TRUE WHERE id=$1",
                order_id
            )

    return {"status": "ok"}

def generate_shipping_label(order: dict) -> bytes:
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
    c = canvas.Canvas(tmp.name, pagesize=landscape(A6))
    width, height = landscape(A6)

    # ================= CONSTANTS =================
    MARGIN = 12 * mm
    LINE = 12
    FONT_SMALL = 8
    FONT_NORMAL = 10
    FONT_LARGE = 14

    # ================= HEADER =================
    header_y = height - MARGIN

    # Logo (small, safe)
    logo_path = "assets/logo.png"
    logo_size = 20 * mm

    if os.path.exists(logo_path):
        c.drawImage(
            logo_path,
            MARGIN,
            header_y - logo_size,
            width=logo_size,
            height=logo_size,
            preserveAspectRatio=True,
            mask="auto"
        )

    # Title
    c.setFont("Helvetica-Bold", FONT_LARGE)
    c.drawCentredString(
        width / 2,
        header_y - 6,
        "Shipping Label"
    )

    # ================= FROM BLOCK =================
    from_x = MARGIN + logo_size + 10
    from_y = header_y - 10

    c.setFont("Helvetica-Bold", FONT_SMALL)
    c.drawString(from_x, from_y, "FROM:")

    c.setFont("Helvetica", FONT_SMALL)
    from_lines = [
        "Luminous Candles Ltd",
        "71–75 Shelton Street",
        "Covent Garden",
        "London WC2H 9JQ",
        "United Kingdom"
    ]

    y = from_y - LINE
    for line in from_lines:
        c.drawString(from_x, y, line)
        y -= LINE

    # ================= TO BLOCK =================
    to_start_y = header_y - logo_size - 35

    c.setFont("Helvetica-Bold", FONT_NORMAL)
    c.drawString(MARGIN, to_start_y, "TO:")

    c.setFont("Helvetica-Bold", 12)
    to_lines = [
        order["customer_name"],
        order["address"],
        f"{order['city']}, {order['state']} {order['zip']}",
        order["country"]
    ]

    y = to_start_y - 16
    for line in to_lines:
        c.drawString(MARGIN, y, line)
        y -= 16

    # ================= ORDER ID =================
    c.setFont("Helvetica", FONT_SMALL)
    c.drawString(
        MARGIN,
        22,
        f"Order ID: {order['id']}"
    )

    # ================= QR CODE =================
    qr_size = 30 * mm
    qr_x = width - MARGIN - qr_size
    qr_y = 18

    qr_widget = qr.QrCodeWidget(str(order["id"]))
    bounds = qr_widget.getBounds()

    d = Drawing(
        qr_size,
        qr_size,
        transform=[
            qr_size / (bounds[2] - bounds[0]), 0, 0,
            qr_size / (bounds[3] - bounds[1]), 0, 0
        ]
    )
    d.add(qr_widget)

    renderPDF.draw(d, c, qr_x, qr_y)

    c.setFont("Helvetica", 7)
    c.drawCentredString(
        qr_x + qr_size / 2,
        qr_y - 6,
        "Scan for Order"
    )

    # ================= FINALIZE =================
    c.showPage()
    c.save()

    with open(tmp.name, "rb") as f:
        pdf = f.read()

    os.unlink(tmp.name)
    return pdf



    # ================= HEADER =================
    c.setFont("Helvetica-Bold", 14)
    c.drawCentredString(
        width / 2,
        height - margin_y - 6,
        "Shipping Label"
    )

    # ================= FROM =================
    from_x = margin_x + logo_width + 8
    from_y = height - 30

    c.setFont("Helvetica-Bold", 9)
    c.drawString(from_x, from_y, "FROM:")

    c.setFont("Helvetica", 8)
    from_lines = [
        "Luminous Candles Ltd",
        "71–75 Shelton Street",
        "Covent Garden",
        "London WC2H 9JQ",
        "United Kingdom"
    ]

    y = from_y - 10
    for line in from_lines:
        c.drawString(from_x, y, line)
        y -= 9

    # ================= TO =================
    c.setFont("Helvetica-Bold", 10)
    c.drawString(margin_x, height - 95, "TO:")

    c.setFont("Helvetica-Bold", 12)
    to_lines = [
        order["customer_name"],
        order["address"],
        f"{order['city']}, {order['state']} {order['zip']}",
        order["country"]
    ]

    y = height - 112
    for line in to_lines:
        c.drawString(margin_x, y, line)
        y -= 14

    # ================= ORDER INFO =================
    c.setFont("Helvetica", 8)
    c.drawString(margin_x, 36, f"Order ID: {order['id']}")
    c.drawString(margin_x, 24, f"Total: £{float(order['total']):.2f}")

    # ================= QR CODE =================
    qr_size = 32 * mm
    qr_x = width - margin_x - qr_size
    qr_y = margin_y + 4

    qr_widget = qr.QrCodeWidget(str(order["id"]))
    bounds = qr_widget.getBounds()

    d = Drawing(
        qr_size,
        qr_size,
        transform=[
            qr_size / (bounds[2] - bounds[0]), 0, 0,
            qr_size / (bounds[3] - bounds[1]), 0, 0
        ]
    )
    d.add(qr_widget)

    renderPDF.draw(d, c, qr_x, qr_y)

    c.setFont("Helvetica", 7)
    c.drawCentredString(
        qr_x + qr_size / 2,
        qr_y - 8,
        "Scan for Order"
    )

    # ================= FINALIZE =================
    c.showPage()
    c.save()

    with open(tmp.name, "rb") as f:
        pdf_bytes = f.read()

    os.unlink(tmp.name)
    return pdf_bytes


# ================== FETCH ORDER (CUSTOMER) ==================
@app.get("/order/{order_id}")
async def get_order(order_id: str):
    try:
        oid = uuid.UUID(order_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid order ID")

    async with db_pool.acquire() as c:
        order = await c.fetchrow(
            "SELECT * FROM orders WHERE id=$1",
            oid
        )

        if not order:
            raise HTTPException(status_code=404, detail="Order not found")

        items = await c.fetch(
            """
            SELECT product_name, price, quantity
            FROM order_items
            WHERE order_id=$1
            """,
            oid
        )

    return {
        "order": dict(order),
        "items": [dict(i) for i in items]
    }


# ================= ADMIN ROUTES =================
@app.get("/admin/orders")
async def get_orders(admin=Depends(admin_required)):
    async with db_pool.acquire() as c:
        return await c.fetch("SELECT * FROM orders ORDER BY created_at DESC")

@app.get("/admin/orders/{order_id}/label")
async def get_label(order_id: str, admin=Depends(admin_required)):
    async with db_pool.acquire() as c:
        label = await c.fetchrow(
            "SELECT label_pdf FROM shipping_labels WHERE order_id=$1",
            uuid.UUID(order_id)
        )
        if not label:
            raise HTTPException(404, "Label not found")

    return Response(label["label_pdf"], media_type="application/pdf")

@app.post("/admin/labels/batch")
async def batch_labels(order_ids: List[str], admin=Depends(admin_required)):
    merger = PdfMerger()

    async with db_pool.acquire() as c:
        for oid in order_ids:
            row = await c.fetchrow(
                "SELECT label_pdf FROM shipping_labels WHERE order_id=$1",
                uuid.UUID(oid)
            )
            if row:
                merger.append(io.BytesIO(row["label_pdf"]))

    out = io.BytesIO()
    merger.write(out)
    merger.close()
    out.seek(0)

    return Response(out.read(), media_type="application/pdf")
