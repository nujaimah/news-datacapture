import asyncio
import os
from datetime import datetime
import re
import json
from playwright.async_api import async_playwright
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
import pickle

SCOPES = ['https://www.googleapis.com/auth/spreadsheets',
          'https://www.googleapis.com/auth/drive']
CLIENT_SECRETS_FILE = 'credentials.json'
TOKEN_PICKLE = 'token.json'
SPREADSHEET_ID = "NAME" # Enter Google Sheet ID
SHEET_NAME = "NAME" # Enter Google Sheet Tab name
LAPRESSE_CAPTURE_FOLDER_ID = "NAME" # Enter Google Drive folder ID
LA_PRESSE_HOMEPAGE = "https://www.lapresse.ca/"
ARTICLE_PATTERN = re.compile(
    r"^https?://www\.lapresse\.ca/.+/\d{4}-\d{2}-\d{2}/.+\.php$"
)
EXCLUDED_ARTICLE_URLS = {
    "https://www.lapresse.ca/renseignements/2023-08-02/"
    "fin-de-l-acces-aux-nouvelles-sur-facebook-instagram-et-google/"
    "comment-continuer-de-vous-informer-efficacement-et-gratuitement.php"
}

def create_dated_capture_folder(drive_service):
    date_str = datetime.now().strftime("%Y-%m-%d")
    folder_metadata = {
        'name': date_str + " Capture",
        'mimeType': 'application/vnd.google-apps.folder',
        'parents': [LAPRESSE_CAPTURE_FOLDER_ID],
    }
    folder = drive_service.files().create(
        body=folder_metadata,
        fields='id'
    ).execute()
    folder_id = folder['id']
    print(f"Created capture folder for {date_str} with ID {folder_id}")
    return folder_id

def authenticate_google_services():
    creds = None
    if os.path.exists('token.pickle'):
        with open('token.pickle', 'rb') as token:
            creds = pickle.load(token)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRETS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open('token.pickle', 'wb') as token:
            pickle.dump(creds, token)
    drive_service = build('drive', 'v3', credentials=creds)
    sheets_service = build('sheets', 'v4', credentials=creds)
    return drive_service, sheets_service

async def scroll_to_bottom(page, scroll_delay=1000, max_scrolls=20):
    previous_height = await page.evaluate("document.body.scrollHeight")
    scrolls = 0
    while scrolls < max_scrolls:
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(scroll_delay)
        new_height = await page.evaluate("document.body.scrollHeight")
        if new_height == previous_height:
            break
        previous_height = new_height
        scrolls += 1

async def extract_article_links(page):
    elems = await page.query_selector_all("a")
    seen_urls = set()
    urls = []
    for elem in elems:
        href = await elem.get_attribute('href')
        if href:
            if not href.startswith("http"):
                href = "https://www.lapresse.ca" + href

            if href in EXCLUDED_ARTICLE_URLS:
                continue

            if ARTICLE_PATTERN.match(href) and href not in seen_urls:
                seen_urls.add(href)
                urls.append(href)
    return urls

async def check_ai_mention(page):
    keywords = ["ChatGPT", "automated", "robot", "AI tools", "data team", "OpenAI", "Otter.ai",
                "AI-Based", "artificial intelligence", "machine learning", "AI model",
                "AI technology", "AI-generated", "AI-assisted"]
    try:
        article_element = await page.query_selector("article")
        if not article_element:
            return "False"
        paragraphs = await article_element.query_selector_all("p")
        text_content = " ".join([await p.inner_text() for p in paragraphs])
    except Exception:
        return "False"
    text_lower = text_content.lower()
    for kw in keywords:
        if kw.lower() in text_lower:
            return f"True - {kw}"
    return "False"

async def extract_author_contacts(context, article_page):
    contacts = set()
    profile_url = None

    try:
        profile_url = await article_page.eval_on_selector(
            'div.authorModule meta[itemprop="url"]',
            'el => el.getAttribute("content")'
        )
    except Exception:
        profile_url = None

    if not profile_url:
        try:
            href = await article_page.eval_on_selector(
                'div.authorModule a[href^="/auteurs/"]',
                'el => el.getAttribute("href")'
            )
            if href:
                profile_url = href if href.startswith("http") else "https://www.lapresse.ca" + href
        except Exception:
            profile_url = None

    async def scan_page_for_contacts(page):
        local_contacts = set()
        try:
            links = await page.query_selector_all("a[href]")
        except Exception:
            return local_contacts

        for a in links:
            href = await a.get_attribute("href")
            if not href:
                continue

            if "intent/tweet" in href:
                continue

            if href.startswith("mailto:") and "@" in href:
                local_contacts.add(href)
                continue

            m_tw = re.match(r"^https?://(twitter|x)\.com/([^/?#]+)$", href)
            if m_tw:
                handle = m_tw.group(2).lower()
                if handle in {"lp_lapresse"}:
                    continue
                local_contacts.add(href)
                continue

            if re.match(r"^https?://(www\.)?linkedin\.com/in/[^/?#]+", href):
                local_contacts.add(href)
                continue

        return local_contacts

    contacts |= await scan_page_for_contacts(article_page)

    if profile_url:
        p = await context.new_page()
        try:
            await p.goto(profile_url, wait_until="domcontentloaded", timeout=60000)
            await p.wait_for_timeout(1000)
            contacts |= await scan_page_for_contacts(p)
        except Exception:
            pass
        finally:
            await p.close()

    return "\n".join(sorted(contacts))

