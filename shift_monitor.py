#!/usr/bin/env python3
"""
PVMG Schedule Shift Monitor - Single Run Version
Designed to be triggered every 30 minutes by GitHub Actions (see .github/workflows/monitor.yml)
Each run: logs in, scans all shift pages, alerts on NEW matching shifts via ntfy.sh, saves state.
"""

import os
import json
import time
import re
import logging
import requests
import icalendar
import recurring_ical_events
from datetime import datetime
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options

# ==================== CONFIGURATION ====================
# These come from GitHub Actions "Secrets" - never hardcoded here
WEBSITE_URL = "https://pvmg-schedule.com/#/management/advertised-shifts"
LOGIN_EMAIL = os.environ["PVMG_EMAIL"]
LOGIN_PASSWORD = os.environ["PVMG_PASSWORD"]
NTFY_TOPIC = os.environ["NTFY_TOPIC"]

# Kill switch: set the AUTO_TAKE_SG secret to "false" to instantly disable
# auto-taking without touching code. Defaults to on if not set.
AUTO_TAKE_SG = os.environ.get("AUTO_TAKE_SG", "true").strip().lower() == "true"

# Google Calendar conflict check (optional - only runs if GCAL_ICAL_URL is set)
GCAL_ICAL_URL = os.environ.get("GCAL_ICAL_URL", "")

# Tap-to-take button (optional - only added to notifications if both are set)
GH_DISPATCH_TOKEN = os.environ.get("GH_DISPATCH_TOKEN", "")
GH_REPO = os.environ.get("GH_REPO", "")  # e.g. "yourusername/shift-monitor"

# State file lives in the repo itself and gets committed back after each run
STATE_FILE = "state.json"

# ==================== LOGGING ====================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

def safe_click(driver, element):
    """Scroll element into view, then click it. Falls back to a JS click
    if a normal click is blocked by an overlapping element (icon, header, etc.)."""
    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", element)
    time.sleep(0.3)
    try:
        element.click()
    except Exception:
        driver.execute_script("arguments[0].click();", element)

def get_table_signature(driver):
    """A snapshot of the table's current visible content, used to detect
    when a page change has actually finished rendering (not just been clicked)."""
    try:
        rows = driver.find_elements(By.XPATH, "//table//tbody//tr")
        return "|".join(row.text for row in rows)
    except Exception:
        return None

def wait_for_table_change(driver, previous_signature, timeout=10):
    """Poll until the table's content differs from the given previous snapshot,
    or until timeout. Returns True if a change was detected."""
    start = time.time()
    while time.time() - start < timeout:
        current = get_table_signature(driver)
        if current is not None and current != previous_signature and current != "":
            return True
        time.sleep(0.3)
    return False

# ==================== FILTER LOGIC ====================

def is_weekday(date_string):
    """date_string format: 'Mon, Sep 7, 2026' -> True if Mon-Fri"""
    try:
        day = date_string.split(',')[0].strip()
        return day not in ['Sat', 'Sun']
    except Exception:
        return False

def should_alert(shift_type, date_string):
    """
    Rules:
    - VP1 or VP2 (exact match, excludes VPOB): Mon-Fri only
    - Second or Third (exact match): Mon-Fri only
    - Any shift containing SG: any day
    """
    t = shift_type.strip()

    if t in ['VP1', 'VP2'] and is_weekday(date_string):
        return True
    if t in ['Second', 'Third'] and is_weekday(date_string):
        return True
    if 'SG' in t:
        return True
    return False

# ==================== STATE (persisted via git commit) ====================

def get_previous_shifts():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r') as f:
                return json.load(f)
        except Exception:
            return []
    return []

def save_shifts(shifts):
    with open(STATE_FILE, 'w') as f:
        json.dump(shifts, f, indent=2)

def format_shift(shift):
    return f"{shift['date']}|{shift['hospital']}|{shift['shift_type']}"

# ==================== NOTIFICATIONS ====================

