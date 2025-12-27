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
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

ADMIN_EMAIL = os.getenv("ADMIN_EMAIL")
ADMIN_PASSWORD_HASH = os.getenv("ADMIN_PASSWORD_HASH")

if not ADMIN_EMAIL or not ADMIN_PASSWORD_HASH:
    raise RuntimeError(
        "ADMIN_EMAIL or ADMIN_PASSWORD_HASH is missing in environment variables"
    )

ADMIN_USER = {
    "email": ADMIN_EMAIL,
    "password_hash": ADMIN_PASSWORD_HASH
}


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
@app.post("/admin/login")
async def admin_login(data: AdminLogin):
    if data.email != ADMIN_USER["email"]:
        raise HTTPException(401, "Invalid credentials")

    if not pwd_context.verify(data.password, ADMIN_USER["password_hash"]):
        raise HTTPException(401, "Invalid credentials")

    token = jwt.encode(
        {"sub": data.email, "exp": datetime.utcnow() + timedelta(hours=8)},
        SECRET_KEY,
        algorithm=ALGORITHM
    )

    return {"access_token": token}

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
        raise HTTPException(400, "Invalid signature")

    if event["type"] != "checkout.session.completed":
        return {"status": "ignored"}

    session = event["data"]["object"]
    order_id = uuid.UUID(session["metadata"]["order_id"])

    async with db_pool.acquire() as c:
        await c.execute(
            "UPDATE orders SET status='PAID' WHERE id=$1",
            order_id
        )

    return {"status": "ok"}

# ================= LABEL GENERATION =================
def generate_shipping_label(order: dict) -> bytes:
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
    c = canvas.Canvas(tmp.name, pagesize=landscape(A6))
    width, height = landscape(A6)

    c.setFont("Helvetica-Bold", 14)
    c.drawCentredString(width/2, height-30, "Shipping Label")

    qr_widget = qr.QrCodeWidget(str(order["id"]))
    bounds = qr_widget.getBounds()

    d = Drawing(
        100, 100,
        transform=[
            100 / (bounds[2] - bounds[0]), 0, 0,
            100 / (bounds[3] - bounds[1]), 0, 0
        ]
    )
    d.add(qr_widget)
    renderPDF.draw(d, c, width-120, 30)

    c.showPage()
    c.save()

    with open(tmp.name, "rb") as f:
        pdf = f.read()

    os.unlink(tmp.name)
    return pdf

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