async def extract_article_data(page):
    title = "No title"
    try:
        title = await page.eval_on_selector(
            'h1.headlines.titleModule span.title',
            'el => el.textContent.trim()'
        )
    except Exception:
        pass

    author_name = None
    try:
        author_names = await page.eval_on_selector_all(
            'div.authorModule__details span.authorModule__name',
            'els => els.map(el => el.textContent.trim())'
        )
        if author_names:
            author_name = ", ".join(author_names)
        else:
            affiliation_fallback = await page.eval_on_selector(
                'div.authorModule__details span.authorModule__affiliation',
                'el => el.textContent.trim()'
            )
            if affiliation_fallback:
                author_name = affiliation_fallback
    except Exception:
        try:
            affiliation_fallback = await page.eval_on_selector(
                'div.authorModule__details span.authorModule__affiliation',
                'el => el.textContent.trim()'
            )
            if affiliation_fallback:
                author_name = affiliation_fallback
        except Exception:
            author_name = None

    if not author_name or author_name.lower() in ["", "unknown"]:
        try:
            org_name = await page.eval_on_selector(
                'span.organization.authorModule__organisation[itemprop="affiliation"]',
                'el => el.textContent.trim()'
            )
            if org_name:
                author_name = org_name
        except Exception:
            pass

    affiliation = "La Presse"

    additional_affiliation = ""
    try:
        credit_texts = await page.eval_on_selector_all(
            'p.credit.photoModule__caption.photoModule__caption--credit',
            'els => els.map(el => el.textContent.trim()).filter(t => t)'
        )
        if credit_texts:
            additional_affiliation = "\n".join(credit_texts)
    except Exception:
        additional_affiliation = ""

    date_published = None
    date_updated = None
    try:
        date_published = await page.eval_on_selector(
            'time[itemprop="datePublished"]',
            'el => el.getAttribute("datetime")'
        )
    except Exception:
        pass
    try:
        date_updated = await page.eval_on_selector(
            'time[itemprop="dateModified"]',
            'el => el.getAttribute("datetime")'
        )
    except Exception:
        pass

    if date_published and date_updated and date_updated != date_published:
        date_posted = f"{date_published} (Updated: {date_updated})"
    elif date_published:
        date_posted = date_published
    elif date_updated:
        date_posted = f"Updated: {date_updated}"
    else:
        date_posted = "No date found"

    media_urls = set()

    media_elements = await page.query_selector_all("video, audio")
    for media in media_elements:
        src = await media.get_attribute('src')
        if src:
            media_urls.add(src)
        source_elems = await media.query_selector_all("source")
        for source in source_elems:
            src = await source.get_attribute('src')
            if src:
                media_urls.add(src)

    video_elems = await page.query_selector_all("video[data-video-encodings]")
    for v in video_elems:
        enc = await v.get_attribute("data-video-encodings")
        if not enc:
            continue
        try:
            data = json.loads(enc)
            hls = data.get("application/x-mpegURL") or {}
            if isinstance(hls, dict):
                src = hls.get("src")
                if src:
                    media_urls.add(src)
        except Exception:
            continue

    audio_divs = await page.query_selector_all("div[data-audio-url]")
    for div in audio_divs:
        aurl = await div.get_attribute("data-audio-url")
        if aurl:
            media_urls.add(aurl)

    audio_tags = await page.query_selector_all("audio[data-audio-url]")
    for a in audio_tags:
        aurl = await a.get_attribute("data-audio-url")
        if aurl:
            media_urls.add(aurl)

    if not author_name or author_name.strip() == "":
        author_name = "Unknown"

    return {
        "title": title or "No title",
        "author": author_name,
        "affiliation": affiliation,
        "additional_affiliation": additional_affiliation,
        "date_posted": date_posted,
        "media_urls": sorted(media_urls),
    }

