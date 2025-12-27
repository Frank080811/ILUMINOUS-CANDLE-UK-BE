from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, conint, confloat, EmailStr
import uuid, os, logging
from typing import List
from fastapi.responses import Response

import stripe
from dotenv import load_dotenv
import smtplib
from email.message import EmailMessage
import asyncpg

import tempfile, os
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A6, landscape
from reportlab.lib.units import mm
from reportlab.graphics.barcode import qr
from reportlab.graphics import renderPDF
from reportlab.graphics.shapes import Drawing

from PyPDF2 import PdfMerger
import io, tempfile

from passlib.context import CryptContext
from jose import jwt
from datetime import datetime, timedelta
from fastapi.security import HTTPBearer

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
security = HTTPBearer()
# ================== ENV ==================
load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
FRONTEND_URL = os.getenv("FRONTEND_URL")
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
FROM_EMAIL = os.getenv("FROM_EMAIL")

stripe.api_key = os.getenv("STRIPE_SECRET_KEY_TEST")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")

# ================== ADMIN PORTAL ==================
SECRET_KEY = os.getenv("ADMIN_JWT_SECRET")
ALGORITHM = "HS256"
pwd_context = CryptContext(schemes=["bcrypt"])

ADMIN_USER = {
    "email": os.getenv("ADMIN_EMAIL"),
    "password_hash": pwd_context.hash(os.getenv("ADMIN_PASSWORD"))
}


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

class CheckoutTotals(BaseModel):
    subtotal: confloat(ge=0)
    tax: confloat(ge=0)
    shipping: confloat(ge=0)
    total: confloat(ge=0)

class CheckoutRequest(BaseModel):
    customer: CustomerInfo
    cart: List[Item]
    totals: CheckoutTotals

def send_order_confirmation_email(
    to_email: str,
    order: dict,
    items: list[dict]
):
    msg = EmailMessage()
    msg["From"] = FROM_EMAIL
    msg["To"] = to_email
    msg["Subject"] = f"Order Confirmation – {order['id']}"

    item_lines = []
    for i in items:
        line_total = float(i["price"]) * i["quantity"]
        item_lines.append(
            f"- {i['product_name']} × {i['quantity']}  (£{line_total:.2f})"
        )

    body = f"""
Dear {order['customer_name']},

Thank you for shopping with Luminous Candles.

Your order has been successfully confirmed. Below are your order details:

Order ID:
{order['id']}

Items Ordered:
{chr(10).join(item_lines)}

Order Summary:
Subtotal: £{float(order['subtotal']):.2f}
Tax: £{float(order['tax']):.2f}
Shipping: £{float(order['shipping']):.2f}
-----------------------------
Total Paid: £{float(order['total']):.2f}

Shipping Address:
{order['customer_name']}
{order['address']}
{order['city']}, {order['state']} {order['zip']}
{order['country']}

Your order is now being prepared for shipment.

If you have any questions, please contact us at support@luminouscandles.co.uk.

Warm regards,
Luminous Candles Team
"""

    msg.set_content(body)

    with smtplib.SMTP("smtp.gmail.com", 587) as s:
        s.starttls()
        s.login(SMTP_USER, SMTP_PASSWORD)
        s.send_message(msg)

# ================== ADMIN LOGIN ==================
class AdminLogin(BaseModel):
    email: str
    password: str

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


# ================== CHECKOUT ==================
@app.post("/create-checkout-session")
async def create_checkout(req: CheckoutRequest):

    # ---------------- FRONTEND-CALCULATED VALUES ----------------
    subtotal = round(req.totals.subtotal, 2)
    tax = round(req.totals.tax, 2)
    shipping = round(req.totals.shipping, 2)
    total = round(req.totals.total, 2)

    # ---------------- OPTIONAL SAFETY CHECK ----------------
    computed_subtotal = sum(i.price * i.qty for i in req.cart)

    if round(computed_subtotal, 2) != subtotal:
        raise HTTPException(
            status_code=400,
            detail="Subtotal mismatch between cart and totals"
        )

    if round(subtotal + tax + shipping, 2) != total:
        raise HTTPException(
            status_code=400,
            detail="Total mismatch"
        )

    order_id = uuid.uuid4()

    # ---------------- STRIPE LINE ITEMS ----------------
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

    # Tax (frontend-calculated)
    if tax > 0:
        line_items.append({
            "price_data": {
                "currency": "usd",
                "product_data": {"name": "Sales Tax"},
                "unit_amount": int(tax * 100),
            },
            "quantity": 1,
        })

    # Shipping (frontend-calculated)
    if shipping > 0:
        line_items.append({
            "price_data": {
                "currency": "usd",
                "product_data": {"name": "Shipping"},
                "unit_amount": int(shipping * 100),
            },
            "quantity": 1,
        })

    # ---------------- STRIPE SESSION ----------------
    session = stripe.checkout.Session.create(
        mode="payment",
        line_items=line_items,
        success_url=f"{FRONTEND_URL}/success.html?order_id={order_id}",
        cancel_url=f"{FRONTEND_URL}/cancel.html",
        customer_email=req.customer.email,
        metadata={"order_id": str(order_id)},
    )

    # ---------------- Protect Admin Routes ----------------
