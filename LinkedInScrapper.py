import csv
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
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
            post_text = self._normalize_post_text(content)
            if self._poster_driver:
                self._push_log("Posting to LinkedIn feed...")
                self._post_to_linkedin(self._poster_driver, post_text, None)
                self._push_log("Post submitted to LinkedIn.")
            else:
                self._push_log("Poster driver not available; skipping LinkedIn post.")
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
                    "Title should be optimized, catchy, and professional (max 8 words). "
                    "Description should be detailed and motivational (around 250-350 words) in English, "
                    "include plenty of tasteful emojis throughout, and end with SEO-friendly hashtags "
                    "(exactly 10, space-separated). "
                    "Return JSON only, no extra text. "
                    f"Topic: {title}."
                ),
                "max_output_tokens": 3000,
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
                output = self._extract_openai_text(data)
                parsed = self._safe_parse_json(output)
                if parsed:
                    return {
                        "title": parsed.get("title", title).strip() or title,
                        "description": parsed.get("description", "").strip() or title,
                    }
                self._push_log("OpenAI response missing JSON payload; using fallback.")
            except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError) as exc:
                self._push_log(f"OpenAI error, using fallback: {exc}")

        return {
            "title": title,
            "description": self._generate_fallback_description(title),
        }

    def _extract_openai_text(self, data: Dict[str, Any]) -> str:
        if not data:
            return ""
        output_text = data.get("output_text")
        if isinstance(output_text, str) and output_text.strip():
            return output_text.strip()
        chunks: List[str] = []
        for item in data.get("output", []) or []:
            for content in item.get("content", []) or []:
                text = content.get("text")
                if isinstance(text, str) and text.strip():
                    chunks.append(text.strip())
        return "\n".join(chunks).strip()

    def _normalize_post_text(self, content: Dict[str, str]) -> str:
        title = (content.get("title") or "").strip()
        description = (content.get("description") or "").strip()
        combined = f"{title}\n\n{description}" if title else description
        combined = re.sub(r"\r\n", "\n", combined)
        combined = re.sub(r"\n{3,}", "\n\n", combined)
        combined = "\n\n".join([re.sub(r"\s+", " ", part).strip() for part in combined.split("\n\n")])
        return self._enforce_char_limit(combined.strip(), max_chars=2800)

    def _generate_post_pdf(self, title: str, text: str, image_path: Optional[str]) -> Optional[str]:
        try:
            output_dir = ensure_output_dir()
            filename = output_dir / f"poster_post_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
            c = canvas.Canvas(str(filename), pagesize=A4)
            width, height = A4
            x = 2 * cm
            y = height - 2.2 * cm
            max_width = width - 4 * cm
            normalized_text = re.sub(r"\s+", " ", text or "").strip()

            c.setFont("Helvetica-Bold", 18)
            for line in self._wrap_text(title, max_width, c, "Helvetica-Bold", 18):
                c.drawString(x, y, line)
                y -= 0.8 * cm
            y -= 0.4 * cm

            c.setFont("Helvetica", 11)
            for line in self._wrap_text(normalized_text, max_width, c, "Helvetica", 11):
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

    def _hashtags_from_title(self, title: str, max_tags: int = 10) -> str:
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

    def _generate_fallback_description(self, topic: str, target_words: int = 320) -> str:
        base_topic = topic.strip() or "Professional Growth"
        emoji_cycle = ["🚀", "✨", "🔥", "✅", "🎯", "💡", "📈", "🧠", "🤝", "🌟", "💪", "🗣️", "📌", "🏆", "🛠️"]
        sections = [
            f"{base_topic} is more than a topic; it is a mindset you can practice daily. 🌟",
            "Start with clarity. Define what success looks like, then break it into small, visible steps. 🎯",
            "Consistency beats intensity. A small win each day builds real momentum. 💪",
            "Focus on outcomes, not just activity. Track progress, reflect, and iterate. 📌",
            "People remember value. Share insights, document lessons, and help others win. 🤝",
            "Quality work compounds. When you improve 1% every day, you build a powerful edge. 📈",
            "Use feedback as data. Adjust your approach without losing your core direction. 🧭",
            "Build simple systems: a weekly plan, a daily checklist, and a review ritual. 🛠️",
            "Protect your energy. Deep work and clear priorities create better results. 🧠",
            "Celebrate progress, not perfection. Growth is a long game, and you are on it. 🏆",
        ]
        connectors = [
            "Here is the part most people miss:",
            "A practical step you can apply today:",
            "If you feel stuck, try this:",
            "Remember this simple rule:",
            "The real breakthrough is usually simple:",
        ]
        paragraph_templates = [
            "{topic} works best when you combine strategy with execution. {connector} "
            "write down your top three actions for the week, schedule them, and protect those slots. "
            "This creates a clear path from intention to impact. {emoji}",
            "Think about {topic} as a system, not a single task. {connector} focus on repeatable habits "
            "like learning, shipping, and sharing. Those habits turn effort into results over time. {emoji}",
            "In {topic}, attention to detail creates trust. {connector} slow down for quality, "
            "then speed up with templates and checklists. That balance keeps quality high and stress low. {emoji}",
            "{topic} also benefits from community. {connector} find peers, ask questions, "
            "and contribute ideas. Collaboration shortens the learning curve and keeps you motivated. {emoji}",
            "If you want to level up in {topic}, measure progress. {connector} review what worked, "
            "what did not, and what you will change next week. Small adjustments add up fast. {emoji}",
        ]
        paragraphs: List[str] = []
        paragraphs.append(f"{base_topic}\n")
        paragraphs.append("Progress is built on consistency, not perfection. 🌟")
        paragraphs.extend(sections)

        idx = 0
        while len(" ".join(paragraphs).split()) < target_words:
            template = paragraph_templates[idx % len(paragraph_templates)]
            connector = connectors[idx % len(connectors)]
            emoji = emoji_cycle[idx % len(emoji_cycle)]
            paragraphs.append(template.format(topic=base_topic, connector=connector, emoji=emoji))
            idx += 1

        text = "\n\n".join(paragraphs).strip()
        hashtags = self._hashtags_from_title(base_topic, max_tags=10)
        # Expand to SEO-friendly tags when topic is short.
        if len(hashtags.split()) < 10:
            extra = [
                "#Productivity",
                "#Leadership",
                "#Career",
                "#PersonalBrand",
                "#Strategy",
                "#Learning",
                "#GrowthMindset",
                "#Innovation",
            ]
            for tag in extra:
                if tag not in hashtags.split():
                    hashtags += f" {tag}"
                if len(hashtags.split()) >= 10:
                    break
        return f"{text}\n\n{hashtags}"

    def _enforce_char_limit(self, text: str, max_chars: int = 2800) -> str:
        if len(text) <= max_chars:
            return text
        hashtags = ""
        parts = text.split("\n\n")
        if parts:
            last = parts[-1].strip()
            if last.startswith("#"):
                hashtags = last
                parts = parts[:-1]
        base = "\n\n".join(parts).strip()
        if not hashtags:
            return (base[: max_chars - 1] + "…").strip()
        reserve = len(hashtags) + 2
        allowed = max_chars - reserve - 1
        trimmed = base[:allowed].rstrip()
        return f"{trimmed}…\n\n{hashtags}"

    def _post_to_linkedin(self, driver: webdriver.Chrome, text: str, image_path: Optional[str]) -> None:
        driver.get("https://www.linkedin.com/feed/")
        time.sleep(2)
        self._push_log("Post: clicking target element after login...")
        target = WebDriverWait(driver, 12).until(
            EC.element_to_be_clickable((By.XPATH, "//*[@id='ember34']"))
        )
        target.click()
        self._push_log("Post: target element clicked, waiting for composer...")
        editor = WebDriverWait(driver, 12).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "div[role='textbox'], div.ql-editor, div[contenteditable='true']"))
        )
        self._set_editor_text(driver, editor, text)
        self._push_log("Post: content entered.")
        time.sleep(1.2)
        self._push_log("Post: clicking Post button...")
        post_button = WebDriverWait(driver, 12).until(
            EC.element_to_be_clickable((By.XPATH, "//*[@id='ember247']"))
        )
        post_button.click()
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

    def _set_editor_text(self, driver: webdriver.Chrome, editor, text: str) -> None:
        driver.execute_script(
            "arguments[0].focus(); arguments[0].textContent = arguments[1]; arguments[0].dispatchEvent(new Event('input', {bubbles: true}));",
            editor,
            text,
        )
        time.sleep(0.4)
        # Fallback: send keys if JS injection fails to register.
        try:
            if editor.text.strip() == "":
                editor.click()
                editor.send_keys(text)
        except Exception:
            pass

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
