import imaplib
import email
import re
import time
import logging
import os
from email.header import decode_header
from dotenv import load_dotenv
import requests

load_dotenv()

# ── Config ──────────────────────────────────────────────────────────────────
GMAIL_USER         = os.getenv("GMAIL_USER")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")
CHECK_INTERVAL     = int(os.getenv("CHECK_INTERVAL_SECONDS", "60"))
PAYMENT_SENDER     = os.getenv("PAYMENT_SENDER", "payments@anywherecommerce.com")

# Exact deposit amounts (in USD, as floats)
DEPOSIT_AMOUNTS = {200.00, 500.00}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bot.log"),
    ],
)
log = logging.getLogger(__name__)


# ── Telegram helpers ─────────────────────────────────────────────────────────
def send_telegram(text: str) -> bool:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
        log.info("Telegram message sent successfully.")
        return True
    except Exception as e:
        log.error(f"Failed to send Telegram message: {e}")
        return False


# ── Email parsing ────────────────────────────────────────────────────────────
def extract_field(pattern: str, text: str, default: str = "N/A") -> str:
    """Extract a single field from plain email text using a regex pattern."""
    match = re.search(pattern, text, re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return default


def parse_payment_email(body: str) -> dict | None:
    """
    Parse the AnywhereCommerce payment email body.

    Payment type is determined by:
      - REFUND   → DESCRIPTION contains 'refund'  OR  TOTAL AMOUNT is negative
      - DEPOSIT  → TOTAL AMOUNT is exactly $200.00 or $500.00
      - PAYMENT  → everything else
    """
    # Normalise tabs/multiple spaces to single space
    body_clean = re.sub(r"[ \t]+", " ", body)

    customer_name = extract_field(r"CUSTOMER\s*NAME\s*[:\-]?\s*([^\n\r]+)", body_clean)
    description   = extract_field(r"DESCRIPTION\s*[:\-]?\s*([^\n\r]+)", body_clean)

    # Total amount – may have a leading minus for refunds, e.g. "USD -150.00" or "-USD 150.00"
    raw_amount = extract_field(
        r"TOTAL\s*AMOUNT\s*[:\-]?\s*([-]?\s*USD\s*[-]?\s*[\d,\.]+|USD\s*[-]?\s*[\d,\.]+)",
        body_clean,
    )

    if customer_name == "N/A" and raw_amount == "N/A":
        return None  # Does not look like a payment email

    # ── Parse numeric amount ────────────────────────────────────────────────
    # Strip "USD", spaces, commas → keep minus and digits/dot
    numeric_str = re.sub(r"[^\d\.\-]", "", raw_amount.replace("USD", ""))
    try:
        amount_value = float(numeric_str)
    except ValueError:
        amount_value = 0.0

    # Format display amount (always positive in the message body)
    amount_display = f"${abs(amount_value):,.2f}"

    # ── Opportunity / Job number from DESCRIPTION ───────────────────────────
    # Looks for patterns like "Opportunity 12345", "Opp# 98765", "Job 555"
    opp_match = re.search(
        r"(?:opportunity|opp|job)\s*[#:\-]?\s*(\w[\w\-]*)",
        description, re.IGNORECASE
    )
    job_number = opp_match.group(1) if opp_match else description  # fallback: whole description

    # ── Determine payment type ──────────────────────────────────────────────
    is_refund  = amount_value < 0 or "refund" in description.lower()
    is_deposit = (not is_refund) and (round(abs(amount_value), 2) in DEPOSIT_AMOUNTS)
    # everything else → regular payment

    return {
        "customer_name":  customer_name,
        "amount_display": amount_display,
        "job_number":     job_number,
        "is_refund":      is_refund,
        "is_deposit":     is_deposit,
    }


def format_message(data: dict) -> str:
    if data["is_refund"]:
        header = "❌ <b>Возврат</b>"
    elif data["is_deposit"]:
        header = "💸 <b>Получен: Депозит</b>"
    else:
        header = "💸 <b>Получен: Платеж</b>"

    return (
        f"{header}\n"
        f"<b>- Сумма:</b> {data['amount_display']}\n"
        f"<b>- Клиент:</b> {data['customer_name']}\n"
        f"<b>- Работа:</b> {data['job_number']}"
    )


# ── Gmail IMAP ───────────────────────────────────────────────────────────────
def get_email_body(msg) -> str:
    """Extract plain-text body from an email.Message object."""
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            cd = str(part.get("Content-Disposition", ""))
            if ct == "text/plain" and "attachment" not in cd:
                charset = part.get_content_charset() or "utf-8"
                try:
                    body += part.get_payload(decode=True).decode(charset, errors="replace")
                except Exception:
                    pass
    else:
        charset = msg.get_content_charset() or "utf-8"
        try:
            body = msg.get_payload(decode=True).decode(charset, errors="replace")
        except Exception:
            pass
    return body


def check_new_emails():
    """Connect to Gmail via IMAP, find unseen payment emails, process them."""
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        mail.select("inbox")

        # Search for UNSEEN emails from the payment sender
        search_criteria = f'(UNSEEN FROM "{PAYMENT_SENDER}")'
        status, data = mail.search(None, search_criteria)

        if status != "OK" or not data[0]:
            mail.logout()
            return

        email_ids = data[0].split()
        log.info(f"Found {len(email_ids)} new payment email(s).")

        for eid in email_ids:
            try:
                _, msg_data = mail.fetch(eid, "(RFC822)")
                raw = msg_data[0][1]
                msg = email.message_from_bytes(raw)
                body = get_email_body(msg)

                log.debug(f"Email body:\n{body}")

                parsed = parse_payment_email(body)
                if parsed is None:
                    log.warning(f"Email {eid.decode()} could not be parsed — skipping.")
                    # Still mark as read so we don't retry endlessly
                    mail.store(eid, "+FLAGS", "\\Seen")
                    continue

                message = format_message(parsed)
                sent = send_telegram(message)

                if sent:
                    # Mark as read only after successful delivery
                    mail.store(eid, "+FLAGS", "\\Seen")
                    log.info(f"Processed email {eid.decode()} — {'Deposit' if parsed['is_deposit'] else 'Payment'}.")
                else:
                    log.error(f"Telegram delivery failed for email {eid.decode()}; will retry next cycle.")

            except Exception as e:
                log.error(f"Error processing email {eid}: {e}")

        mail.logout()

    except imaplib.IMAP4.error as e:
        log.error(f"IMAP error: {e}")
    except Exception as e:
        log.error(f"Unexpected error: {e}")


# ── Main loop ────────────────────────────────────────────────────────────────
def main():
    log.info("Gmail → Telegram payment bot started.")
    log.info(f"Monitoring: {PAYMENT_SENDER}")
    log.info(f"Check interval: {CHECK_INTERVAL}s")

    # Quick connectivity check
    test_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getMe"
    try:
        r = requests.get(test_url, timeout=10)
        r.raise_for_status()
        bot_info = r.json().get("result", {})
        log.info(f"Connected to Telegram bot: @{bot_info.get('username', '?')}")
    except Exception as e:
        log.error(f"Telegram bot token check failed: {e}")
        return

    while True:
        log.info("Checking Gmail for new payment emails…")
        check_new_emails()
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
