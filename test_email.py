from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail
import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

message = Mail(
    from_email=os.getenv("FROM_EMAIL"),
    to_emails="bentjun25@gmail.com",
    subject="Test Email from FastAPI",
    html_content="<strong>This is a test email</strong>",
)

try:
    sg = SendGridAPIClient(os.getenv("SENDGRID_API_KEY"))
    response = sg.send(message)
    print(f"✅ Status Code: {response.status_code}")
    print(f"Body: {response.body}")
except Exception as e:
    print(f"❌ Error sending email: {e}")
