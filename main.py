from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, conint, confloat, EmailStr
import uuid
import warnings
import tempfile
import os
from typing import List
from reportlab.pdfgen import canvas
from reportlab.graphics.barcode import code128
from reportlab.lib.pagesizes import landscape, A6
from reportlab.lib.units import mm
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail, Attachment, FileContent, FileName, FileType, Disposition
import base64
import stripe
from dotenv import load_dotenv

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

sg_client = SendGridAPIClient(SENDGRID_API_KEY)

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
    checkoutId: str | None = None
    client_email: EmailStr

# ----------------- Storage -----------------
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

        subtotal = sum(item.price * item.qty for item in items)
        tax_rate = get_tax_rate_by_state(customer.state)
        tax = round(subtotal * tax_rate, 2)
        shipping = 5.99 if subtotal <= 50 else 0.0

        if tax > 0:
            line_items.append({
                "price_data": {
                    "currency": "gbp",
                    "product_data": {"name": "Sales Tax"},
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
        print("[STRIPE ERROR]", e.user_message or str(e))
        raise HTTPException(status_code=400, detail=e.user_message or str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ----------------- Email Utility -----------------
def send_email(to_email: str, subject: str, html_content: str, attachments: list[str] = None):
    message = Mail(
        from_email=FROM_EMAIL,
        to_emails=to_email,
        subject=subject,
        html_content=html_content,
    )

    if attachments:
        for filepath in attachments:
            try:
                with open(filepath, "rb") as f:
                    encoded = base64.b64encode(f.read()).decode()
                    message.attachment = Attachment(
                        FileContent(encoded),
                        FileName(os.path.basename(filepath)),
                        FileType("application/pdf"),
                        Disposition("attachment"),
                    )
            except Exception as e:
                print(f"[WARN] Could not attach file {filepath}: {e}")

    try:
        response = sg_client.send(message)
        print(f"[OK] Email sent to {to_email}, Status: {response.status_code}")
        return True
    except Exception as e:
        print(f"[ERROR] Email failed to {to_email}: {e}")
        return False

# ----------------- Tax Helper -----------------
def get_tax_rate_by_state(state: str) -> float:
    tax_rates = {
        "California": 0.075,
        "New York": 0.04,
        "Texas": 0.045,
        "Florida": 0.06,
        "Illinois": 0.0625,
        "Nevada": 0.0685,
        "Washington": 0.065,
    }
    return tax_rates.get(state, 0.07)

# ----------------- Label Generator -----------------
def generate_local_label(order: dict, customer: dict, order_id: str) -> str:
    try:
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
        c = canvas.Canvas(tmp.name, pagesize=landscape(A6))
        width, height = landscape(A6)

        # Margins
        top_margin = 8 * mm
        side_margin = 8 * mm
        bottom_margin = 12 * mm  # extra space for barcode

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

        # TO section
        c.setFont("Helvetica-Bold", 11)
        y_to_start = y_top - logo_h - 20 * mm  # shifted slightly upward
        c.drawString(side_margin, y_to_start, "TO:")

        c.setFont("Helvetica-Bold", 14)
        line_gap = 6 * mm
        to_lines = [
            customer.get("fullName", ""),
            customer.get("address", ""),
            f"{customer.get('city', '')}, {customer.get('state', '')} {customer.get('zip', '')}",
            customer.get("country", "GB"),
        ]

        # Compute vertical space for TO block
        total_text_height = len(to_lines) * line_gap
        start_y = y_to_start - 4 * mm  # small gap after "TO:"
        for i, text in enumerate(to_lines):
            c.drawCentredString(width / 2, start_y - (i * line_gap), text)

        # Barcode at bottom with proper margin
        barcode_height = 20 * mm
        barcode = code128.Code128(order_id, barHeight=barcode_height, barWidth=0.5 * mm)
        barcode_x = (width - barcode.width) / 2
        barcode_y = bottom_margin
        barcode.drawOn(c, barcode_x, barcode_y)

        # Save PDF
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

        ORDERS_DB[checkout_id] = {
            "id": checkout_id,
            "customer": request.customer.dict(),
            "cart": [i.dict() for i in request.cart],
            "subtotal": subtotal,
            "tax": tax,
            "shipping": shipping,
            "total": total,
        }

        return {"url": checkout_url}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

# ----------------- Order Fetch -----------------
@app.get("/order/{checkout_id}")
async def get_order(checkout_id: str):
    order = ORDERS_DB.get(checkout_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    return order

# ----------------- Payment Success -----------------
@app.post("/payment-success")
async def payment_success(req: SuccessRequest):
    if not req.checkoutId or req.checkoutId not in ORDERS_DB:
        raise HTTPException(status_code=404, detail="Order not found")

    order = ORDERS_DB[req.checkoutId]
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

    send_email(req.client_email, "Your Order Confirmation", html)
    label = generate_local_label(order, req.customer.dict(), req.checkoutId)
    send_email(ADMIN_EMAIL, f"New Order ({req.checkoutId})", html, [label] if label else None)

    return {"status": "success", "message": "Order confirmed and emails sent"}
