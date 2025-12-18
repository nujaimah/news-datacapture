import asyncio
from datetime import datetime
import re
import os
import json
from playwright.async_api import async_playwright, TimeoutError
from googleapiclient.discovery import build as gsheet_build
from googleapiclient.http import MediaFileUpload
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
import pickle

SCOPES = ['https://www.googleapis.com/auth/spreadsheets',
          'https://www.googleapis.com/auth/drive']
CLIENT_SECRETS_FILE = 'credentials.json' # Credentials file for Google Drive access
TOKEN_PICKLE = 'token.json' # Token file for Google Drive access

SPREADSHEET_ID = 'NAME' # Enter Google Sheet ID 
SHEET_NAME = "NAME" # Enter Google Sheet tab name
CBC_CAPTURE_FOLDER_ID = 'NAME' # Enter Google Drive folder ID

# Getting Google Credentials for accessing Google Drive
def get_oauth_credentials():
    creds = None
    if os.path.exists(TOKEN_PICKLE):
        with open(TOKEN_PICKLE, 'rb') as token:
            creds = pickle.load(token)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRETS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_PICKLE, 'wb') as token:
            pickle.dump(creds, token)
    return creds

# Create a new folder in Google Drive with the current date 
def create_dated_capture_folder(drive_service):
    date_str = datetime.now().strftime("%Y-%m-%d")
    folder_metadata = {
        'name': date_str + " Capture",
        'mimeType': 'application/vnd.google-apps.folder',
        'parents': [CBC_CAPTURE_FOLDER_ID],
    }
    folder = drive_service.files().create(
        body=folder_metadata,
        fields='id'
    ).execute()
    folder_id = folder['id']
    print(f"Created capture folder for {date_str} with ID {folder_id}")
    return folder_id

# Extract all article links from the homepage 
async def extract_relevant_article_links(page):
    await page.wait_for_selector("a")
    link_elements = await page.query_selector_all("a")

    exclude_urls = {
        "https://www.cbc.ca/news/about-cbc-news-1.1294364",
        "https://www.cbc.ca/news/corrections-clarifications-1.5893564",
        "https://www.cbc.ca/news/public-appearances-1.4969965",
        "https://www.cbc.ca/accessibility/accessibility-feedback-1.5131151"
    }

    article_urls = []
    pattern = re.compile(
        r"https?://www\.cbc\.ca/.+(-\d+(?:\.\d+)?$|/post/)"
    )

    for elem in link_elements:
        href = await elem.get_attribute('href')
        if href:
            full_url = href if href.startswith("http") else f"https://www.cbc.ca{href}"
            if pattern.match(full_url) and full_url not in exclude_urls:
                if full_url not in article_urls:
                    article_urls.append(full_url)

    print(f"Extracted {len(article_urls)} relevant article URLs (including kidsnews posts)")
    return article_urls

async def trigger_player_links(page):
    try:
        await page.evaluate("window.scrollTo(0, 0)")
        await page.wait_for_timeout(500)
    except Exception:
        pass

    # 1) TTS (audio) buttons
    try:
        tts_buttons = await page.query_selector_all(
            "button.ttsPlayPauseButton-b4Yle, .ttsPlayIcon"
        )
        print(f"Found {len(tts_buttons)} TTS buttons on article page")
        for idx, btn in enumerate(tts_buttons):
            try:
                await btn.scroll_into_view_if_needed()
                await page.wait_for_timeout(300)
                # Click twice with short delays
                await btn.click(timeout=2000)
                await page.wait_for_timeout(800)
                await btn.click(timeout=2000)
                await page.wait_for_timeout(1200)
                print(f"Clicked TTS button {idx+1}/{len(tts_buttons)} twice")
            except Exception:
                continue
    except Exception:
        pass

    # 2) Video play buttons (only those currently in DOM/viewport)
    try:
        video_buttons = []
        for sel in ["div.play-button-container", "svg.videoItemPlayBtn"]:
            elems = await page.query_selector_all(sel)
            video_buttons.extend(elems)
        print(f"Found {len(video_buttons)} video play controls on article page")
        for idx, el in enumerate(video_buttons):
            try:
                await el.scroll_into_view_if_needed()
                await page.wait_for_timeout(300)
                await el.click(timeout=2000)
                await page.wait_for_timeout(1000)
                print(f"Clicked video control {idx+1}/{len(video_buttons)}")
            except Exception:
                continue
    except Exception:
        pass

    # Short final wait
    await page.wait_for_timeout(1500)