def admin_required(token=Depends(security)):
    payload = jwt.decode(token.credentials, SECRET_KEY, algorithms=[ALGORITHM])
    return payload

    # ---------------- DATABASE ----------------
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

    return {
        "url": session.url,
        "orderId": str(order_id)
    }

# ================== STRIPE WEBHOOK ==================
@app.post("/stripe/webhook")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, STRIPE_WEBHOOK_SECRET
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    if event["type"] != "checkout.session.completed":
        return {"status": "ignored"}

    session = event["data"]["object"]

    if "order_id" not in session.get("metadata", {}):
        raise HTTPException(400, "Missing order_id metadata")

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

        # 1️⃣ Mark as PAID
        await c.execute(
            "UPDATE orders SET status='PAID' WHERE id=$1",
            order_id
        )

        # 2️⃣ Generate label (idempotent)
        label_exists = await c.fetchrow(
            "SELECT 1 FROM shipping_labels WHERE order_id=$1",
            order_id
        )

        if not label_exists:
            label_pdf = generate_local_label(
                dict(order),
                {
                    "fullName": order["customer_name"],
                    "address": order["address"],
                    "city": order["city"],
                    "state": order["state"],
                    "zip": order["zip"],
                    "country": order["country"],
                },
                str(order_id),
            )

            await c.execute(
                """
                INSERT INTO shipping_labels (id, order_id, label_pdf)
                VALUES ($1, $2, $3)
                """,
                uuid.uuid4(),
                order_id,
                label_pdf,
            )

        # 3️⃣ Fetch items for email
        items = await c.fetch(
            """
            SELECT product_name, price, quantity
            FROM order_items
            WHERE order_id=$1
            """,
            order_id
        )

        # 4️⃣ Send professional confirmation email (once)
        if not order["email_sent"]:
            send_order_confirmation_email(
                order["email"],
                dict(order),
                [dict(i) for i in items],
            )

            await c.execute(
                "UPDATE orders SET email_sent=TRUE WHERE id=$1",
                order_id
            )

    return {"status": "ok"}

@app.get("/admin/orders/{order_id}/label")
async def download_label(order_id: str):
    async with db_pool.acquire() as c:
        label = await c.fetchrow(
            """
            SELECT label_pdf
            FROM shipping_labels
            WHERE order_id=$1
            """,
            uuid.UUID(order_id)
        )

        if not label:
            raise HTTPException(404, "Label not found")

    return Response(
        content=label["label_pdf"],
        media_type="application/pdf",
        headers={
            "Content-Disposition": f"inline; filename=label-{order_id}.pdf"
        },
    )
# ----------------- GENERATE LABEL -----------------
def generate_local_label(order: dict, customer: dict, order_id: str) -> bytes:
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
    c = canvas.Canvas(tmp.name, pagesize=landscape(A6))
    width, height = landscape(A6)

    # ----------------- MARGINS -----------------
    top_margin = 8 * mm
    side_margin = 8 * mm
    bottom_margin = 10 * mm

    # ----------------- LOGO -----------------
    logo_path = "images/LOGON.jpg"
    logo_w, logo_h = 22 * mm, 22 * mm
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

    # ----------------- FROM SECTION -----------------
    from_x = side_margin + logo_w + 6 * mm
    from_y = y_top - 5 * mm

    c.setFont("Helvetica-Bold", 9)
    c.drawString(from_x, from_y, "FROM:")

    c.setFont("Helvetica", 8)
    sender_lines = [
        "Luminous Candles Ltd T/A Nelux Candles",
        "71–75 Shelton Street",
        "Covent Garden, London WC2H 9JQ",
        "United Kingdom",
    ]

    for i, line in enumerate(sender_lines):
        c.drawString(from_x, from_y - ((i + 1) * 4.5 * mm), line)

    # ----------------- TO SECTION -----------------
    to_block_top = y_top - logo_h - 18 * mm
    c.setFont("Helvetica-Bold", 10)
    c.drawString(side_margin, to_block_top, "TO:")

    c.setFont("Helvetica-Bold", 13)
    line_gap = 6 * mm

    to_lines = [
        customer.get("fullName", ""),
        customer.get("address", ""),
        f"{customer.get('city', '')}, {customer.get('state', '')} {customer.get('zip', '')}",
        customer.get("country", "GB"),
    ]

    start_y = to_block_top - 6 * mm
    for i, text in enumerate(to_lines):
        c.drawCentredString(width / 2, start_y - (i * line_gap), text)

 
