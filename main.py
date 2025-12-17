from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, conint, confloat, EmailStr
import uuid, os
from typing import List

import stripe
from dotenv import load_dotenv
import smtplib
from email.message import EmailMessage
import asyncpg

# ================== ENV ==================
load_dotenv()

def normalize_frontend_url(url: str | None) -> str:
    if not url or not url.strip():
        raise RuntimeError("FRONTEND_URL is not set")

    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    return url.rstrip("/")

FRONTEND_URL = normalize_frontend_url(os.getenv("FRONTEND_URL"))
DATABASE_URL = os.getenv("DATABASE_URL")
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
FROM_EMAIL = os.getenv("FROM_EMAIL")

stripe.api_key = os.getenv("STRIPE_SECRET_KEY_TEST")

# ================== APP ==================
app = FastAPI(title="Luminous Candles API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

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

class SuccessRequest(BaseModel):
    checkoutId: str

# ================== TEMP STORE ==================
ORDERS_DB: dict[str, dict] = {}


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
def send_email(to_email: str, html: str):
    msg = EmailMessage()
    msg["From"] = FROM_EMAIL
    msg["To"] = to_email
    msg["Subject"] = "Order Confirmation"
    msg.add_alternative(html, subtype="html")

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

    checkout_id = str(uuid.uuid4())

    line_items = [{
        "price_data": {
            "currency": "gbp",
            "product_data": {"name": i.name},
            "unit_amount": int(i.price * 100),
        },
        "quantity": i.qty,
    } for i in req.cart]

    if tax > 0:
        line_items.append({
            "price_data": {
                "currency": "gbp",
                "product_data": {"name": "Tax"},
                "unit_amount": int(tax * 100),
            },
            "quantity": 1,
        })

    if shipping > 0:
        line_items.append({
            "price_data": {
                "currency": "gbp",
                "product_data": {"name": "Shipping"},
                "unit_amount": int(shipping * 100),
            },
            "quantity": 1,
        })

    session = stripe.checkout.Session.create(
        mode="payment",
        payment_method_types=["card"],
        line_items=line_items,
        success_url=f"{FRONTEND_URL}/success.html?checkoutId={checkout_id}",
        cancel_url=f"{FRONTEND_URL}/cancel.html",
        customer_email=req.customer.email,
    )

    ORDERS_DB[checkout_id] = {
        "customer": req.customer.dict(),
        "cart": [i.dict() for i in req.cart],
        "subtotal": subtotal,
        "tax": tax,
        "shipping": shipping,
        "total": total,
    }

    return {"url": session.url}

# ================== PAYMENT SUCCESS ==================
@app.post("/payment-success")
async def payment_success(req: SuccessRequest):
    order = ORDERS_DB.pop(req.checkoutId, None)
    if not order:
        raise HTTPException(404, "Order not found")

    async with db_pool.acquire() as c:
        await c.execute("""
        INSERT INTO orders (
            id, customer_name, email, phone, address, city, state, zip, country,
            subtotal, tax, shipping, total
        ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
        """,
        uuid.UUID(req.checkoutId),
        order["customer"]["fullName"],
        order["customer"]["email"],
        order["customer"]["phone"],
        order["customer"]["address"],
        order["customer"]["city"],
        order["customer"]["state"],
        order["customer"]["zip"],
        order["customer"]["country"],
        order["subtotal"],
        order["tax"],
        order["shipping"],
        order["total"],
        )

        for i in order["cart"]:
            await c.execute("""
            INSERT INTO order_items (order_id, product_name, price, quantity)
            VALUES ($1,$2,$3,$4)
            """, uuid.UUID(req.checkoutId), i["name"], i["price"], i["qty"])

    send_email(order["customer"]["email"], f"""
        <h2>Order Confirmed</h2>
        <p>Subtotal: £{order['subtotal']}</p>
        <p>Tax: £{order['tax']}</p>
        <p>Shipping: £{order['shipping']}</p>
        <h3>Total: £{order['total']}</h3>
    """)

    return {"status": "success"}

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