def get_calendar_events_for_date(shift_date_str):
    """shift_date_str like 'Mon, Sep 7, 2026' -> fetch the calendar and return
    a list of event names occurring that day. Returns None if not configured
    or if something went wrong (so callers can skip the conflict line silently)."""
    if not GCAL_ICAL_URL:
        return None
    try:
        date_part = shift_date_str.split(",", 1)[1].strip()  # "Sep 7, 2026"
        target_date = datetime.strptime(date_part, "%b %d, %Y").date()

        response = requests.get(GCAL_ICAL_URL, timeout=15)
        response.raise_for_status()
        cal = icalendar.Calendar.from_ical(response.text)

        start = datetime.combine(target_date, datetime.min.time())
        end = datetime.combine(target_date, datetime.max.time())
        events = recurring_ical_events.of(cal).between(start, end)

        return [str(event.get("SUMMARY", "Untitled event")) for event in events]
    except Exception as e:
        logger.error(f"Error checking calendar for {shift_date_str}: {e}")
        return None

def format_conflict_line(shift_date_str):
    events = get_calendar_events_for_date(shift_date_str)
    if events is None:
        return ""  # not configured, or the check failed - say nothing rather than guess
    if events:
        return "\n\nYour calendar already has:\n" + "\n".join(f"- {e}" for e in events)
    return "\n\nNo conflicts on your calendar that day."

def build_take_action_header(shift):
    """Build an ntfy 'Actions' header containing a button that, when tapped,
    triggers a GitHub Actions run to take this specific shift. Returns None
    if the required secrets aren't configured."""
    if not GH_DISPATCH_TOKEN or not GH_REPO:
        return None

    payload = {
        "event_type": "take_shift",
        "client_payload": {
            "date": shift["date"],
            "hospital": shift["hospital"],
            "shift_type": shift["shift_type"],
            "provider": shift["provider"],
        }
    }
    body_json = json.dumps(payload)

    def esc(s):
        # ntfy's Actions header uses commas/semicolons as field separators,
        # so any that appear inside a value must be escaped
        return s.replace(",", "\\,").replace(";", "\\;")

    url = f"https://api.github.com/repos/{GH_REPO}/dispatches"
    return (
        f"http, Take Shift, {esc(url)}, method=POST, "
        f"headers.Authorization=Bearer {GH_DISPATCH_TOKEN}, "
        f"headers.Accept=application/vnd.github+json, "
        f"body={esc(body_json)}, clear=true"
    )

def send_ntfy(title, message, actions_header=None):
    try:
        url = f"https://ntfy.sh/{NTFY_TOPIC}"
        headers = {"Title": title, "Priority": "high"}
        if actions_header:
            headers["Actions"] = actions_header
        response = requests.post(url, data=message.encode('utf-8'), headers=headers)
        if response.status_code == 200:
            logger.info(f"Push notification sent to ntfy.sh/{NTFY_TOPIC}")
        else:
            logger.error(f"ntfy.sh error: {response.status_code}")
    except Exception as e:
        logger.error(f"Error sending push notification: {e}")

# ==================== WEB SCRAPING ====================

def login_to_website(driver):
    """Log in if needed; if already logged in (session cookie), just continues."""
    logger.info("Navigating to advertised shifts page...")
    driver.get(WEBSITE_URL)
    time.sleep(2)

    try:
        sign_in_button = WebDriverWait(driver, 5).until(
            EC.element_to_be_clickable((By.XPATH, "//button[contains(text(), 'Sign In to Calendar')]"))
        )
        logger.info("Sign In button found - logging in")
        safe_click(driver, sign_in_button)

        # Clicking navigates to a separate /#/login page
        email_field = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.XPATH, "//input[@placeholder='you@example.com']"))
        )
        email_field.send_keys(LOGIN_EMAIL)

        password_field = driver.find_element(By.XPATH, "//input[@placeholder='Enter your password']")
        password_field.send_keys(LOGIN_PASSWORD)

        submit_button = driver.find_element(By.XPATH, "//button[normalize-space(.)='Sign In']")
        safe_click(driver, submit_button)
        logger.info("Submitted login form")

        time.sleep(3)
        # After login it redirects to homepage - go back to the shifts page
        driver.get(WEBSITE_URL)
        time.sleep(2)

    except Exception as e:
        logger.warning(f"Login flow raised an exception (may already be logged in, or a selector is wrong): {e}")
        driver.save_screenshot("debug_screenshot.png")
        with open("debug_page.html", "w", encoding="utf-8") as f:
            f.write(driver.page_source)
        logger.warning("Saved debug_screenshot.png and debug_page.html for inspection")

    try:
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.XPATH, "//button[contains(text(), 'List View')]"))
        )
        logger.info("On advertised shifts page, ready to proceed")
    except Exception as e:
        logger.error(f"Never reached the shifts list page: {e}")
        driver.save_screenshot("debug_screenshot.png")
        with open("debug_page.html", "w", encoding="utf-8") as f:
            f.write(driver.page_source)
        logger.error("Saved debug_screenshot.png and debug_page.html for inspection")
        raise

