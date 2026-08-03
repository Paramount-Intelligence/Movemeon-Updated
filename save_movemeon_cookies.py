"""
Local-only MoveMeOn session helper (Selenium login → cookies JSON file).

DEPRECATED for production: the live monitor persists sessions in Supabase via
database.save_scraper_session (platform='movemeon') — see monitor.py / script_clean.py.
MongoDB is no longer used for cookie storage.

Usage:
  python save_movemeon_cookies.py

Writes movemeon_cookies.json (cookies + localStorage) in the repo root.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone

from dotenv import load_dotenv
from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

load_dotenv(encoding="utf-8-sig")


class Config:
    MOVEMEON_EMAIL = os.getenv("MOVEMEON_EMAIL")
    MOVEMEON_PASSWORD = os.getenv("MOVEMEON_PASSWORD")
    TARGET_URL = "https://portal.movemeon.com/dashboard/candidate/jobs"
    SIGNIN_URL = "https://portal.movemeon.com/auth/signin"
    HEADLESS = os.getenv("HEADLESS", "False").lower() == "true"
    COOKIES_FILE = "movemeon_cookies.json"
    CHROME_BIN = os.getenv("CHROME_BIN")
    CHROMEDRIVER_PATH = os.getenv("CHROMEDRIVER_PATH")


def initialize_driver():
    options = Options()
    if Config.HEADLESS:
        options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    user_agent = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    options.add_argument(f"user-agent={user_agent}")
    if Config.CHROME_BIN:
        options.binary_location = Config.CHROME_BIN
    service = (
        Service(executable_path=Config.CHROMEDRIVER_PATH)
        if Config.CHROMEDRIVER_PATH
        else Service()
    )
    driver = webdriver.Chrome(service=service, options=options)
    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {
            "source": """
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined
            })
        """
        },
    )
    return driver


def is_logged_in(driver):
    try:
        if "dashboard/candidate/jobs" in driver.current_url:
            cards = driver.find_elements(
                By.CSS_SELECTOR,
                "div.rounded-xl.border.bg-card, a[href*='/jobs/']",
            )
            return len(cards) > 0
        return False
    except Exception:
        return False


def save_cookies(driver):
    try:
        cookies = driver.get_cookies()
        local_storage = driver.execute_script("return window.localStorage;")
        if not cookies:
            print("WARNING: No cookies found to save.")
            return False

        session_data = {
            "cookies": cookies,
            "local_storage": local_storage,
            "saved_at": datetime.now(timezone.utc).isoformat(),
        }
        with open(Config.COOKIES_FILE, "w", encoding="utf-8") as handle:
            json.dump(session_data, handle)
        print(
            f"SUCCESS: Session data saved to local file: {Config.COOKIES_FILE} "
            f"({len(cookies)} cookies)"
        )
        print(
            "NOTE: Production uses Supabase scraper_sessions via monitor.py — "
            "not MongoDB or this JSON file."
        )
        return True
    except Exception as exc:
        print(f"ERROR: Error saving session data: {exc}")
    return False


def perform_login(driver):
    try:
        print("Opening MoveMeOn sign-in page...")
        driver.get(Config.SIGNIN_URL)
        driver.maximize_window()
        time.sleep(3)
        print(f"Automated login attempted for: {Config.MOVEMEON_EMAIL}")

        email_field = None
        for sel in ("input[type='email']", "input[name='email']", "input[id*='email']"):
            try:
                email_field = WebDriverWait(driver, 10).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, sel))
                )
                if email_field:
                    break
            except Exception:
                continue
        if not email_field:
            raise RuntimeError("Email field not found")

        email_field.send_keys(Config.MOVEMEON_EMAIL)
        email_field.send_keys(Keys.ENTER)
        time.sleep(3)

        pass_field = None
        for sel in (
            "input[type='password']",
            "input[name='password']",
            "input[id*='password']",
        ):
            try:
                pass_field = WebDriverWait(driver, 10).until(
                    EC.visibility_of_element_located((By.CSS_SELECTOR, sel))
                )
                if pass_field:
                    break
            except Exception:
                continue
        if not pass_field:
            raise RuntimeError("Password field not found")

        pass_field.send_keys(Config.MOVEMEON_PASSWORD)
        pass_field.send_keys(Keys.ENTER)
        WebDriverWait(driver, 20).until(
            lambda d: "dashboard" in d.current_url
            or d.find_elements(By.CSS_SELECTOR, "div.rounded-xl.border.bg-card")
        )
        if "onboarding" in driver.current_url:
            print("  Detecting onboarding page. Attempting to click 'Skip'...")
            try:
                time.sleep(5)
                skip_btn = None
                for selector in (
                    "//button[contains(text(), 'Skip')]",
                    "//a[contains(text(), 'Skip')]",
                    "//*[contains(text(), 'Skip')]",
                    "button[class*='skip']",
                    "a[class*='skip']",
                ):
                    try:
                        if selector.startswith("//"):
                            skip_btn = driver.find_element(By.XPATH, selector)
                        else:
                            skip_btn = driver.find_element(By.CSS_SELECTOR, selector)
                        if skip_btn and skip_btn.is_displayed():
                            break
                    except Exception:
                        continue
                if skip_btn:
                    driver.execute_script("arguments[0].click();", skip_btn)
                    print("  Clicked 'Skip' on onboarding page.")
                    time.sleep(5)
            except Exception as exc_skip:
                print(f"  Failed to skip onboarding: {exc_skip}")
        return True
    except Exception as exc:
        print(f"WARNING: Automated login failed or timed out: {exc}")
        return False


def main():
    print("=" * 50)
    print("MoveMeOn Cookie Saver (local file only — MongoDB removed)")
    print("Production sessions: Supabase scraper_sessions via monitor.py")
    print("=" * 50)

    driver = initialize_driver()
    try:
        print("Opening MoveMeOn curated jobs page...")
        driver.get(Config.TARGET_URL)
        time.sleep(5)

        if is_logged_in(driver):
            print("SUCCESS: Login detected via existing session.")
        else:
            print("LOGIN REQUIRED.")
            if not perform_login(driver):
                print("\n" + "!" * 50)
                print("MANUAL LOGIN REQUIRED")
                print("Complete login in the browser, then press Enter here.")
                print("!" * 50 + "\n")
                input("Press Enter to continue...")

        if "dashboard/candidate/jobs" in driver.current_url or is_logged_in(driver):
            save_cookies(driver)
            print("\nValidating cookies (waiting 10s for Clerk to settle)...")
            driver.get(Config.TARGET_URL)
            time.sleep(10)
            if is_logged_in(driver):
                print("SUCCESS: Cookie validation successful.")
            else:
                print("FAILED: Cookie validation failed — redirected to login.")
        else:
            print("FAILED: Could not reach the jobs page.")
    except Exception as exc:
        print(f"ERROR: {exc}")
    finally:
        print("Done. Closing browser.")
        driver.quit()


if __name__ == "__main__":
    main()
