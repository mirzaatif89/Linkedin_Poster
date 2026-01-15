import csv
import base64
import json
import os
import re
import threading
import time
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import webview
from selenium import webdriver
from selenium.common.exceptions import NoSuchElementException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.pdfgen import canvas
from PIL import Image, ImageDraw, ImageFont
try:
    import undetected_chromedriver as uc
except ImportError:  # uc is optional; fallback to standard driver
    uc = None

USE_UNDETECTED = False  # flip to True only if standard Chrome driver fails

UI_DIR = Path(__file__).parent / "ui"
INDEX_HTML = UI_DIR / "index.html"
ENV_PATH = Path(__file__).parent / ".env"


# ---------- Query and data helpers ----------
def build_keywords(search_term: str, filters: Dict[str, str]) -> str:
    """Combine base search term with simple boolean filters for LinkedIn query."""
    segments = [search_term.strip()]
    if filters.get("title"):
        segments.append(f'title:"{filters["title"].strip()}"')
    if filters.get("company"):
        segments.append(f'company:"{filters["company"].strip()}"')
    if filters.get("location"):
        segments.append(f'location:"{filters["location"].strip()}"')
    if filters.get("industry"):
        segments.append(f'industry:"{filters["industry"].strip()}"')
    return " AND ".join([s for s in segments if s])


def clean_csv_field(value: str) -> str:
    """Normalize whitespace for CSV output."""
    return re.sub(r"\s+", " ", value or "").strip()


def ensure_output_dir() -> Path:
    output_dir = Path("output")
    output_dir.mkdir(exist_ok=True)
    return output_dir


def load_env() -> None:
    if not ENV_PATH.exists():
        return
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


