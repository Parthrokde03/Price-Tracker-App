import os
import re
from datetime import datetime
from urllib.parse import urlparse

import pytz
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from flask import Flask, render_template, request
from flask_sqlalchemy import SQLAlchemy

load_dotenv()

DEFAULT_DB_URI = "sqlite:///prices.db"
SCRAPER_API_URL = os.getenv("SCRAPER_API_URL", "https://api.scraperapi.com")
SCRAPER_API_KEY = os.getenv("SCRAPER_API_KEY")
SUPPORTED_DOMAINS = ("flipkart.com", "amazon.", "amzn.")
PLACEHOLDER_IMAGE = "https://via.placeholder.com/300?text=Image+Not+Found"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)
DEFAULT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Language": "en-US,en;q=0.9",
}
MAX_HISTORY_POINTS = 7

app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL", DEFAULT_DB_URI)
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)
os.makedirs("instance", exist_ok=True)


class PriceHistory(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    url = db.Column(db.String(300), nullable=False)
    title = db.Column(db.String(300))
    price = db.Column(db.Float, nullable=False)
    date = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    def __repr__(self) -> str:
        return f"<PriceHistory {self.url} @ {self.date.isoformat()}>"


def parse_price(price_text: str) -> float:
    cleaned = re.sub(r"[^\d.]", "", price_text.replace(",", ""))
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def extract_flipkart_details(soup: BeautifulSoup) -> tuple[str, str, str]:
    title_tag = soup.find("meta", {"property": "og:title"}) or soup.select_one("span.B_NuCI")
    price_tag = (
        soup.select_one("div._30jeq3._16Jk6d")
        or soup.select_one("div.Nx9bqj.CxhGGd")
        or soup.select_one("div._25b18c ._30jeq3")
    )
    image_tag = soup.find("meta", {"property": "og:image"}) or soup.select_one("img._396cs4._2amPTt._3qGmMb")

    title = title_tag["content"].strip() if title_tag and title_tag.has_attr("content") else (
        title_tag.get_text(strip=True) if title_tag else "Not found"
    )
    price = price_tag.get_text(strip=True) if price_tag else "0"
    image_url = (
        image_tag["content"]
        if image_tag and image_tag.has_attr("content")
        else image_tag["src"]
        if image_tag and image_tag.has_attr("src")
        else PLACEHOLDER_IMAGE
    )
    return title, price, image_url


def extract_amazon_details(soup: BeautifulSoup) -> tuple[str, str, str]:
    title_tag = soup.select_one("#productTitle") or soup.select_one("span#title")
    price_tag = (
        soup.select_one("#corePriceDisplay_desktop_feature_div span.a-offscreen")
        or soup.select_one(".a-price .a-offscreen")
        or soup.select_one("span.a-price-whole")
    )
    image_tag = soup.select_one("#landingImage") or soup.find("meta", {"property": "og:image"})

    title = title_tag.get_text(strip=True) if title_tag else "Not found"
    price = price_tag.get_text(strip=True) if price_tag else "0"
    image_url = (
        image_tag["src"]
        if image_tag and image_tag.has_attr("src")
        else image_tag["content"]
        if image_tag and image_tag.has_attr("content")
        else PLACEHOLDER_IMAGE
    )
    return title, price, image_url


def extract_product_details(product_url: str, soup: BeautifulSoup) -> tuple[str, str, str]:
    hostname = urlparse(product_url).hostname or ""
    if "flipkart.com" in hostname:
        return extract_flipkart_details(soup)
    if "amazon." in hostname or "amzn." in hostname:
        return extract_amazon_details(soup)
    return "Unsupported website", "0", PLACEHOLDER_IMAGE


def fetch_product_page(product_url: str) -> BeautifulSoup:
    if SCRAPER_API_KEY:
        params = {"api_key": SCRAPER_API_KEY, "url": product_url, "render": "true"}
        response = requests.get(SCRAPER_API_URL, params=params, timeout=25)
    else:
        response = requests.get(product_url, headers=DEFAULT_HEADERS, timeout=25)
    response.raise_for_status()
    return BeautifulSoup(response.content, "html.parser")


def build_history(product_url: str):
    history = (
        PriceHistory.query.filter_by(url=product_url)
        .order_by(PriceHistory.date.desc())
        .limit(MAX_HISTORY_POINTS)
        .all()
    )
    history.reverse()  # Chart.js expects chronological order

    from_zone = pytz.utc
    to_zone = pytz.timezone("Asia/Kolkata")

    date_labels = []
    price_data = []
    for item in history:
        ist_time = item.date.replace(tzinfo=from_zone).astimezone(to_zone)
        date_labels.append(ist_time.strftime("%Y-%m-%d %H:%M"))
        price_data.append(item.price)

    return history, date_labels, price_data


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/track", methods=["POST"])
def track():
    product_url = request.form.get("url", "").strip()
    if not product_url:
        return render_template("index.html", error="Please provide a product URL.")

    hostname = urlparse(product_url).hostname or ""
    if not any(domain in hostname for domain in SUPPORTED_DOMAINS):
        return render_template(
            "index.html",
            error="Currently we can track Amazon or Flipkart product links.",
        )

    try:
        soup = fetch_product_page(product_url)
    except requests.RequestException as exc:
        return render_template(
            "index.html",
            error=f"Could not fetch the product page. {exc}",
        )

    title_text, price_text, image_url = extract_product_details(product_url, soup)

    if title_text == "Unsupported website":
        return render_template(
            "index.html",
            error="Currently we can track Amazon or Flipkart product links.",
        )

    price_value = parse_price(price_text)
    if price_value > 0 and title_text != "Unsupported website":
        db.session.add(PriceHistory(url=product_url, title=title_text, price=price_value))
        db.session.commit()

    history, date_labels, price_data = build_history(product_url)

    display_price = price_text if "₹" in price_text else f"₹{price_text}"

    return render_template(
        "result.html",
        title=title_text,
        price=display_price,
        image_url=image_url,
        history=history,
        date_labels=date_labels,
        price_data=price_data,
        url=product_url,
    )


if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=bool(os.getenv("FLASK_DEBUG")))
