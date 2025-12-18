from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, conint, confloat, EmailStr
import uuid, os, logging
from typing import List

import stripe
from dotenv import load_dotenv
import smtplib
from email.message import EmailMessage
import asyncpg

# ================== LOGGING ==================
class RequestIdFilter(logging.Filter):
    def filter(self, record):
        record.request_id = getattr(record, "request_id", "-")
        return True

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(request_id)s | %(message)s",
)

logger = logging.getLogger("luminous-api")
logger.addFilter(RequestIdFilter())

# ================== ENV ==================
load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
FRONTEND_URL = os.getenv("FRONTEND_URL")
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
FROM_EMAIL = os.getenv("FROM_EMAIL")

stripe.api_key = os.getenv("STRIPE_SECRET_KEY_TEST")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")

# ================== APP ==================
app = FastAPI(title="Luminous Candles API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Request-ID"] = str(uuid.uuid4())
    return response

# ================== DB ==================
db_pool: asyncpg.Pool | None = None

@app.on_event("startup")
async def startup():
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL)

    async with db_pool.acquire() as c:
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
            subtotal NUMERIC,
            tax NUMERIC,
            shipping NUMERIC,
            total NUMERIC,
            stripe_session_id TEXT,
            email_sent BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT NOW()
        );
        """)

        await c.execute("""
        CREATE TABLE IF NOT EXISTS order_items (
            id SERIAL PRIMARY KEY,
            order_id UUID REFERENCES orders(id),
            product_name TEXT,
            price NUMERIC,
            quantity INT
        );
        """)

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


# ================== TAX HELPER (YOUR ORIGINAL LOGIC) ==================
def get_tax_rate_by_state(state: str) -> float:
    tax_rates = {
        "Alabama": 0.04, "Alaska": 0.00, "Arizona": 0.056, "Arkansas": 0.065,
        "California": 0.0725, "Colorado": 0.029, "Connecticut": 0.0635, "Delaware": 0.00,
        "Florida": 0.06, "Georgia": 0.04, "Hawaii": 0.04, "Idaho": 0.06,
        "Illinois": 0.0625, "Indiana": 0.07, "Iowa": 0.06, "Kansas": 0.065,
        "Kentucky": 0.06, "Louisiana": 0.0445, "Maine": 0.055, "Maryland": 0.06,
        "Massachusetts": 0.0625, "Michigan": 0.06, "Minnesota": 0.06875,
        "Mississippi": 0.07, "Missouri": 0.04225, "Montana": 0.00,
        "Nebraska": 0.055, "Nevada": 0.0685, "New Hampshire": 0.00,
        "New Jersey": 0.06625, "New Mexico": 0.05125, "New York": 0.04
    }
    return tax_rates.get(state, 0.07)

# ================== EMAIL ==================
def send_email(to_email: str, total: float):
    msg = EmailMessage()
    msg["From"] = FROM_EMAIL
    msg["To"] = to_email
    msg["Subject"] = "Order Confirmed"
    msg.set_content(
        f"Thank you for your order.\n"
        f"Total paid: £{total}"
    )

    with smtplib.SMTP("smtp.gmail.com", 587) as s:
        s.starttls()
        s.login(SMTP_USER, SMTP_PASSWORD)
        s.send_message(msg)

# ================== CHECKOUT ==================
@app.post("/create-checkout-session")
async def create_checkout(req: CheckoutRequest):
    subtotal = sum(i.price * i.qty for i in req.cart)
    tax = round(subtotal * get_tax_rate_by_state(req.customer.state), 2)
    shipping = 5.99 if subtotal <= 50 else 0.0
    total = round(subtotal + tax + shipping, 2)

    order_id = uuid.uuid4()

    # ---------- STRIPE LINE ITEMS ----------
    line_items = []

    # Products
    for i in req.cart:
        line_items.append({
            "price_data": {
                "currency": "usd",
                "product_data": {"name": i.name},
                "unit_amount": int(i.price * 100),
            },
            "quantity": i.qty,
        })

    # Tax (separate)
    if tax > 0:
        line_items.append({
            "price_data": {
                "currency": "usd",
                "product_data": {"name": "Sales Tax"},
                "unit_amount": int(tax * 100),
            },
            "quantity": 1,
        })

    # Shipping (separate)
    if shipping > 0:
        line_items.append({
            "price_data": {
                "currency": "usd",
                "product_data": {"name": "Shipping"},
                "unit_amount": int(shipping * 100),
            },
            "quantity": 1,
        })

    # ---------- STRIPE SESSION ----------
    session = stripe.checkout.Session.create(
        mode="payment",
        line_items=line_items,
        success_url=f"{FRONTEND_URL}/success.html",
        cancel_url=f"{FRONTEND_URL}/cancel.html",
        customer_email=req.customer.email,
        metadata={"order_id": str(order_id)},
    )

    async with db_pool.acquire() as c:
        await c.execute("""
        INSERT INTO orders (
            id, status, customer_name, email, phone,
            address, city, state, zip, country,
            subtotal, tax, shipping, total, stripe_session_id
        ) VALUES (
            $1,'PENDING',$2,$3,$4,
            $5,$6,$7,$8,$9,
            $10,$11,$12,$13,$14
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
        subtotal,
        tax,
        shipping,
        total,
        session.id
        )

        for i in req.cart:
            await c.execute("""
            INSERT INTO order_items (order_id, product_name, price, quantity)
            VALUES ($1,$2,$3,$4)
            """, order_id, i.name, i.price, i.qty)

    return {"url": session.url, "orderId": str(order_id)}

# ================== STRIPE WEBHOOK ==================
@app.post("/stripe/webhook")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, STRIPE_WEBHOOK_SECRET
        )
    except Exception:
        raise HTTPException(400, "Invalid webhook")

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        order_id = session["metadata"]["order_id"]

        async with db_pool.acquire() as c:
            order = await c.fetchrow(
                "SELECT status, email_sent, email, total FROM orders WHERE id=$1",
                uuid.UUID(order_id)
            )

            if order and order["status"] != "PAID":
                await c.execute("""
                UPDATE orders
                SET status='PAID', email_sent=TRUE
                WHERE id=$1
                """, uuid.UUID(order_id))

                if not order["email_sent"]:
                    send_email(order["email"], order["total"])

    return {"status": "ok"}

# ================== FETCH ORDER ==================
@app.get("/order/{order_id}")
async def get_order(order_id: str):
    async with db_pool.acquire() as c:
        order = await c.fetchrow(
            "SELECT * FROM orders WHERE id=$1",
            uuid.UUID(order_id)
        )
        if not order:
            raise HTTPException(404, "Order not found")

        items = await c.fetch(
            "SELECT product_name, price, quantity FROM order_items WHERE order_id=$1",
            uuid.UUID(order_id)
        )

    return {"order": dict(order), "items": [dict(i) for i in items]}