def click_list_view(driver):
    btn = driver.find_element(By.XPATH, "//button[contains(text(), 'List View')]")
    safe_click(driver, btn)
    WebDriverWait(driver, 15).until(
        EC.presence_of_element_located((By.XPATH, "//table//tbody//tr"))
    )
    time.sleep(1)  # let the last row or two settle in
    logger.info("Clicked List View, table has loaded")

def extract_shifts_from_table(driver):
    shifts = []
    rows = driver.find_elements(By.XPATH, "//table//tbody//tr")
    logger.info(f"Found {len(rows)} rows on this page")

    for row in rows:
        try:
            cells = row.find_elements(By.TAG_NAME, "td")
            if len(cells) >= 3:
                date = cells[0].text.strip()
                hospital = cells[1].text.strip()
                shift_type = cells[2].text.strip()
                provider = cells[3].text.strip() if len(cells) > 3 else "Unknown"

                if should_alert(shift_type, date):
                    shifts.append({
                        "date": date,
                        "hospital": hospital,
                        "shift_type": shift_type,
                        "provider": provider
                    })
                    logger.info(f"Matched: {shift_type} on {date} at {hospital}")
        except Exception:
            continue

    return shifts

def get_expected_page_count(driver):
    """Read the site's own 'Page X of Y' text so we can verify we didn't stop early."""
    import re
    try:
        candidates = driver.find_elements(
            By.XPATH, "//*[contains(., 'Page') and contains(., 'of')]"
        )
        # Prefer the most specific (shortest text) match, since a broad match
        # could otherwise grab a large parent container instead of the label itself
        candidates = sorted(candidates, key=lambda el: len(el.text))
        for el in candidates:
            match = re.search(r"Page\s*(\d+)\s*of\s*(\d+)", el.text)
            if match:
                return int(match.group(2))
        logger.warning(f"Found {len(candidates)} candidate element(s) but none matched 'Page X of Y' pattern")
        return None
    except Exception as e:
        logger.warning(f"Could not read the site's own page-count indicator: {e}")
        return None

def find_next_page_button(driver):
    """Find the 'next page' arrow button by its position relative to the
    'Page X of Y' text, rather than guessing its CSS class (which we don't
    know and which the site doesn't label with visible text)."""
    import re
    candidates = driver.find_elements(By.XPATH, "//*[contains(., 'Page') and contains(., 'of')]")
    candidates = sorted(candidates, key=lambda el: len(el.text))
    target = None
    for el in candidates:
        if re.search(r"Page\s*\d+\s*of\s*\d+", el.text):
            target = el
            break
    if target is None:
        return None

    container = target
    for _ in range(5):
        try:
            container = container.find_element(By.XPATH, "..")
        except Exception:
            break
        buttons = container.find_elements(By.TAG_NAME, "button")
        if len(buttons) >= 2:
            return buttons[-1]  # last button next to the page text = the right/next arrow
    return None

def navigate_pages(driver):
    all_shifts = []
    page_num = 1
    expected_total_pages = get_expected_page_count(driver)
    if expected_total_pages:
        logger.info(f"Site reports {expected_total_pages} total page(s)")

    while True:
        logger.info(f"Scanning page {page_num}...")
        signature_before_next = get_table_signature(driver)
        all_shifts.extend(extract_shifts_from_table(driver))

        next_button = find_next_page_button(driver)
        if next_button is None:
            logger.info("No 'next' button found (likely last page)")
            break

        if not next_button.is_enabled():
            logger.info("Next button found but disabled - this is the last page")
            break

        safe_click(driver, next_button)
        changed = wait_for_table_change(driver, signature_before_next, timeout=10)
        if not changed:
            logger.warning("Table did not visibly change after clicking next - "
                           "may be reading stale data, stopping pagination here")
            break
        page_num += 1

    logger.info(f"Total pages scanned: {page_num}")
    if expected_total_pages and page_num != expected_total_pages:
        error_msg = (
            f"Site says {expected_total_pages} page(s) exist, "
            f"but only {page_num} were scanned - some shifts may have been missed!"
        )
        logger.error(f"MISMATCH: {error_msg}")
        send_ntfy("Shift Monitor Error", error_msg)
    return all_shifts

