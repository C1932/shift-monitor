#!/usr/bin/env python3
"""
PVMG Schedule Shift Monitor - Single Run Version
Designed to be triggered every 30 minutes by GitHub Actions (see .github/workflows/monitor.yml)
Each run: logs in, scans all shift pages, alerts on NEW matching shifts via ntfy.sh, saves state.
"""

import os
import json
import time
import logging
import requests
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

# State file lives in the repo itself and gets committed back after each run
STATE_FILE = "state.json"

# ==================== LOGGING ====================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

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

def send_ntfy(title, message):
    try:
        url = f"https://ntfy.sh/{NTFY_TOPIC}"
        headers = {"Title": title, "Priority": "high"}
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
        sign_in_button.click()

        # Clicking navigates to a separate /#/login page
        email_field = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.XPATH, "//input[@placeholder='you@example.com']"))
        )
        email_field.send_keys(LOGIN_EMAIL)

        password_field = driver.find_element(By.XPATH, "//input[@placeholder='Enter your password']")
        password_field.send_keys(LOGIN_PASSWORD)

        submit_button = driver.find_element(By.XPATH, "//button[normalize-space(.)='Sign In']")
        submit_button.click()
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
    btn.click()
    time.sleep(2)
    logger.info("Clicked List View")

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

def navigate_pages(driver):
    all_shifts = []
    page_num = 1

    while True:
        logger.info(f"Scanning page {page_num}...")
        all_shifts.extend(extract_shifts_from_table(driver))

        try:
            next_button = driver.find_element(By.XPATH, "//button[contains(@class, 'next')]")
            if next_button.is_enabled():
                next_button.click()
                time.sleep(2)
                page_num += 1
            else:
                break
        except Exception:
            break

    logger.info(f"Total pages scanned: {page_num}")
    return all_shifts

# ==================== MAIN (single run) ====================

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
                title = f"NEW {shift['shift_type']} SHIFT"
                body = (
                    f"Date: {shift['date']}\n"
                    f"Hospital: {shift['hospital']}\n"
                    f"Provider: {shift['provider']}"
                )
                send_ntfy(title, body)
        else:
            logger.info("No new shifts detected")

        save_shifts(current_shifts)
        logger.info("State saved")

    finally:
        driver.quit()

if __name__ == "__main__":
    run_check()