async def extract_cbc_article_info(page):
    video_audio_links = set()

    # phoenix-player video src
    try:
        phoenix_srcs = await page.eval_on_selector_all(
            "phoenix-player[src^='https://www.cbc.ca/player/play/video/']",
            "nodes => nodes.map(n => n.getAttribute('src'))"
        )
        for src in phoenix_srcs or []:
            if src:
                video_audio_links.add(src)
    except Exception:
        pass

    # phx-info-title anchors
    try:
        video_hrefs = await page.eval_on_selector_all(
            "span.phx-info-title a[href^='https://www.cbc.ca/player/play/video/']",
            "nodes => nodes.map(n => n.href)"
        )
        for href in video_hrefs or []:
            video_audio_links.add(href)
    except Exception:
        pass

    # audio mp3 srcs (TTS)
    try:
        audio_srcs = await page.eval_on_selector_all(
            "audio[src]",
            "nodes => nodes.map(n => n.src)"
        )
        for src in audio_srcs or []:
            if src.endswith(".mp3"):
                video_audio_links.add(src)
    except Exception:
        pass

    # window.__INITIAL_STATE__ player URLs + authors_info
    initial_state_json = await page.evaluate("""() => {
        const scripts = Array.from(document.querySelectorAll('script'));
        for (const script of scripts) {
            if (script.textContent && script.textContent.includes('window.__INITIAL_STATE__')) {
                const content = script.textContent;
                const match = content.match(/window\\.__INITIAL_STATE__\\s?=\\s?(\\{.*\\});?/s);
                if (match) return match[1];
            }
        }
        return null;
    }""")

    authors_info = None
    if initial_state_json:
        try:
            state = json.loads(initial_state_json)
        except json.JSONDecodeError:
            state = None

        if state:
            try:
                detail = state.get("detail", {})
                detail_content = detail.get("content", {})
                for key, val in detail_content.items():
                    if isinstance(val, str) and "https://www.cbc.ca/player/play/" in val:
                        video_audio_links.add(val)
                detail_str = json.dumps(detail)
                for match in re.findall(r'https://www\.cbc\.ca/player/play/[0-9\.]+', detail_str):
                    video_audio_links.add(match)
            except Exception:
                pass

            try:
                state_str = json.dumps(state)
                for match in re.findall(r'https://www\.cbc\.ca/player/play/[0-9\.]+', state_str):
                    video_audio_links.add(match)
            except Exception:
                pass

            try:
                authors = state.get('author', {})
                author_names = []
                if isinstance(authors, dict) and 'name' in authors:
                    author_names.append(authors['name'])
                elif isinstance(authors, list):
                    for auth in authors:
                        if isinstance(auth, dict) and 'name' in auth:
                            author_names.append(auth['name'])
                detail_content = state.get('detail', {}).get('content', {})
                source_info = detail_content.get('source', '')
                if source_info:
                    author_names.append(source_info)
                authors_info = ", ".join(author_names) if author_names else None
            except Exception:
                pass

    return sorted(video_audio_links), authors_info

async def check_ai_mention(page):
    keywords = ["ChatGPT", "automated", "robot", "AI tools", "data team", "OpenAI", "Otter.ai",
                "AI-Based", "artificial intelligence", "machine learning", "AI model",
                "AI technology", "AI-generated", "AI-assisted"]
    try:
        texts = []

        article_element = await page.query_selector("article")
        if article_element:
            paragraphs = await article_element.query_selector_all("p")
            article_text = " ".join([await p.inner_text() for p in paragraphs])
            texts.append(article_text)

        toggletip_elements = await page.query_selector_all("div.toggletipInfoText-Us8br")
        for el in toggletip_elements:
            txt = await el.inner_text()
            if txt:
                texts.append(txt)

        if not texts:
            return "False"

        full_text_lower = " ".join(texts).lower()
        for kw in keywords:
            if kw.lower() in full_text_lower:
                return f"True - {kw}"
        return "False"
    except Exception:
        return "False"

async def extract_author_info(page):
    try:
        tokens = set()

        bio_element = await page.query_selector("p.authorprofile-biography")
        bio_text = ""
        if bio_element:
            bio_text = await bio_element.inner_text()
        else:
            article_element = await page.query_selector("article")
            if article_element:
                bio_text = await article_element.inner_text()

        if bio_text:
            for part in bio_text.split():
                cleaned = part.strip(",.()[]")
                if "@" in cleaned:
                    tokens.add(cleaned)

        social_texts = await page.eval_on_selector_all(
            "ul.authorprofile-links li.authorprofile-linkitem a.authorprofile-item",
            "nodes => nodes.map(n => n.innerText)"
        )
        social_hrefs = await page.eval_on_selector_all(
            "ul.authorprofile-links li.authorprofile-linkitem a.authorprofile-item",
            "nodes => nodes.map(n => n.href)"
        )

        if social_texts:
            for txt in social_texts:
                cleaned = txt.strip()
                if cleaned:
                    tokens.add(cleaned)

        if social_hrefs:
            for href in social_hrefs:
                cleaned = href.strip()
                if cleaned:
                    tokens.add(cleaned)

        return ", ".join(sorted(tokens)) if tokens else ""
    except Exception:
        return ""