# ----------------- QR CODE (BOTTOM-RIGHT) -----------------
    qr_size = 26 * mm
    qr_x = width - side_margin - qr_size
    qr_y = bottom_margin

    qr_widget = qr.QrCodeWidget(order_id)
    bounds = qr_widget.getBounds()

    qr_width = bounds[2] - bounds[0]
    qr_height = bounds[3] - bounds[1]

    drawing = Drawing(
        qr_size,
        qr_size,
        transform=[
            qr_size / qr_width, 0, 0,
            qr_size / qr_height, 0, 0
     ]
    )

    drawing.add(qr_widget)

    renderPDF.draw(drawing, c, qr_x, qr_y)

    c.setFont("Helvetica", 7)
    c.drawCentredString(qr_x + qr_size / 2, qr_y - 4 * mm, "Order ID")


    # ----------------- FINALIZE -----------------
    c.showPage()
    c.save()

    with open(tmp.name, "rb") as f:
        pdf_bytes = f.read()

    os.unlink(tmp.name)
    return pdf_bytes


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

# ================== REVENUE ANALYTICS ==================
@app.get("/admin/analytics")
async def analytics(admin=Depends(admin_required)):
    async with db_pool.acquire() as c:
        revenue = await c.fetchval(
            "SELECT SUM(total) FROM orders WHERE status='PAID'"
        )
        orders = await c.fetchval("SELECT COUNT(*) FROM orders")
        today = await c.fetchval("""
            SELECT SUM(total)
            FROM orders
            WHERE DATE(created_at)=CURRENT_DATE
            AND status='PAID'
        """)

    return {
        "totalRevenue": float(revenue or 0),
        "totalOrders": orders,
        "todayRevenue": float(today or 0)
    }
# ================== ORDER SEARCH & FILTER ==================
@app.get("/admin/orders")
async def orders(
    q: str = "",
    status: str = "",
    admin=Depends(admin_required)
):
    query = """
    SELECT * FROM orders
    WHERE ($1='' OR email ILIKE '%'||$1||'%')
      AND ($2='' OR status=$2)
    ORDER BY created_at DESC
    """
    async with db_pool.acquire() as c:
        return await c.fetch(query, q, status)

# ================== RESEND CONFIRMATION EMAIL ==================
@app.post("/admin/orders/{order_id}/resend-email")
async def resend_email(order_id: str, admin=Depends(admin_required)):
    async with db_pool.acquire() as c:
        order = await c.fetchrow("SELECT * FROM orders WHERE id=$1", uuid.UUID(order_id))
        items = await c.fetch(
            "SELECT product_name, price, quantity FROM order_items WHERE order_id=$1",
            uuid.UUID(order_id)
        )

    send_order_confirmation_email(
        order["email"],
        dict(order),
        [dict(i) for i in items]
    )

    return {"status": "sent"}

# ================== INVOICE PDF VIEW ==================
@app.get("/admin/orders/{order_id}/invoice")
async def invoice(order_id: str, admin=Depends(admin_required)):
    pdf = generate_invoice_pdf(order_id)
    return Response(pdf, media_type="application/pdf")

# ================== SINGLE LABEL DOWNLOAD ==================
@app.get("/admin/orders/{order_id}/label")
async def download_label(order_id: str):
    async with db_pool.acquire() as c:
        label = await c.fetchrow(
            """
            SELECT label_pdf
            FROM shipping_labels
            WHERE order_id=$1
            """,
            uuid.UUID(order_id)
        )

        if not label:
            raise HTTPException(404, "Label not found")

    return Response(
        content=label["label_pdf"],
        media_type="application/pdf",
        headers={
            "Content-Disposition": f"inline; filename=label-{order_id}.pdf"
        },
    )

@app.get("/admin/orders")
async def get_orders(admin=Depends(admin_required)):

    async with db_pool.acquire() as c:
        orders = await c.fetch("""
            SELECT *
            FROM orders
            ORDER BY created_at DESC
        """)

        result = []
        for o in orders:
            items = await c.fetch("""
                SELECT product_name, quantity
                FROM order_items
                WHERE order_id=$1
            """, o["id"])

            order_dict = dict(o)
            order_dict["items"] = [dict(i) for i in items]
            result.append(order_dict)

    return result



@app.post("/admin/labels/batch")
async def batch_print_labels(order_ids: list[str]):
    merger = PdfMerger()

    async with db_pool.acquire() as c:
        for oid in order_ids:
            row = await c.fetchrow(
                "SELECT label_pdf FROM shipping_labels WHERE order_id=$1",
                uuid.UUID(oid)
            )
            if row:
                merger.append(io.BytesIO(row["label_pdf"]))

    output = io.BytesIO()
    merger.write(output)
    merger.close()
    output.seek(0)

    return Response(
        content=output.read(),
        media_type="application/pdf",
        headers={
            "Content-Disposition": "inline; filename=batch-labels.pdf"
        },
    )