# ==================== AUTO-TAKE (SG shifts only) ====================

def find_row_button_for_shift(driver, shift, max_pages=10):
    """Search all pages for a row matching this shift's exact date/hospital/
    shift_type/provider, and return its action button, or None if not found
    (e.g. someone else already took it)."""
    for _ in range(max_pages):
        rows = driver.find_elements(By.XPATH, "//table//tbody//tr")
        for row in rows:
            try:
                cells = row.find_elements(By.TAG_NAME, "td")
                if len(cells) >= 4:
                    if (cells[0].text.strip() == shift['date'] and
                            cells[1].text.strip() == shift['hospital'] and
                            cells[2].text.strip() == shift['shift_type'] and
                            cells[3].text.strip() == shift['provider']):
                        buttons = row.find_elements(By.TAG_NAME, "button")
                        if buttons:
                            return buttons[0]
            except Exception:
                continue

        next_button = find_next_page_button(driver)
        if next_button is None or not next_button.is_enabled():
            break
        signature_before = get_table_signature(driver)
        safe_click(driver, next_button)
        wait_for_table_change(driver, signature_before, timeout=10)

    return None

def read_modal_shift_details(driver):
    """Read the Date/Hospital/Shift Type/Current Provider fields out of the
    'Confirm Take Shift' modal, so we can verify before confirming."""
    full_text = driver.find_element(By.TAG_NAME, "body").text

    def extract(label):
        match = re.search(rf"{re.escape(label)}\s*\n?\s*(.+)", full_text)
        return match.group(1).strip() if match else None

    return {
        "date": extract("Date:"),
        "hospital": extract("Hospital:"),
        "shift_type": extract("Shift Type:"),
        "provider": extract("Current Provider:"),
    }

def attempt_auto_take(driver, shift):
    """Re-locate a newly detected shift, click Take Shift, verify the
    confirmation modal matches exactly, then Confirm or back out with Cancel.
    Returns a status string describing what happened. Safe to call whether
    the driver is already logged in (mid-run) or completely fresh (a
    standalone take-shift run triggered by a notification tap)."""
    logger.info(f"Attempting auto-take for shift: {shift}")
    try:
        login_to_website(driver)
        click_list_view(driver)

        button = find_row_button_for_shift(driver, shift)
        if button is None:
            logger.warning("Could not re-locate this shift - it may already be taken")
            return "not_found"

        button_text = button.text.strip()
        if button_text != "Take Shift":
            logger.info(f"Shift not available to take (button says '{button_text}')")
            return "unavailable"

        safe_click(driver, button)

        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.XPATH, "//*[contains(text(), 'Confirm Take Shift')]"))
        )
        modal_details = read_modal_shift_details(driver)
        logger.info(f"Confirmation modal shows: {modal_details}")

        matches = (
            modal_details.get("date") == shift["date"] and
            modal_details.get("hospital") == shift["hospital"] and
            modal_details.get("shift_type") == shift["shift_type"] and
            modal_details.get("provider") == shift["provider"]
        )

        if matches:
            confirm_button = driver.find_element(By.XPATH, "//button[normalize-space(.)='Confirm']")
            safe_click(driver, confirm_button)
            logger.info("Confirmed - shift taken")
            return "taken"
        else:
            logger.error(f"Modal mismatch - expected {shift}, got {modal_details}. Cancelling.")
            driver.save_screenshot("debug_screenshot.png")
            with open("debug_page.html", "w", encoding="utf-8") as f:
                f.write(driver.page_source)
            try:
                cancel_button = driver.find_element(By.XPATH, "//button[normalize-space(.)='Cancel']")
                safe_click(driver, cancel_button)
            except Exception:
                pass
            return "mismatch"

    except Exception as e:
        logger.error(f"Error during auto-take attempt: {e}")
        try:
            driver.save_screenshot("debug_screenshot.png")
            with open("debug_page.html", "w", encoding="utf-8") as f:
                f.write(driver.page_source)
        except Exception:
            pass
        return "error"

# ==================== MAIN (single run) ====================