async def save_pdf_with_metadata(playwright_page, url, drive_service, folder_id):
    await playwright_page.goto(url, wait_until="domcontentloaded", timeout=60000)
    await playwright_page.wait_for_timeout(2000)

    title_element = await playwright_page.query_selector("h1")
    title = await title_element.inner_text() if title_element else "No title found"

    byline_element = await playwright_page.query_selector("div.bylineDetails")
    author = "No author found"
    if byline_element:
        author_links = await playwright_page.query_selector_all("span.authorText a")
        if author_links:
            authors = []
            for link in author_links:
                authors.append(await link.inner_text())
            author = ", ".join(authors)
        else:
            byline_text = await byline_element.inner_text()
            author = byline_text.split("·")[0].strip()

    date_element = await playwright_page.query_selector(
        "time, .date, .posted-date, [class*='date']"
    )
    date_posted = await date_element.inner_text() if date_element else "No date found"

    date_str = datetime.now().strftime("%Y-%m-%d")
    safe_title = "".join(
        c for c in title if c.isalnum() or c in (" ", "-")
    ).replace(" ", "_")[:60]
    pdf_file = f"cbc_{safe_title}_{date_str}.pdf"

    print(f"Title: {title}")
    print(f"Author: {author}")
    print(f"Date posted: {date_posted}")
    print(f"Saving PDF: {pdf_file}")

    await playwright_page.pdf(
        path=pdf_file,
        format="A4",
        print_background=True,
        margin={"top": "10mm", "bottom": "10mm", "left": "10mm", "right": "10mm"}
    )

    file_metadata = {'name': os.path.basename(pdf_file), 'parents': [folder_id]}
    media = MediaFileUpload(pdf_file, mimetype='application/pdf', resumable=True)
    file = drive_service.files().create(
        body=file_metadata, media_body=media, fields='id'
    ).execute()
    print(f"Uploaded {pdf_file} to Google Drive with file ID {file['id']}")

    return (title, author, url, date_posted)

def append_to_google_sheet(data_rows, service):
    sheet = service.spreadsheets()
    body = {'values': data_rows}
    result = sheet.values().append(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{SHEET_NAME}!A:H",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body=body
    ).execute()
    print(f"{result.get('updates').get('updatedRows')} rows appended to Google Sheet")

def ensure_header_row(service):
    sheet = service.spreadsheets()
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
    sheet.values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{SHEET_NAME}!A1:H1",
        valueInputOption="RAW",
        body={'values': header}
    ).execute()

async def main():
    creds = get_oauth_credentials()
    drive_service = gsheet_build('drive', 'v3', credentials=creds)
    sheet_service = gsheet_build('sheets', 'v4', credentials=creds)
    ensure_header_row(sheet_service)

    capture_folder_id = create_dated_capture_folder(drive_service)

    homepage_url = "https://www.cbc.ca/news"
    date_str = datetime.now().strftime("%Y-%m-%d")
    homepage_pdf = f"cbc_homepage_{date_str}.pdf"

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(
            viewport={"width": 1600, "height": 4000},
            ignore_https_errors=True
        )
        page = await context.new_page()

        await page.goto(homepage_url, wait_until="domcontentloaded", timeout=120000)
        await page.wait_for_timeout(2000)

        article_urls = await extract_relevant_article_links(page)
        print(f"Filtered {len(article_urls)} article URLs after extraction.")
        for url in article_urls:
            print(url)

        await page.pdf(
            path=homepage_pdf,
            format="A4",
            print_background=True,
            margin={"top": "10mm", "bottom": "10mm", "left": "10mm", "right": "10mm"}
        )
        print(f"Homepage PDF saved as {homepage_pdf}")

        file_metadata = {'name': os.path.basename(homepage_pdf), 'parents': [capture_folder_id]}
        media = MediaFileUpload(homepage_pdf, mimetype='application/pdf', resumable=True)
        file = drive_service.files().create(
            body=file_metadata, media_body=media, fields='id'
        ).execute()
        print(f"Uploaded {homepage_pdf} to Google Drive with file ID {file['id']}")

        metadata_rows = []
        for link in article_urls:
            article_page = await context.new_page()
            meta = await save_pdf_with_metadata(
                article_page, link, drive_service, capture_folder_id
            )

            await trigger_player_links(article_page)

            video_audio_links, extra_author_info = await extract_cbc_article_info(article_page)
            ai_mention = await check_ai_mention(article_page)
            author_info = await extract_author_info(article_page)

            additional_affiliations = ", ".join(
                x for x in [extra_author_info] if x
            )

            row = (
                meta[0],  # Title
                meta[1],  # Author
                author_info,  # Social Media/Email
                meta[2],  # Link
                meta[3],  # Date Posted/Last Updated
                additional_affiliations,  # Additional Affiliations
                "\n".join(video_audio_links) if video_audio_links else "",  # Video/Audio Flag
                ai_mention  # AI Mention?
            )
            metadata_rows.append(row)
            await article_page.close()

        await browser.close()

    append_to_google_sheet(metadata_rows, sheet_service)

if __name__ == "__main__":
    asyncio.run(main())
