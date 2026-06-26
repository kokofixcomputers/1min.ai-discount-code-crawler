import imaplib
import email
import re
import time
import datetime
import pytz
import requests
from email.header import decode_header
from email.message import Message


IMAP_HOST = "IMAP HOST"
IMAP_USER = "IMAP USERNAME"
IMAP_PASS = "IMAP PASSWORD"

MAILBOXES = ["INBOX", "Junk"]

DISCORD_WEBHOOK_URL = "https://discord.com..."  # <-- put your webhook URL here


GENERIC_CODE_REGEX = re.compile(
    r"Deals of the Week.*?([A-Z0-9]{6,12}).*?→",
    re.DOTALL
)

DISCOUNT_PERCENT_REGEX = re.compile(
    r"→\s*[A-Za-z ]*?(\d{1,3})\s*%",
    re.DOTALL
)

DISCOUNT_VALIDITY_REGEX = re.compile(
    r"%\s*OFF\s*on\s*all\s*payments\s*before\s*(.*?)\.",
    re.DOTALL
)


def decode_mime_words(s: str) -> str:
    if not s:
        return ""
    parts = decode_header(s)
    out = []
    for text, enc in parts:
        if isinstance(text, bytes):
            out.append(text.decode(enc or "utf-8", errors="replace"))
        else:
            out.append(text)
    return "".join(out)


def get_text_from_msg(msg: Message) -> str:
    parts = []
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype in ("text/plain", "text/html"):
                payload = part.get_payload(decode=True)
                if isinstance(payload, bytes):
                    parts.append(
                        payload.decode(
                            part.get_content_charset() or "utf-8",
                            errors="replace",
                        )
                    )
                elif payload is not None:
                    parts.append(str(payload))
    else:
        payload = msg.get_payload(decode=True)
        if isinstance(payload, bytes):
            parts.append(
                payload.decode(
                    msg.get_content_charset() or "utf-8",
                    errors="replace",
                )
            )
        elif payload is not None:
            parts.append(str(payload))
    return "\n".join(parts)


def parse_validity_to_epoch(validity_str: str) -> int | None:
    """
    Convert 'July 1, 6 AM PST' into a Unix epoch (int) assuming the current year.
    Returns None if parsing fails.
    """
    if not validity_str:
        return None
    try:
        now = datetime.datetime.now()
        dt = datetime.datetime.strptime(validity_str, "%B %d, %I %p PST")
        dt = dt.replace(year=now.year)
        tz = pytz.timezone("US/Pacific")
        dt = tz.localize(dt)
        return int(dt.timestamp())
    except Exception:
        return None


def send_discount_to_discord(
    mailbox: str,
    subject: str,
    code: str,
    percent: int | None,
    validity_str: str | None,
):
    epoch = parse_validity_to_epoch(validity_str)
    # Use :F for full date/time, or :R for relative
    if epoch is not None:
        validity_tag = f"<t:{epoch}:F>"
    else:
        validity_tag = validity_str or "unknown"

    discount_text = f"{percent}%" if percent is not None else "unknown"

    content = (
        f"📬 New 1min.ai discount code!\n"
        f"**Code:** `{code}`\n"
        f"**Discount:** {discount_text} OFF\n"
        f"**Valid until:** {validity_tag}\n"
    )

    try:
        resp = requests.post(
            DISCORD_WEBHOOK_URL,
            json={"content": content},
            timeout=5,
        )
        if resp.status_code >= 400:
            print(f"[DISCORD] Error {resp.status_code}: {resp.text}")
        else:
            print("[DISCORD] Notification sent.")
    except Exception as e:
        print(f"[DISCORD] Exception while sending webhook: {e}")


def extract_discount(body: str):
    deals_idx = body.find("Deals of the Week")
    if deals_idx == -1:
        return None, None, None
    section = body[deals_idx:deals_idx + 2000]

    code = None
    percent = None
    validity = None

    m_code = GENERIC_CODE_REGEX.search(section)
    if m_code:
        code = m_code.group(1).strip()

    m_pct = DISCOUNT_PERCENT_REGEX.search(section)
    if m_pct:
        percent = int(m_pct.group(1))

    m_valid = DISCOUNT_VALIDITY_REGEX.search(section)
    if m_valid:
        validity = m_valid.group(1).strip()

    return code, percent, validity