def build_result_notification(shift, result):
    title = {
        "taken": f"Taken - {shift['shift_type']}",
        "unavailable": f"Not Available - {shift['shift_type']}",
        "mismatch": f"Verification Failed - {shift['shift_type']}",
        "not_found": f"Could Not Find Shift - {shift['shift_type']}",
        "error": f"Error Taking Shift - {shift['shift_type']}",
    }.get(result, f"Auto-Take Result - {shift['shift_type']}")
    body = {
        "taken": f"Successfully took the shift.\nDate: {shift['date']}\nHospital: {shift['hospital']}\nProvider: {shift['provider']}",
        "unavailable": f"Shift was no longer available to take.\nDate: {shift['date']}\nHospital: {shift['hospital']}",
        "mismatch": f"Found the shift but confirmation details didn't match - backed out without taking it. Check manually.\nDate: {shift['date']}\nHospital: {shift['hospital']}",
        "not_found": f"Could not find this shift again - it may already be taken.\nDate: {shift['date']}\nHospital: {shift['hospital']}",
        "error": f"An error occurred trying to take this shift. Check manually.\nDate: {shift['date']}\nHospital: {shift['hospital']}",
    }.get(result, str(result))
    return title, body

def run_check():
    chrome_options = Options()
    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-blink-features=AutomationControlled")

    driver = webdriver.Chrome(options=chrome_options)
    driver.set_page_load_timeout(30)

    try:
        logger.info("=" * 60)
        logger.info("Starting shift check")

        login_to_website(driver)
        click_list_view(driver)
        current_shifts = navigate_pages(driver)
        logger.info(f"Total matching shifts this run: {len(current_shifts)}")

        previous_shifts = get_previous_shifts()
        previous_ids = set(format_shift(s) for s in previous_shifts)

        new_shifts = [s for s in current_shifts if format_shift(s) not in previous_ids]

        if new_shifts:
            logger.info(f"Found {len(new_shifts)} NEW shifts - sending alerts")
            for shift in new_shifts:
                title = f"Alert - {shift['shift_type']}"
                body = (
                    f"Date: {shift['date']}\n"
                    f"Hospital: {shift['hospital']}\n"
                    f"Provider: {shift['provider']}"
                    f"{format_conflict_line(shift['date'])}"
                )
                # SG shifts are auto-taken separately below with no button needed.
                # Everything else gets a tap-to-take button, if configured.
                action_header = None if 'SG' in shift['shift_type'] else build_take_action_header(shift)
                send_ntfy(title, body, actions_header=action_header)

            sg_new_shifts = [s for s in new_shifts if 'SG' in s['shift_type']]
            if sg_new_shifts and not AUTO_TAKE_SG:
                logger.info(f"AUTO_TAKE_SG is disabled - skipping auto-take for {len(sg_new_shifts)} SG shift(s)")
            elif sg_new_shifts:
                for shift in sg_new_shifts:
                    result = attempt_auto_take(driver, shift)
                    result_title, result_body = build_result_notification(shift, result)
                    send_ntfy(result_title, result_body)
        else:
            logger.info("No new shifts detected")

        save_shifts(current_shifts)
        logger.info("State saved")

    finally:
        driver.quit()

def run_take_single_shift():
    """Standalone mode: take exactly one specific shift, identified by the
    TAKE_SHIFT_* environment variables set by the button-tap workflow.
    Does not scan the site or touch state.json at all."""
    shift = {
        "date": os.environ.get("TAKE_SHIFT_DATE", ""),
        "hospital": os.environ.get("TAKE_SHIFT_HOSPITAL", ""),
        "shift_type": os.environ.get("TAKE_SHIFT_TYPE", ""),
        "provider": os.environ.get("TAKE_SHIFT_PROVIDER", ""),
    }
    logger.info(f"Take-single-shift mode triggered for: {shift}")

    chrome_options = Options()
    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-blink-features=AutomationControlled")

    driver = webdriver.Chrome(options=chrome_options)
    driver.set_page_load_timeout(30)
    try:
        result = attempt_auto_take(driver, shift)
    finally:
        driver.quit()

    title, body = build_result_notification(shift, result)
    send_ntfy(title, body)

if __name__ == "__main__":
    try:
        if os.environ.get("TAKE_SHIFT_DATE"):
            run_take_single_shift()
        else:
            run_check()
    except Exception as e:
        error_msg = f"The shift monitor crashed and stopped early: {e}"
        logger.error(error_msg)
        try:
            send_ntfy("Shift Monitor Error", error_msg)
        except Exception:
            pass  # don't let a failed notification hide the original error
        raise  # still mark this run as failed in GitHub, so the Actions log shows it too