# ---------- Core scraper API exposed to JS ----------
class ScraperAPI:
    def __init__(self) -> None:
        self.logs: List[str] = []
        self.status: str = "idle"
        self.output_path: Optional[str] = None
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._resume_event = threading.Event()
        self._poster_thread: Optional[threading.Thread] = None
        self._poster_driver: Optional[webdriver.Chrome] = None

    # Progress -------------------------------------------------
    def _push_log(self, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        with self._lock:
            self.logs.append(f"[{timestamp}] {message}")

    def get_progress(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "logs": list(self.logs),
                "status": self.status,
                "output_path": self.output_path,
            }

    # Public entry point ---------------------------------------
    def start_scrape(self, payload: Dict[str, Any]) -> Dict[str, str]:
        if self.status == "running":
            return {"status": "running"}

        self.status = "running"
        self.logs = []
        self.output_path = None
        self._resume_event.clear()
        self._push_log("Preparing the browser and logging in...")
        self._thread = threading.Thread(target=self._scrape, args=(payload,), daemon=True)
        self._thread.start()
        return {"status": "running"}

    def resume_after_verification(self) -> Dict[str, str]:
        if self.status != "verification":
            return {"status": self.status}
        self._push_log("Continue clicked. Resuming after manual verification.")
        self._resume_event.set()
        return {"status": "running"}

    def poster_login(self, payload: Dict[str, Any]) -> Dict[str, str]:
        email = (payload.get("email") or "").strip()
        password = payload.get("password") or ""
        if not email or not password:
            self._push_log("Poster login missing email or password.")
            return {"status": "error"}
        if self._poster_thread and self._poster_thread.is_alive():
            return {"status": "running"}
        self._poster_thread = threading.Thread(target=self._poster_login, args=(email, password), daemon=True)
        self._poster_thread.start()
        return {"status": "running"}

    def poster_generate_and_post(self, payload: Dict[str, Any]) -> Dict[str, str]:
        title = (payload.get("title") or "").strip()
        email = (payload.get("email") or "").strip()
        password = payload.get("password") or ""
        if not title:
            self._push_log("Poster missing title.")
            return {"status": "error"}
        if self._poster_thread and self._poster_thread.is_alive():
            return {"status": "running"}
        self._poster_thread = threading.Thread(
            target=self._poster_generate_and_post, args=(email, password, title), daemon=True
        )
        self._poster_thread.start()
        return {"status": "running"}

    # Scraping -------------------------------------------------
    def _scrape(self, payload: Dict[str, Any]) -> None:
        data = self._normalize_payload(payload)
        if not data["email"] or not data["password"] or not data["search_term"]:
            self._push_log("Missing required fields.")
            self.status = "error"
            return

        driver: Optional[webdriver.Chrome] = None
        results: List[Dict[str, str]] = []

        try:
            driver = self._create_driver()
            self._login(driver, data["email"], data["password"])
            query = build_keywords(data["search_term"], data["filters"])
            base_url = self._build_posts_search_url(query, data["sort_by"], data["date_posted"])
            self._push_log(f"Using posts query: {query}")

            for page in range(1, data["page_limit"] + 1):
                self._push_log(f"Scraping page {page}/{data['page_limit']}...")
                page_results = self._scrape_posts_page(driver, base_url, page)
                results.extend(page_results)

            self._push_log(f"Collected {len(results)} posts. Writing CSV...")
            self.output_path = self._write_csv(results)
            self._push_log(f"Done. Saved to {self.output_path}")
            self.status = "idle"
        except Exception as exc:  # broad catch to surface errors in UI
            self._push_log(f"Error: {exc}")
            self.status = "error"
        finally:
            if driver:
                driver.quit()

    def _poster_login(self, email: str, password: str) -> None:
        try:
            if self._poster_driver:
                try:
                    self._poster_driver.quit()
                except Exception:
                    pass
            self._poster_driver = self._create_driver()
            self._push_log("Poster login: opening LinkedIn...")
            self._login(self._poster_driver, email, password)
            self._push_log("Poster login complete.")
        except Exception as exc:
            self._push_log(f"Poster login error: {exc}")

    def _poster_generate_and_post(self, email: str, password: str, title: str) -> None:
        try:
            if email and password:
                if not self._poster_driver:
                    self._poster_driver = self._create_driver()
                self._push_log("Poster login: opening LinkedIn...")
                self._login(self._poster_driver, email, password)
                self._push_log("Poster login complete.")
            content = self._generate_post_content(title)
            image_path = self._generate_post_image(content["title"])
            if image_path:
                self._push_log(f"Image saved: {image_path}")
            else:
                self._push_log("Image generation failed or skipped.")
            pdf_path = self._generate_post_pdf(content["title"], content["description"], image_path)
            if pdf_path:
                self._push_log(f"PDF saved: {pdf_path}")
        except Exception as exc:
            self._push_log(f"Poster post error: {exc}")

    def _normalize_payload(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        page_limit = payload.get("pages") or 1
        try:
            page_limit = max(1, int(page_limit))
        except ValueError:
            page_limit = 1

        return {
            "email": (payload.get("email") or "").strip(),
            "password": payload.get("password") or "",
            "search_term": (payload.get("searchTerm") or "").strip(),
            "notes": (payload.get("notes") or "").strip(),
            "page_limit": page_limit,
            "sort_by": (payload.get("sortBy") or "relevance").strip(),
            "date_posted": (payload.get("datePosted") or "").strip(),
            "filters": {
                "location": (payload.get("location") or "").strip(),
                "industry": (payload.get("industry") or "").strip(),
                "title": (payload.get("title") or "").strip(),
                "company": (payload.get("company") or "").strip(),
            },
        }

    def _create_driver(self) -> webdriver.Chrome:
        options = Options()
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument("--disable-notifications")
        options.add_argument("--start-maximized")
        options.add_argument("--log-level=3")
        options.add_argument("--disable-infobars")
        # Some Chrome/driver builds reject excludeSwitches; keep options minimal for compatibility.
        if USE_UNDETECTED and uc:
            return uc.Chrome(options=options, use_subprocess=True)
        local_driver = Path("C:/chromeDriver/chromedriver.exe")
        if local_driver.exists():
            return webdriver.Chrome(service=Service(str(local_driver)), options=options)
        return webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)

    def _login(self, driver: webdriver.Chrome, email: str, password: str) -> None:
        driver.get("https://www.linkedin.com/login")
        time.sleep(2)
        driver.find_element(By.ID, "username").send_keys(email)
        driver.find_element(By.ID, "password").send_keys(password)
        driver.find_element(By.CSS_SELECTOR, "button[type='submit']").click()
        time.sleep(3)
        self._push_log("Login attempt complete.")
        self._handle_verification_if_needed(driver)

    def _handle_verification_if_needed(self, driver: webdriver.Chrome) -> None:
        if not self._is_verification_prompt_present(driver):
            return
        self.status = "verification"
        self._push_log("Verification required. Enter the code in the LinkedIn window, then click 'Continue' in the app.")
        self._resume_event.clear()
        self._resume_event.wait()
        self._push_log("Verification acknowledged, continuing.")
        self.status = "running"
        time.sleep(1.5)

    def _is_verification_prompt_present(self, driver: webdriver.Chrome) -> bool:
        # Heuristic selectors commonly present on LinkedIn verification pages
        selectors = [
            "input#input__email_verification_pin",
            "input[name='pin']",
            "input[autocomplete='one-time-code']",
            "input[placeholder*='code']",
        ]
        for selector in selectors:
            elements = driver.find_elements(By.CSS_SELECTOR, selector)
            if elements:
                return True
        # fallback: look for heading text
        headings = driver.find_elements(By.CSS_SELECTOR, "h1, h2")
        for h in headings:
            text = h.text.lower()
            if "verification" in text or "security check" in text or "enter code" in text:
                return True
        return False

    def _build_posts_search_url(self, keywords: str, sort_by: str, date_posted: str) -> str:
        encoded = json.dumps(keywords)[1:-1]  # simple escape for quotes
        params = [("keywords", encoded), ("origin", "GLOBAL_SEARCH_HEADER")]
        if sort_by == "recent":
            params.append(("sortBy", "recent"))
        if date_posted in {"past-24h", "past-week", "past-month"}:
            params.append(("datePosted", date_posted))
        query = "&".join(f"{k}={v}" for k, v in params)
        return f"https://www.linkedin.com/search/results/content/?{query}"

    def _scrape_posts_page(self, driver: webdriver.Chrome, base_url: str, page: int) -> List[Dict[str, str]]:
        driver.get(f"{base_url}&page={page}")
        self._wait_for_results(driver)
        self._scroll_once(driver)
        cards = self._find_cards(driver)
        parsed: List[Dict[str, str]] = []
        for card in cards:
            data = self._parse_post_card(card)
            if data:
                parsed.append(data)
        self._push_log(f"Page {page}: {len(parsed)} posts captured.")
        return parsed

    def _wait_for_results(self, driver: webdriver.Chrome, timeout: int = 12) -> None:
        try:
            WebDriverWait(driver, timeout).until(
                EC.presence_of_element_located(
                    (
                        By.CSS_SELECTOR,
                        "div.feed-shared-update-v2, li.reusable-search__result-container, div.reusable-search__result-container, div.search-reusables__entry, article",
                    )
                )
            )
        except Exception:
            self._push_log("No results detected yet; continuing.")

    def _scroll_once(self, driver: webdriver.Chrome) -> None:
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight * 0.6);")
        time.sleep(1.5)

    def _find_cards(self, driver: webdriver.Chrome):
        cards = driver.find_elements(By.CSS_SELECTOR, "div.feed-shared-update-v2, li.reusable-search__result-container, div.reusable-search__result-container, div.search-reusables__entry")
        if not cards:
            # fallback: any article within search results
            cards = driver.find_elements(By.CSS_SELECTOR, "article")
        if not cards:
            self._push_log("No post cards found on this page.")
        return cards

    def _safe_text(self, root, selector: str) -> str:
        els = root.find_elements(By.CSS_SELECTOR, selector)
        for el in els:
            text = el.text.strip()
            if text:
                return text
        return ""

    def _parse_post_card(self, card) -> Optional[Dict[str, str]]:
        author = self._safe_text(
            card,
            "span.update-components-actor__title span[dir='ltr'], "
            "span.entity-result__title-text a span[dir='ltr'], "
            "span.feed-shared-actor__name, "
            "a.app-aware-link[href*='/in/'] span[aria-hidden='true']",
        )
        post_time = self._safe_text(
            card,
            "span.update-components-actor__sub-description span.visually-hidden, "
            "span.update-components-actor__sub-description, "
            "span.feed-shared-actor__sub-description span.visually-hidden, "
            "span.feed-shared-actor__sub-description",
        )

        snippet = self._safe_text(
            card,
            "div.feed-shared-update-v2__description-wrapper span[dir='ltr'], "
            "div.feed-shared-text-view span[dir='ltr'], "
            "div.entity-result__summary span[dir='ltr'], "
            "div.update-components-text span[dir='ltr'], "
            "span.break-words",
        )

        post_url = self._extract_post_link(card)
        job_title = self._extract_job_title_from_text(snippet)
        location = self._extract_location_from_text(snippet)
        job_type = self._extract_job_type_from_text(snippet)
        workplace = self._extract_workplace_type_from_text(snippet)
        qualifications = self._extract_section_from_text(snippet, ["qualification", "requirements", "must have", "should have", "education"])
        skills = self._extract_section_from_text(snippet, ["skills", "tech stack", "technologies", "experience with"])
        contact = self._extract_contact_details(snippet)
        salary = self._extract_salary(snippet)

        if not (author or snippet or post_url):
            self._push_log("Skipped a card (no author/snippet/link detected).")
            return None

        return {
            "Author name": clean_csv_field(author),
            "Job Tittle": clean_csv_field(job_title),
            "Location": clean_csv_field(location),
            "Job type(full time/ Part-Time)": clean_csv_field(job_type),
            "Remote/Onsite": clean_csv_field(workplace),
            "Job Description": clean_csv_field(snippet),
            "Post Date/Time": clean_csv_field(post_time),
            "required Qualification": clean_csv_field(qualifications),
            "Required SKills": clean_csv_field(skills),
            "Post Link": clean_csv_field(post_url),
            "Contact Destil (email/web-link/Contact number)": clean_csv_field(contact),
            "salary Pkg": clean_csv_field(salary),
        }

    def _extract_post_link(self, card) -> str:
        links = card.find_elements(By.CSS_SELECTOR, "a.app-aware-link")
        urls = [l.get_attribute("href") for l in links if l.get_attribute("href")]
        for url in urls:
            if "/posts/" in url or "/feed/update" in url:
                return url.split("?")[0]
        for url in urls:
            if "urn:li:activity:" in url:
                return f"https://www.linkedin.com/feed/update/{url.split('?')[0]}"
        for url in urls:
            if url.startswith("http"):
                return url.split("?")[0]
        data_urn = card.get_attribute("data-urn") or ""
        if "urn:li:activity:" in data_urn:
            return f"https://www.linkedin.com/feed/update/{data_urn}"
        urn_el = card.find_elements(By.CSS_SELECTOR, "[data-urn]")
        for el in urn_el:
            urn = el.get_attribute("data-urn") or ""
            if "urn:li:activity:" in urn:
                return f"https://www.linkedin.com/feed/update/{urn}"
        return urls[0].split("?")[0] if urls else ""

    def _extract_job_title_from_text(self, text: str) -> str:
        if not text:
            return ""
        patterns = [
            r"(?:job title|title|position|role|opening|hiring for)\s*[:\-]\s*([^\n•\-]{3,80})",
        ]
        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return match.group(1).strip()
        for line in re.split(r"[\\n•]", text):
            lowered = line.lower()
            if any(k in lowered for k in ["developer", "engineer", "designer", "manager", "analyst", "specialist", "consultant", "assistant", "architect", "lead", "intern"]):
                return line.strip()
        return ""

    def _extract_location_from_text(self, text: str) -> str:
        if not text:
            return ""
        match = re.search(r"(?:location|based in)\s*[:\-]\s*([^\n•\-]{2,80})", text, re.IGNORECASE)
        if match:
            return match.group(1).strip()
        for line in re.split(r"[\\n•]", text):
            if "location" in line.lower():
                return line.split(":", 1)[-1].strip()
        return ""

    def _extract_job_type_from_text(self, text: str) -> str:
        lowered = (text or "").lower()
        if "full-time" in lowered or "full time" in lowered:
            return "Full-time"
        if "part-time" in lowered or "part time" in lowered:
            return "Part-time"
        if "contract" in lowered:
            return "Contract"
        if "intern" in lowered:
            return "Internship"
        return ""

    def _extract_workplace_type_from_text(self, text: str) -> str:
        lowered = (text or "").lower()
        if "remote" in lowered:
            return "Remote"
        if "hybrid" in lowered:
            return "Hybrid"
        if "on-site" in lowered or "onsite" in lowered:
            return "On-site"
        return ""

    def _extract_section_from_text(self, text: str, keywords: List[str]) -> str:
        if not text:
            return ""
        lowered = text.lower()
        for keyword in keywords:
            idx = lowered.find(keyword)
            if idx == -1:
                continue
            tail = text[idx:]
            lines = [line.strip() for line in tail.splitlines() if line.strip()]
            return " ".join(lines[:4])
        return ""

    def _extract_contact_details(self, text: str) -> str:
        if not text:
            return ""
        emails = re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text)
        phones = re.findall(r"(?:\+?\d{1,3}[-\s]?)?(?:\(?\d{2,4}\)?[-\s]?)?\d{3,4}[-\s]?\d{3,4}", text)
        urls = re.findall(r"https?://\S+|www\.\S+", text)
        parts = []
        if emails:
            parts.append(";".join(sorted(set(emails))))
        if urls:
            parts.append(";".join(sorted(set(urls))))
        if phones:
            parts.append(";".join(sorted(set(p.strip() for p in phones if len(p.strip()) >= 7))))
        return ";".join([p for p in parts if p])

    def _extract_salary(self, text: str) -> str:
        if not text:
            return ""
        patterns = [
            r"(?:\$|USD|PKR|INR|EUR|GBP|AED)\s?\d[\d,]*(?:\s?-\s?(?:\$|USD|PKR|INR|EUR|GBP|AED)?\s?\d[\d,]*)?",
            r"\d[\d,]*\s?(?:per year|per annum|per month|per hour)",
        ]
        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return match.group(0).strip()
        return ""

    def _generate_post_content(self, title: str) -> Dict[str, str]:
        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        if api_key:
            payload = {
                "model": "gpt-4o-mini",
                "input": (
                    "Create a motivational LinkedIn post based on the topic. "
                    "Return JSON with keys: title, description. "
                    "Title should be catchy and professional. "
                    "Description should be detailed and motivational (250-350 words) in English. "
                    f"Topic: {title}."
                ),
            }
            try:
                req = urllib.request.Request(
                    "https://api.openai.com/v1/responses",
                    data=json.dumps(payload).encode("utf-8"),
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {api_key}",
                    },
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=30) as resp:
                    data = json.load(resp)
                output = data.get("output_text") or ""
                parsed = self._safe_parse_json(output)
                if parsed:
                    return {
                        "title": parsed.get("title", title).strip() or title,
                        "description": parsed.get("description", "").strip() or title,
                    }
            except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError) as exc:
                self._push_log(f"OpenAI error, using fallback: {exc}")

        return {
            "title": title,
            "description": (
                f"{title}\n\n"
                "Progress is built on consistency, not perfection. "
                "Focus on one meaningful step today, then another tomorrow. "
                "Momentum compounds faster than motivation, and small wins build confidence. "
                "If you feel stuck, simplify your next move and commit to a short, focused sprint. "
                "Keep learning, keep iterating, and keep showing up. "
                "Your future self will thank you for the discipline you practice today."
            ),
        }

    def _generate_post_image(self, title: str) -> Optional[str]:
        output_dir = ensure_output_dir()
        filename = output_dir / f"poster_image_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
        gemini_key = os.environ.get("GEMINI_API_KEY", "").strip()
        if gemini_key:
            prompt = (
                "Create a premium, professional LinkedIn post image. "
                f"Topic: {title}. "
                "Use a clean editorial layout, generous whitespace, and a refined sans-serif title. "
                "Add subtle geometric accents and a soft gradient background. "
                "Keep the design sophisticated and corporate."
            )
            payload = {
                "prompt": {"text": prompt},
                "aspectRatio": "1:1",
            }
            try:
                req = urllib.request.Request(
                    "https://generativelanguage.googleapis.com/v1beta/models/imagen-3.0-generate-002:generateImages"
                    f"?key={gemini_key}",
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=60) as resp:
                    data = json.load(resp)
                image_data = None
                for item in data.get("generatedImages", []):
                    image_info = item.get("image") or {}
                    b64 = image_info.get("imageBytes")
                    if b64:
                        image_data = base64.b64decode(b64)
                        break
                if image_data:
                    with filename.open("wb") as f:
                        f.write(image_data)
                    return str(filename.resolve())
                self._push_log("Gemini image generation error: empty image data.")
            except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, ValueError) as exc:
                self._push_log(f"Gemini image generation error: {exc}")
        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        if api_key:
            prompt = (
                "Create a premium, professional LinkedIn post image. "
                f"Topic: {title}. "
                "Use a clean editorial layout, generous whitespace, and a refined sans-serif title. "
                "Add subtle geometric accents and a soft gradient background. "
                "Keep the design sophisticated and corporate."
            )
            payload = {
                "model": "gpt-image-1",
                "prompt": prompt,
                "size": "1024x1024",
            }
            try:
                req = urllib.request.Request(
                    "https://api.openai.com/v1/images/generations",
                    data=json.dumps(payload).encode("utf-8"),
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {api_key}",
                    },
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=60) as resp:
                    data = json.load(resp)
                image_data = None
                if data.get("data") and isinstance(data["data"], list):
                    item = data["data"][0]
                    if "b64_json" in item:
                        image_data = base64.b64decode(item["b64_json"])
                    elif "url" in item:
                        with urllib.request.urlopen(item["url"], timeout=60) as img_resp:
                            image_data = img_resp.read()
                if image_data:
                    with filename.open("wb") as f:
                        f.write(image_data)
                    return str(filename.resolve())
                self._push_log("Image generation error: empty image data.")
            except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, ValueError) as exc:
                self._push_log(f"Image generation error: {exc}")
        # Local fallback: generate a simple title image
        try:
            width, height = 1200, 1200
            image = Image.new("RGB", (width, height), (245, 248, 252))
            draw = ImageDraw.Draw(image)
            # Soft gradient background
            top = (248, 250, 253)
            bottom = (230, 238, 246)
            for y in range(height):
                ratio = y / (height - 1)
                r = int(top[0] + (bottom[0] - top[0]) * ratio)
                g = int(top[1] + (bottom[1] - top[1]) * ratio)
                b = int(top[2] + (bottom[2] - top[2]) * ratio)
                draw.line([(0, y), (width, y)], fill=(r, g, b))

            # Accent band and geometric shapes
            accent = (18, 82, 120)
            accent_light = (64, 145, 196)
            draw.rectangle([0, 0, width, 140], fill=accent)
            draw.polygon(
                [(0, height - 240), (320, height), (0, height)],
                fill=(218, 230, 240),
            )
            draw.ellipse([width - 420, 180, width - 140, 460], outline=accent_light, width=6)
            draw.rectangle([width - 520, 480, width - 260, 520], fill=accent_light)

            title_text = (title or "LinkedIn Post").strip()
            subtitle = "Insightful. Professional. Ready to share."

            try:
                title_font = ImageFont.truetype("arialbd.ttf", 64)
                subtitle_font = ImageFont.truetype("arial.ttf", 28)
            except OSError:
                title_font = ImageFont.load_default()
                subtitle_font = ImageFont.load_default()

            max_text_width = width - 220
            lines: List[str] = []
            words = title_text.split()
            current: List[str] = []
            for word in words:
                test_line = " ".join(current + [word])
                bbox = draw.textbbox((0, 0), test_line, font=title_font)
                if bbox[2] - bbox[0] <= max_text_width:
                    current.append(word)
                else:
                    if current:
                        lines.append(" ".join(current))
                    current = [word]
            if current:
                lines.append(" ".join(current))

            y = 260
            for line in lines[:5]:
                bbox = draw.textbbox((0, 0), line, font=title_font)
                line_width = bbox[2] - bbox[0]
                x = (width - line_width) // 2
                draw.text((x, y), line, fill=(16, 32, 48), font=title_font)
                y += 78

            bbox = draw.textbbox((0, 0), subtitle, font=subtitle_font)
            sub_x = (width - (bbox[2] - bbox[0])) // 2
            draw.text((sub_x, y + 10), subtitle, fill=(70, 90, 110), font=subtitle_font)

            image.save(filename, "PNG")
            return str(filename.resolve())
        except Exception as exc:
            self._push_log(f"Local image generation error: {exc}")
            return None

    def _generate_post_pdf(self, title: str, text: str, image_path: Optional[str]) -> Optional[str]:
        try:
            output_dir = ensure_output_dir()
            filename = output_dir / f"poster_post_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
            c = canvas.Canvas(str(filename), pagesize=A4)
            width, height = A4
            x = 2 * cm
            y = height - 2.2 * cm
            max_width = width - 4 * cm

            c.setFont("Helvetica-Bold", 18)
            for line in self._wrap_text(title, max_width, c, "Helvetica-Bold", 18):
                c.drawString(x, y, line)
                y -= 0.8 * cm
            y -= 0.4 * cm

            c.setFont("Helvetica", 11)
            for line in self._wrap_text(text, max_width, c, "Helvetica", 11):
                c.drawString(x, y, line)
                y -= 0.6 * cm
                if y < 6 * cm:
                    break

            if image_path and os.path.exists(image_path):
                img_w = width - 4 * cm
                img_h = img_w * 0.6
                img_y = max(2 * cm, y - img_h - 0.8 * cm)
                c.drawImage(image_path, x, img_y, width=img_w, height=img_h, preserveAspectRatio=True, anchor="n")

            c.showPage()
            c.save()
            return str(filename.resolve())
        except Exception as exc:
            self._push_log(f"PDF generation error: {exc}")
            return None

    def _wrap_text(self, text: str, max_width: float, c: canvas.Canvas, font: str, size: int) -> List[str]:
        c.setFont(font, size)
        words = text.split()
        lines: List[str] = []
        current: List[str] = []
        for word in words:
            test_line = " ".join(current + [word])
            if c.stringWidth(test_line, font, size) <= max_width:
                current.append(word)
            else:
                if current:
                    lines.append(" ".join(current))
                current = [word]
        if current:
            lines.append(" ".join(current))
        return lines

    def _safe_parse_json(self, text: str) -> Optional[Dict[str, Any]]:
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", text, re.DOTALL)
            if not match:
                return None
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                return None

    def _hashtags_from_title(self, title: str, max_tags: int = 4) -> str:
        words = re.findall(r"[A-Za-z0-9]+", title)
        tags = []
        for word in words:
            if len(tags) >= max_tags:
                break
            if len(word) < 3:
                continue
            tag = f"#{word.capitalize()}"
            if tag not in tags:
                tags.append(tag)
        if not tags:
            tags = ["#Motivation", "#Growth", "#Mindset"]
        return " ".join(tags)

    def _post_to_linkedin(self, driver: webdriver.Chrome, text: str, image_path: Optional[str]) -> None:
        driver.get("https://www.linkedin.com/feed/")
        time.sleep(2)
        self._push_log("Post: feed opened, searching composer trigger...")
        trigger_selectors = [
            "button.share-box-feed-entry__trigger",
            "button[data-control-name='sharebox_trigger']",
            "button[aria-label*='Start a post']",
            "button[aria-label*='Create a post']",
            "div[role='button'][aria-label*='Start a post']",
            "div[role='button'][aria-label*='Create a post']",
        ]
        trigger = self._first_clickable(driver, trigger_selectors)
        if trigger:
            trigger.click()
            self._push_log("Post: composer trigger clicked.")
        else:
            raise RuntimeError("Cannot find post composer trigger.")
        time.sleep(1.5)
        self._push_log("Post: searching editor...")
        editor = self._first_clickable(
            driver,
            [
                "div.ql-editor",
                "div.share-box__text-editor",
                "div.editor-content",
                "div[role='textbox']",
            ],
        )
        if not editor:
            raise RuntimeError("Cannot find post editor.")
        editor.click()
        editor.send_keys(text)
        time.sleep(0.8)
        self._push_log("Post: content entered.")
        if image_path:
            self._push_log("Post: attaching image...")
            self._attach_image(driver, image_path)
            self._push_log("Post: image attached.")
        self._push_log("Post: searching Post button...")
        post_btn = self._first_clickable(
            driver,
            [
                "button.share-actions__primary-action",
                "button[data-control-name='share_post']",
                "button[aria-label*='Post']",
                "button[aria-label='Post']",
                "div[role='button'][aria-label='Post']",
            ],
        )
        if not post_btn:
            raise RuntimeError("Cannot find Post button.")
        post_btn.click()
        self._push_log("Post: Post button clicked.")
        time.sleep(1.5)

    def _attach_image(self, driver: webdriver.Chrome, image_path: str) -> None:
        buttons = [
            "button[aria-label*='Add a photo']",
            "button[aria-label*='Add media']",
            "button.share-actions__secondary-action",
            "button[aria-label*='Add a photo or video']",
        ]
        btn = self._first_clickable(driver, buttons)
        if btn:
            btn.click()
            time.sleep(0.8)
        file_inputs = driver.find_elements(By.CSS_SELECTOR, "input[type='file']")
        for inp in file_inputs:
            try:
                inp.send_keys(image_path)
                time.sleep(1.2)
                return
            except Exception:
                continue

    def _first_clickable(self, driver: webdriver.Chrome, selectors: List[str]):
        for selector in selectors:
            try:
                el = WebDriverWait(driver, 8).until(
                    EC.element_to_be_clickable((By.CSS_SELECTOR, selector))
                )
                return el
            except Exception:
                continue
        return None

    def _write_csv(self, rows: List[Dict[str, str]]) -> str:
        output_dir = ensure_output_dir()
        filename = output_dir / f"linkedin_posts_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        headers = [
            "Author name",
            "Job Tittle",
            "Location",
            "Job type(full time/ Part-Time)",
            "Remote/Onsite",
            "Job Description",
            "Post Date/Time",
            "required Qualification",
            "Required SKills",
            "Post Link",
            "Contact Destil (email/web-link/Contact number)",
            "salary Pkg",
        ]
        with filename.open("w", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow(headers)
            for row in rows:
                writer.writerow([clean_csv_field(row.get(h, "")) for h in headers])
        return str(filename.resolve())


# ---------- Application entry ----------
def main() -> None:
    load_env()
    api = ScraperAPI()
    index_path = INDEX_HTML.resolve()
    if not index_path.exists():
        raise FileNotFoundError(f"Cannot find UI at {index_path}")

    webview.create_window("LinkedIn Scraper", url=index_path.as_uri(), js_api=api, width=1040, height=780)
    webview.start(http_server=True, gui="edgechromium", debug=False)


if __name__ == "__main__":
    main()