def connect_and_login():
    mail = imaplib.IMAP4_SSL(IMAP_HOST)
    mail.login(IMAP_USER, IMAP_PASS)
    return mail


def process_message(mail, mailbox, msg_id):
    status, data = mail.fetch(msg_id, "(RFC822)")
    if status != "OK" or not data:
        return
    raw_email = data[0][1]
    if not isinstance(raw_email, (bytes, bytearray)):
        print(f"[{mailbox}] Msg {msg_id.decode()} | Unexpected fetch type: {type(raw_email)}")
        return
    msg = email.message_from_bytes(raw_email)
    subject = decode_mime_words(msg.get("Subject"))
    body = get_text_from_msg(msg)

    code, percent, validity = extract_discount(body)

    if code:
        print(
            f"[{mailbox}] Msg {msg_id.decode()} | "
            f"Code: {code} | Discount: {percent if percent is not None else 'unknown'} | "
            f"Valid until: {validity if validity is not None else 'unknown'} | "
            f"Subject: {subject}"
        )
        send_discount_to_discord(mailbox, subject, code, percent, validity)
    else:
        print(f"[{mailbox}] Msg {msg_id.decode()} | No discount found | Subject: {subject}")


def idle_until_event(mail, mailbox, timeout=60):
    tag = mail._new_tag().decode()  # tag is bytes; decode to str
    mail.select(mailbox)

    # 1. Send IDLE command: tag + " IDLE"
    cmd = f"{tag} IDLE\r\n".encode()
    mail.send(cmd)

    # 2. Wait for continuation request: "+ ..."
    line = mail.readline()
    if not line.startswith(b"+"):
        mail.send(b"DONE\r\n")
        raise RuntimeError(f"IDLE not accepted: {line!r}")

    start = time.time()
    event = False

    # 3. Read untagged responses while IDLE is active
    while time.time() - start < timeout:
        line = mail.readline()
        if not line:
            continue
        if line.startswith(tag.encode()):
            # Tagged response for IDLE -> IDLE is done
            break
        if b"EXISTS" in line or b"EXPUNGE" in line:
            event = True

    # 4. Send DONE
    mail.send(b"DONE\r\n")

    # 5. Wait for tagged IDLE response (may have been read already)
    while True:
        line = mail.readline()
        if not line:
            continue
        if line.startswith(tag.encode()):
            break

    return event


def process_unseen_in_mailbox(mail, mailbox):
    status, _ = mail.select(mailbox)
    if status != "OK":
        print(f"[{mailbox}] Error selecting mailbox")
        return
    status, data = mail.search(None, "UNSEEN")
    if status == "OK":
        ids = data[0].split()
        if ids:
            print(f"[{mailbox}] Found {len(ids)} existing UNSEEN messages")
        for msg_id in ids:
            process_message(mail, mailbox, msg_id)


def loop():
    while True:
        try:
            mail = connect_and_login()
            try:
                # 1. Process existing UNSEEN messages first
                for mailbox in MAILBOXES:
                    try:
                        process_unseen_in_mailbox(mail, mailbox)
                    except Exception as e:
                        print(f"[{mailbox}] Error processing existing UNSEEN: {e}")

                # 2. Then go into IDLE and handle new events
                for mailbox in MAILBOXES:
                    try:
                        had_event = idle_until_event(mail, mailbox, timeout=60)
                        if had_event:
                            status, data = mail.search(None, "UNSEEN")
                            if status == "OK":
                                for msg_id in data[0].split():
                                    process_message(mail, mailbox, msg_id)
                    except Exception as e:
                        print(f"[{mailbox}] Error during IDLE: {e}")
            finally:
                try:
                    mail.logout()
                except Exception:
                    pass
        except Exception as e:
            print(f"[GLOBAL] Error: {e}")
            time.sleep(10)


if __name__ == "__main__":
    loop()