async def save_pdf_and_upload(page, url, drive_service, folder_id, prefix="lapresse"):
    await page.goto(url, wait_until='domcontentloaded', timeout=90000)
    await page.wait_for_timeout(2000)
    article_data = await extract_article_data(page)
    ai_mention = await check_ai_mention(page)
    date_str = datetime.now().strftime("%Y-%m-%d")
    safe_title = "".join(
        c for c in article_data["title"] if c.isalnum() or c in (" ", "-", "_")
    ).rstrip()
    safe_title = safe_title.replace(" ", "_")[:60]
    pdf_filename = f"{prefix}_story_{safe_title}_{date_str}.pdf"
    await page.pdf(
        path=pdf_filename,
        format='A4',
        print_background=True,
        margin={"top": "10mm", "bottom": "10mm", "left": "10mm", "right": "10mm"}
    )
    file_metadata = {'name': pdf_filename, 'parents': [folder_id]}
    media = MediaFileUpload(pdf_filename, mimetype='application/pdf')
    uploaded_file = drive_service.files().create(
        body=file_metadata, media_body=media, fields='id'
    ).execute()
    article_data["ai_mention"] = ai_mention
    return article_data, pdf_filename, uploaded_file.get('id')

async def append_to_sheet(sheets_service, data_row):
    loop = asyncio.get_running_loop()

    def append_sync():
        sheet_range = f"{SHEET_NAME}!A1"
        body = {'values': [data_row]}
        result = sheets_service.spreadsheets().values().append(
            spreadsheetId=SPREADSHEET_ID,
            range=sheet_range,
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body=body
        ).execute()
        return result

    result = await loop.run_in_executor(None, append_sync)
    return result

async def ensure_header_row(sheets_service):
    loop = asyncio.get_running_loop()

    def update_sync():
        header = [[
            "Title",
            "Author",
            "Social/Email",
            "Link",
            "Date Posted/Last Updated",
            "Additional Affiliations",
            "Video/Audio Links",
            "AI Mention?"
        ]]
        return sheets_service.spreadsheets().values().update(
            spreadsheetId=SPREADSHEET_ID,
            range=f"{SHEET_NAME}!A1:H1",
            valueInputOption="RAW",
            body={'values': header}
        ).execute()

    await loop.run_in_executor(None, update_sync)

async def main():
    drive_service, sheets_service = authenticate_google_services()
    await ensure_header_row(sheets_service)

    capture_folder_id = create_dated_capture_folder(drive_service)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        await page.goto(LA_PRESSE_HOMEPAGE, wait_until="domcontentloaded", timeout=90000)
        await page.wait_for_timeout(5000)

        await scroll_to_bottom(page, scroll_delay=1000, max_scrolls=30)

        homepage_pdf = f"lapresse_homepage_{datetime.now().strftime('%Y-%m-%d')}.pdf"
        await page.pdf(
            path=homepage_pdf,
            format='A4',
            print_background=True,
            margin={"top": "10mm", "bottom": "10mm", "left": "10mm", "right": "10mm"}
        )
        file_metadata = {'name': homepage_pdf, 'parents': [capture_folder_id]}
        media = MediaFileUpload(homepage_pdf, mimetype='application/pdf')
        drive_service.files().create(
            body=file_metadata, media_body=media, fields='id'
        ).execute()
        print(f"Uploaded homepage PDF: {homepage_pdf}")

        article_urls = await extract_article_links(page)
        print(f"Found {len(article_urls)} article URLs on homepage after scrolling.")

        processed_urls = set()
        for url in article_urls:
            if url in processed_urls:
                print(f"Skipping duplicate article URL: {url}")
                continue
            print(f"Processing article {url}")
            article_page = await context.new_page()
            try:
                article_data, pdf_filename, file_id = await save_pdf_and_upload(
                    article_page, url, drive_service, capture_folder_id
                )
                social_email = await extract_author_contacts(context, article_page)
                media_links_str = "\n".join(article_data["media_urls"]) if article_data["media_urls"] else ""
                sheet_row = [
                    article_data["title"],
                    article_data["author"],
                    social_email,
                    url,
                    article_data["date_posted"],
                    article_data.get("additional_affiliation", ""),
                    media_links_str,
                    article_data.get("ai_mention", "False"),
                ]
                append_result = await append_to_sheet(sheets_service, sheet_row)
                print(f"Appended row to sheet for article: {article_data['title']}")
                processed_urls.add(url)
            except Exception as e:
                print(f"Error processing {url}: {e}")
            finally:
                await article_page.close()

        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
